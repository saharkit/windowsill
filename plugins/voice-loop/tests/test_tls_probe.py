"""The TLS probe's pure parts: how a failure is classified, which fix it names on which machine,
and that ``--fix`` only claims green after a SECOND probe says so.

Nothing here touches the network or spawns anything: the https request is one injected callable
(``prober``) and the repair is another (``runner``), which is the same seam-and-real-body shape the
server's suite uses. The live behaviour — a real handshake against a real host, and the same probe
with the certificate store emptied out — is a real invocation in CI (see TESTING.md); what a fake
structurally cannot prove is not claimed here.
"""

from __future__ import annotations

import importlib.util
import ssl
import subprocess
import urllib.error
from pathlib import Path

import pytest

_PROBE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "tls-probe.py"
_spec = importlib.util.spec_from_file_location("tls_probe", _PROBE_PATH)
tls_probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tls_probe)


FRAMEWORK_PREFIX = "/Library/Frameworks/Python.framework/Versions/3.10"
CERT_MESSAGE = (
    "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer "
    "certificate (_ssl.c:1007)"
)


def cert_error() -> urllib.error.URLError:
    """What urllib actually hands back on the trap this script exists to name."""
    return urllib.error.URLError(ssl.SSLCertVerificationError(CERT_MESSAGE))


def raising(err: BaseException):
    def prober(url: str, timeout: float) -> None:
        raise err

    return prober


def ok_prober(url: str, timeout: float) -> None:
    return None


# --- classification: the three answers, and only three ------------------------------------------


def test_a_verified_handshake_is_ok():
    assert tls_probe.probe("https://example.invalid/", 1.0, ok_prober) == ("ok", "verified")


def test_an_http_error_status_still_means_the_certificate_verified():
    # 401 from a cloud API and 404 from a bare endpoint are both handshakes that completed.
    err = urllib.error.HTTPError("https://api.example/", 401, "Unauthorized", {}, None)
    result, detail = tls_probe.probe("https://api.example/", 1.0, raising(err))
    assert result == "ok"
    assert "401" in detail


def test_a_certificate_failure_is_named_as_one():
    result, detail = tls_probe.probe("https://pypi.org/", 1.0, raising(cert_error()))
    assert result == "certificate"
    assert "unable to get local issuer certificate" in detail


def test_a_refused_connection_is_unreachable_not_a_certificate_problem():
    result, _ = tls_probe.probe("https://pypi.org/", 1.0, raising(urllib.error.URLError(OSError("refused"))))
    assert result == "unreachable"


def test_a_certificate_failure_is_recognized_however_it_is_wrapped():
    # typed and nested, typed and bare, and message-only — urllib's wrapping has moved between
    # versions, and the classification may not move with it.
    assert tls_probe.is_cert_failure(cert_error())
    assert tls_probe.is_cert_failure(ssl.SSLCertVerificationError(CERT_MESSAGE))
    assert tls_probe.is_cert_failure(urllib.error.URLError(urllib.error.URLError(OSError(CERT_MESSAGE))))
    assert not tls_probe.is_cert_failure(urllib.error.URLError(OSError("connection refused")))


def test_text_that_merely_mentions_the_marker_is_not_a_certificate_failure():
    # A proxy error body or a wrapped tunnel failure can carry the marker in its text; answering
    # that with "run the installer" would repair the wrong machine, so the message fallback only
    # applies to an OSError (which URLError and every ssl error already are).
    assert not tls_probe.is_cert_failure(ValueError(CERT_MESSAGE))
    assert tls_probe.is_cert_failure(OSError(CERT_MESSAGE))


# --- which fix, on which machine ----------------------------------------------------------------


def test_the_python_org_layout_is_detected_and_its_own_installer_is_the_fix():
    fix = tls_probe.remedy(
        system="Darwin", base_prefix=FRAMEWORK_PREFIX, executable="/usr/bin/python3", environ={},
        exists=lambda path: path == "/Applications/Python 3.10/Install Certificates.command",
    )
    assert fix["kind"] == "install-certificates-command"
    assert fix["runnable"] is True
    assert fix["command"] == "/Applications/Python 3.10/Install Certificates.command"


def test_the_framework_version_comes_from_the_layout_not_from_the_running_version():
    assert tls_probe.framework_version("/Library/Frameworks/Python.framework/Versions/3.13/bin") == "3.13"
    assert tls_probe.framework_version("/opt/homebrew/opt/python@3.12") is None
    assert tls_probe.framework_version("/usr") is None


def test_the_python_org_layout_without_its_installer_falls_back_to_the_relink_it_would_have_done():
    fix = tls_probe.remedy(
        system="Darwin", base_prefix=FRAMEWORK_PREFIX, executable="/py", environ={}, exists=lambda path: False,
    )
    assert fix["kind"] == "certifi-relink"
    assert fix["runnable"] is False  # a shell pipeline is printed, never run for the user
    assert "certifi" in fix["command"]


def test_a_homebrew_mac_is_told_the_trap_does_not_apply_to_it():
    fix = tls_probe.remedy(
        system="Darwin", base_prefix="/opt/homebrew/opt/python@3.12", executable="/py", environ={},
        exists=lambda path: False,
    )
    assert fix["kind"] == "homebrew-certifi"
    assert "not python.org-installer Python" in fix["why"]


def test_linux_is_pointed_at_the_system_trust_store():
    fix = tls_probe.remedy(system="Linux", base_prefix="/usr", executable="/usr/bin/python3", environ={})
    assert fix["kind"] == "system-trust-store"
    assert "ca-certificates" in fix["command"]


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        ({"SSL_CERT_FILE": "/empty.pem"}, "unset SSL_CERT_FILE"),
        ({"SSL_CERT_DIR": "/empty"}, "unset SSL_CERT_DIR"),
        ({"SSL_CERT_FILE": "/empty.pem", "SSL_CERT_DIR": "/empty"}, "unset SSL_CERT_FILE SSL_CERT_DIR"),
    ],
)
def test_an_env_override_outranks_every_other_diagnosis(environ, expected):
    # Repairing the interpreter's own store changes nothing while one of these stands, so it is the
    # answer even on the layout that would otherwise get Install Certificates.command.
    fix = tls_probe.remedy(
        system="Darwin", base_prefix=FRAMEWORK_PREFIX, executable="/py", environ=environ,
        exists=lambda path: True,
    )
    assert fix["kind"] == "env-override"
    assert fix["command"] == expected


# --- the message: it names the fix, which is the acceptance criterion ---------------------------


def test_the_failure_message_names_install_certificates_command_verbatim(monkeypatch):
    """TESTING.md row 5.6, in a test: on the python.org layout the rendered message carries the
    installer's path VERBATIM under "Fix, exactly:", and offers to run it.

    ``remedy()`` is pinned to that machine rather than read off this one — asserting
    ``report["fix"]["command"] in message`` would pass for every remedy on every host, which is
    exactly what a criterion about ONE branch must not do.
    """
    on_that_mac = tls_probe.remedy(
        system="Darwin", base_prefix=FRAMEWORK_PREFIX, executable="/py", environ={},
        exists=lambda path: path == "/Applications/Python 3.10/Install Certificates.command",
    )
    monkeypatch.setattr(tls_probe, "remedy", lambda **kwargs: on_that_mac)
    report = tls_probe.run("https://pypi.org/", 1.0, False, raising(cert_error()))
    message = tls_probe.render(report)
    assert "FAIL: certificate verification failed" in message
    assert "unable to get local issuer certificate" in message
    assert report["fix"]["kind"] == "install-certificates-command"
    assert "Fix, exactly:\n          /Applications/Python 3.10/Install Certificates.command" in message
    assert "--fix" in message  # it is runnable, so the message offers it
    # and not the diagnosis for a machine this is not
    assert "unset SSL_CERT_FILE" not in message


def test_an_env_override_failure_names_the_unset_not_the_installer(monkeypatch):
    # The other half of the pair: with the store overridden, the message must NOT send the user to
    # an installer that would change nothing while the override stands. (This is what the CI
    # "empty store via SSL_CERT_FILE" leg actually proves — see selftest.yml.)
    with_the_store_overridden = tls_probe.remedy(
        system="Darwin", base_prefix=FRAMEWORK_PREFIX, executable="/py",
        environ={"SSL_CERT_FILE": "/empty.pem", "SSL_CERT_DIR": "/empty"}, exists=lambda path: True,
    )
    monkeypatch.setattr(tls_probe, "remedy", lambda **kwargs: with_the_store_overridden)
    message = tls_probe.render(tls_probe.run("https://pypi.org/", 1.0, False, raising(cert_error())))
    assert "Fix, exactly:\n          unset SSL_CERT_FILE SSL_CERT_DIR" in message
    assert "Install Certificates.command" not in message
    assert "--fix" not in message  # an unset is the user's own shell to do


def test_the_unreachable_message_does_not_pretend_to_know_about_tls():
    report = tls_probe.run("https://pypi.org/", 1.0, False, raising(urllib.error.URLError(OSError("no route"))))
    message = tls_probe.render(report)
    assert "UNKNOWN" in message
    assert "neither proven nor disproven" in message
    assert report["fix"] is None


def test_a_credential_in_the_endpoint_is_never_printed_back(monkeypatch):
    # An endpoint can carry userinfo or a key in its query string, and this message lands in the
    # agent transcript and in whatever the user pastes into an issue.
    assert tls_probe.display_url("https://user:s3cret@speech.example/v1?key=abc") == "https://speech.example/v1"
    assert tls_probe.display_url("https://pypi.org/") == "https://pypi.org/"
    assert tls_probe.display_url("not-a-url") == "not-a-url"
    report = tls_probe.run("https://user:s3cret@speech.example/", 1.0, False, raising(cert_error()))
    rendered = tls_probe.render(report)
    assert "s3cret" not in rendered and "s3cret" not in report["url"]


def test_a_green_under_a_configured_proxy_says_what_it_did_not_cover(monkeypatch):
    # The probe bypasses proxies on purpose; pip and the model download do not. Without this line a
    # bypassed proxy with an untrusted CA reads as "everything verifies" and fails an hour later.
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
    report = tls_probe.run("https://pypi.org/", 1.0, False, ok_prober)
    assert report["proxy"] == "HTTPS_PROXY"
    assert "bypassed it" in tls_probe.render(report)


def test_a_green_with_no_proxy_configured_says_nothing_extra(monkeypatch):
    for name in tls_probe.PROXY_VARS:
        monkeypatch.delenv(name, raising=False)
    report = tls_probe.run("https://pypi.org/", 1.0, False, ok_prober)
    assert report["proxy"] == ""
    assert "bypassed" not in tls_probe.render(report)


# --- --fix: red to green, and the second probe that proves it -----------------------------------


class Flaky:
    """A prober that fails until the repair has run — the cert-less install, in one object."""

    def __init__(self) -> None:
        self.repaired = False
        self.probes = 0

    def __call__(self, url: str, timeout: float) -> None:
        self.probes += 1
        if not self.repaired:
            raise cert_error()


def completed(returncode: int = 0, stderr: bytes = b""):
    return subprocess.CompletedProcess(args=["x"], returncode=returncode, stderr=stderr, stdout=b"")


def test_fix_runs_the_installer_and_only_claims_green_after_a_second_probe(monkeypatch):
    monkeypatch.setattr(tls_probe, "remedy", lambda **kwargs: {
        "kind": "install-certificates-command", "runnable": True,
        "command": "/Applications/Python 3.10/Install Certificates.command", "why": "empty store",
    })
    prober = Flaky()
    spawned: list[list[str]] = []

    def runner(argv, timeout):
        spawned.append(argv)
        prober.repaired = True
        return completed()

    report = tls_probe.run("https://pypi.org/", 1.0, True, prober, runner)
    assert report["result"] == "ok"
    assert report["fixed"] is True
    assert prober.probes == 2  # probed, repaired, probed AGAIN — the claim rests on the second one
    # argv, not a shell string: the installer's path contains a space and must stay ONE argument
    assert spawned == [["/Applications/Python 3.10/Install Certificates.command"]]
    assert "repaired by" in tls_probe.render(report)


def test_a_repair_that_does_not_take_leaves_the_probe_red(monkeypatch):
    monkeypatch.setattr(tls_probe, "remedy", lambda **kwargs: {
        "kind": "install-certificates-command", "runnable": True, "command": "/installer", "why": "empty store",
    })
    report = tls_probe.run("https://pypi.org/", 1.0, True, raising(cert_error()),
                           lambda argv, timeout: completed(1, b"Permission denied"))
    assert report["result"] == "certificate"
    assert report["fixed"] is False
    assert "Permission denied" in tls_probe.render(report)


def test_a_repair_that_exits_zero_and_changes_nothing_says_which_half_failed(monkeypatch):
    # The most confusing state there is: the command ran fine and the probe is STILL red. The
    # second probe is what catches it, and the message has to name that rather than leave the user
    # to re-run it hopefully.
    monkeypatch.setattr(tls_probe, "remedy", lambda **kwargs: {
        "kind": "install-certificates-command", "runnable": True, "command": "/installer", "why": "empty store",
    })
    prober = Flaky()  # never repaired: the runner exits 0 without fixing anything

    report = tls_probe.run("https://pypi.org/", 1.0, True, prober, lambda argv, timeout: completed(0))
    assert report["result"] == "certificate"
    assert report["fixed"] is False
    assert prober.probes == 2  # it DID re-probe — that is how the "still red" is known
    assert "the command itself exited 0" in tls_probe.render(report)


def test_fix_leaves_a_printed_only_remedy_alone(monkeypatch):
    monkeypatch.setattr(tls_probe, "remedy", lambda **kwargs: {
        "kind": "certifi-relink", "runnable": False, "command": "pip install certifi && ln -sf …", "why": "no installer",
    })

    def runner(argv, timeout):  # pragma: no cover - reaching this IS the failure
        raise AssertionError("a printed-only remedy must never be executed")

    report = tls_probe.run("https://pypi.org/", 1.0, True, raising(cert_error()), runner)
    assert report["result"] == "certificate"
    assert "--fix" not in tls_probe.render(report)  # it does not offer what it will not do


@pytest.mark.parametrize(
    ("error", "detail_fragment"),
    [
        (subprocess.TimeoutExpired(cmd="x", timeout=1), "timed out"),
        (OSError("Permission denied"), "OSError"),
    ],
)
def test_an_unrunnable_repair_is_a_result_not_an_exception(error, detail_fragment):
    def runner(argv, timeout):
        raise error

    ok, detail = tls_probe.apply_fix("/installer", runner)
    assert ok is False
    assert detail_fragment in detail


# --- which host gets probed ---------------------------------------------------------------------


def test_an_https_endpoint_in_the_config_is_what_gets_probed():
    assert tls_probe.resolve_url({"tts": {"endpoint": "https://speech.example/"}}) == "https://speech.example"
    assert tls_probe.resolve_url({"stt": {"endpoint": "https://speech.example"}}) == "https://speech.example"


def test_a_plain_http_endpoint_carries_no_certificate_so_the_fallback_host_is_probed():
    # A LAN server on http has nothing to verify — but the model download and `pip install` on the
    # same machine still do, and that is what the fallback host stands for.
    assert tls_probe.resolve_url({"tts": {"endpoint": "http://127.0.0.1:8355"}}) == tls_probe.DEFAULT_URL


def test_a_configured_cloud_voice_is_probed_at_its_own_api_host():
    config = {"tts": {"backend": "cloud", "cloud": {"provider": "elevenlabs"}}}
    assert tls_probe.resolve_url(config) == tls_probe.ELEVENLABS_HOST


def test_an_empty_config_falls_back_to_the_host_pip_needs():
    assert tls_probe.resolve_url({}) == tls_probe.DEFAULT_URL


def test_the_openai_compatible_cloud_path_has_no_remote_default_host_of_its_own():
    # Deliberate, and easy to misread as a gap: speak.py defaults the OpenAI-compatible endpoint to
    # the LOCAL server on http, so there is no https host to probe unless one is configured — in
    # which case tts.endpoint above already wins. ElevenLabs is special-cased because it is the one
    # provider whose default IS a remote host.
    unset = {"tts": {"backend": "cloud"}}
    openai = {"tts": {"backend": "cloud", "cloud": {"provider": "openai"}}}
    configured = {"tts": {"backend": "cloud", "cloud": {"provider": "openai"}, "endpoint": "https://api.openai.com"}}
    assert tls_probe.resolve_url(unset) == tls_probe.DEFAULT_URL
    assert tls_probe.resolve_url(openai) == tls_probe.DEFAULT_URL
    assert tls_probe.resolve_url(configured) == "https://api.openai.com"


# --- the config the URL comes from ---------------------------------------------------------------


def test_the_config_file_is_read_from_where_the_environment_points(tmp_path):
    written = tmp_path / "config.json"
    written.write_text('{"tts": {"endpoint": "https://speech.example/"}}', encoding="utf-8")
    environ = {"VOICE_LOOP_CONFIG": str(written)}
    assert tls_probe.config_path(environ) == str(written)
    loaded = tls_probe.load_config(tls_probe.config_path(environ))
    assert loaded == {"tts": {"endpoint": "https://speech.example/"}}


def test_an_absent_or_unreadable_config_is_an_empty_one_not_a_crash(tmp_path):
    # A half-written or hand-edited config must not stop the probe: no config simply means the
    # fallback host, which is the honest answer rather than a traceback in the middle of setup.
    assert tls_probe.load_config(str(tmp_path / "nothing.json")) == {}
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert tls_probe.load_config(str(broken)) == {}
    a_list = tmp_path / "list.json"
    a_list.write_text("[1, 2]", encoding="utf-8")
    assert tls_probe.load_config(str(a_list)) == {}


def test_config_path_defaults_under_xdg_config_home():
    assert tls_probe.config_path({"XDG_CONFIG_HOME": "/x"}) == "/x/voice-loop/config.json"


def test_with_no_url_flag_the_probe_goes_to_the_host_the_config_names(tmp_path, monkeypatch, capsys):
    """The invocation every doc and SKILL.md snippet uses: `tls-probe.sh`, no arguments. It is the
    only path that reads the config at all, so it is the one that decides which host is contacted."""
    written = tmp_path / "config.json"
    written.write_text('{"tts": {"endpoint": "https://speech.example/"}}', encoding="utf-8")
    monkeypatch.setenv("VOICE_LOOP_CONFIG", str(written))
    probed: list[str] = []

    def prober(url: str, timeout: float) -> None:
        probed.append(url)

    assert tls_probe.main([], prober) == 0
    assert probed == ["https://speech.example"]
    assert "https://speech.example" in capsys.readouterr().out  # lgtm[py/incomplete-url-substring-sanitization]


# --- the command line ---------------------------------------------------------------------------


def test_exit_codes_distinguish_the_three_outcomes(capsys):
    assert tls_probe.main(["--url", "https://x.example/"], ok_prober) == 0
    assert tls_probe.main(["--url", "https://x.example/"], raising(cert_error())) == 1
    assert tls_probe.main(["--url", "https://x.example/"], raising(urllib.error.URLError(OSError("down")))) == 2
    capsys.readouterr()


def test_being_called_wrong_is_not_reported_as_the_certificate_trap(capsys):
    # 64, never 1: /voice-setup is told "exit 1 is the trap, run what the message names", so a
    # typo'd flag coming back as 1 is how an agent offers to run an installer for a mis-invocation.
    assert tls_probe.main(["--url", "http://127.0.0.1:8355"], ok_prober) == tls_probe.EXIT_USAGE
    assert "no certificate to verify" in capsys.readouterr().err
    assert tls_probe.main(["--no-such-flag"], ok_prober) == tls_probe.EXIT_USAGE
    assert "unknown argument: --no-such-flag" in capsys.readouterr().err
    assert tls_probe.main(["--timeout", "soon"], ok_prober) == tls_probe.EXIT_USAGE
    assert "wants a number of seconds" in capsys.readouterr().err
    assert tls_probe.EXIT_USAGE not in tls_probe.EXIT_CODES.values()


@pytest.mark.parametrize("flag", ["--url", "--timeout"])
def test_a_flag_left_without_its_value_says_so_instead_of_unknown_argument(flag, capsys):
    assert tls_probe.main([flag], ok_prober) == tls_probe.EXIT_USAGE
    assert f"{flag} wants a value after it" in capsys.readouterr().err


def test_a_valid_timeout_is_accepted_and_carried_to_the_probe(capsys):
    seen: list[float] = []

    def prober(url: str, timeout: float) -> None:
        seen.append(timeout)

    assert tls_probe.main(["--url", "https://x.example/", "--timeout", "2.5"], prober) == 0
    assert seen == [2.5]
    capsys.readouterr()


def test_json_output_is_one_parseable_object(capsys):
    import json

    assert tls_probe.main(["--url", "https://x.example/", "--json"], raising(cert_error())) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["result"] == "certificate"
    assert report["fix"]["command"]


def test_help_prints_the_usage_and_exits_clean(capsys):
    assert tls_probe.main(["--help"], ok_prober) == 0
    assert "Usage:" in capsys.readouterr().out
