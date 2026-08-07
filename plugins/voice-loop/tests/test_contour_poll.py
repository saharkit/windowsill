"""The contour poller's pure parts: config normalization, the two probes at their seams, the
alert rules, and the atomic status file — plus the CLI's exit-code contract (0 quiet, 1 page,
64 called wrong).

Nothing here touches the network, a GPU, or the real state dir: the health fetch is one injected
callable (``fetch``), the nvidia-smi spawn is another (``runner``), and the clock is a third — the
same seam-and-real-body shape the rest of the suite uses. The two exceptions are deliberate and
narrow: ``sample_vram``'s DEFAULT runner is exercised against a real subprocess (a stub script,
never nvidia-smi), because a probe nothing ever spawns is a probe nobody has run; and the atomic
write is pinned at ``os.replace`` rather than by its outcome, because the outcome of an atomic
write and of a truncating one are the same file. The real invocation — a poll against the live
server the loopback job starts, including a REAL demotion alert on a GPU-less runner — is in CI
(see TESTING.md); what a fake structurally cannot prove is not claimed here.
"""

from __future__ import annotations

import http.client
import importlib.util
import json
import os
import shlex
import subprocess
import sys
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

import pytest

_POLL_PATH = Path(__file__).resolve().parents[1] / "scripts" / "contour_poll.py"
_spec = importlib.util.spec_from_file_location("contour_poll", _POLL_PATH)
contour_poll = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(contour_poll)

# The fixed clock every poll in this file runs on: time is an input, never an ambient read.
_T0 = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)


# --- normalize_service / resolve_settings: the config surface -----------------------------------


def test_a_bare_url_string_becomes_a_service_named_by_where_it_lives():
    service = contour_poll.normalize_service("http://127.0.0.1:8358/health")
    assert service == {
        "name": "127.0.0.1:8358",
        "health": "http://127.0.0.1:8358/health",
        "expect_device": "",
    }


def test_a_full_entry_keeps_its_name_and_its_expectation():
    service = contour_poll.normalize_service(
        {"name": "rvc", "health": "http://127.0.0.1:8358/health", "expect_device": "gpu"}
    )
    assert service["name"] == "rvc"
    assert service["expect_device"] == "gpu"


def test_a_control_character_in_a_service_name_is_stripped_before_it_meets_the_line_based_ledger():
    # The announced-ledger is one key per line and the log format is single-line; a "\n" embedded in
    # a name would split both on read-back — the same truncate-and-re-voice failure the whitespace
    # fix closed for spaces, one character over.
    service = contour_poll.normalize_service({"name": "voice\nserver", "health": "http://127.0.0.1:8358/health"})
    assert service["name"] == "voice server"
    # a name that is NOTHING BUT control characters falls back to the URL-derived name, never ""
    blank = contour_poll.normalize_service({"name": "\x00\n\t", "health": "http://127.0.0.1:8358/health"})
    assert blank["name"] == "127.0.0.1:8358"


def test_an_entry_without_a_usable_url_is_not_a_service():
    # A configured service that can never be polled is a monitoring hole that looks like coverage
    # — normalize to None and let the caller be loud about it.
    assert contour_poll.normalize_service("not-a-url") is None
    assert contour_poll.normalize_service({"name": "rvc"}) is None
    assert contour_poll.normalize_service({"health": "ftp://example/health"}) is None
    assert contour_poll.normalize_service(8358) is None


def test_the_default_contour_is_the_local_speech_server_and_nothing_else():
    settings = contour_poll.resolve_settings({})
    assert [s["health"] for s in settings["services"]] == ["http://127.0.0.1:8355/health"]
    # loopback only — no real host is ever baked into the shipped defaults
    assert settings["timeout"] == 5.0
    assert settings["vram_min_free_mib"] == 200
    assert settings["status_path"] == contour_poll.STATUS_PATH


def test_contour_knobs_come_from_the_config():
    settings = contour_poll.resolve_settings(
        {"contour": {"timeout": 2, "vram": {"min_free_mib": 512, "command": False}, "status_path": "/var/tmp/c.json"}}
    )
    assert settings["timeout"] == 2.0
    assert settings["vram_min_free_mib"] == 512
    assert settings["vram_command"] == ""  # JSON false disables the probe; "" cannot (cfg parity)
    assert settings["status_path"] == "/var/tmp/c.json"


def test_an_explicit_null_or_empty_value_is_unset_which_is_why_false_disables_the_vram_probe():
    # bash-cfg parity, and the reason contour.vram.command takes JSON `false` rather than "": an
    # empty string IS "unset" everywhere in this plugin, so "" would re-enable the probe it looks
    # like it disables. Pinned here because that is the one place the rule is load-bearing.
    assert contour_poll.cfg({"contour": {"timeout": None}}, "contour.timeout", 5) == 5
    assert contour_poll.cfg({"contour": {"status_path": ""}}, "contour.status_path", "/d/c.json") == "/d/c.json"
    assert contour_poll.resolve_settings({"contour": {"vram": {"command": ""}}})["vram_command"] == contour_poll.DEFAULT_VRAM_COMMAND
    assert contour_poll.resolve_settings({"contour": {"vram": {"command": False}}})["vram_command"] == ""


def test_a_configured_entry_that_can_never_be_polled_is_an_error_not_a_skip():
    # A configured service this poller silently never polls is a monitoring hole wearing the word
    # "ok" — so it stops the run rather than shrinking the contour behind the operator's back.
    with pytest.raises(contour_poll.ConfigError, match="no fetchable"):
        contour_poll.resolve_settings({"contour": {"services": ["not-a-url"]}})


def test_services_configured_is_pinned_by_the_line_it_changes_not_by_itself(tmp_path, monkeypatch, capsys):
    """The flag exists for ONE reason: the success line. "contour ok" printed over the shipped
    default, while the operator believes their own list is being watched, is the coverage lie this
    whole poller exists to prevent — so the assertion is on what main() SAYS in both cases, not on
    the flag's value. (Asserting the value alone left the flag deletable with a green suite.)"""
    monkeypatch.setattr(contour_poll, "fetch_health", lambda url, timeout: {"ok": True, "device": "cuda"})
    monkeypatch.setattr(contour_poll, "sample_vram", lambda command, timeout, runner: None)
    status = str(tmp_path / "c.json")

    _config_file(tmp_path, monkeypatch, {})
    assert contour_poll.main(["--status", status]) == 0
    assert "contour.services unset — the default local service only" in capsys.readouterr().out

    _config_file(tmp_path, monkeypatch, {"contour": {"services": ["http://a/health"]}})
    assert contour_poll.main(["--status", status]) == 0
    assert "contour.services unset" not in capsys.readouterr().out

    # …and the settings key the line is computed from, so a rename cannot pass silently
    assert contour_poll.resolve_settings({})["services_configured"] is False
    assert contour_poll.resolve_settings({"contour": {"services": ["http://a/health"]}})["services_configured"] is True


def test_a_services_value_that_is_not_a_list_is_an_error_not_a_silent_default():
    # {"services": {...}} is the natural typo, and it used to fall back to DEFAULT_SERVICES: the
    # poller then polled loopback, printed "contour ok", and none of the three configured
    # services was ever looked at.
    with pytest.raises(contour_poll.ConfigError, match="must be a list"):
        contour_poll.resolve_settings({"contour": {"services": {"speech": "http://127.0.0.1:8355/health"}}})


def test_two_entries_resolving_to_one_name_are_an_error_not_a_silent_merge():
    # Same host:port, different paths: both normalize to the same name, so one overwrote the
    # other's slot, the page said "X; X", and one history sample was lost every poll.
    with pytest.raises(contour_poll.ConfigError, match="two entries named"):
        contour_poll.resolve_settings(
            {"contour": {"services": ["http://127.0.0.1:8355/health", "http://127.0.0.1:8355/healthz"]}}
        )
    # naming one of them is the fix the message asks for, and it resolves
    settings = contour_poll.resolve_settings(
        {"contour": {"services": ["http://127.0.0.1:8355/health", {"name": "spare", "health": "http://127.0.0.1:8355/healthz"}]}}
    )
    assert [s["name"] for s in settings["services"]] == ["127.0.0.1:8355", "spare"]


@pytest.mark.parametrize(
    ("dotted", "config"),
    [
        ("contour.timeout", {"contour": {"timeout": "5s"}}),
        ("contour.vram.min_free_mib", {"contour": {"vram": {"min_free_mib": "200 MiB"}}}),
        ("contour.max_age", {"contour": {"max_age": "10m"}}),
    ],
)
def test_a_mistyped_scalar_knob_is_a_named_config_error_not_a_bare_valueerror(dotted, config):
    # A bare ValueError out of here reaches Python's own handler, which exits 1 — the PAGE code.
    with pytest.raises(contour_poll.ConfigError, match=dotted.replace(".", r"\.")):
        contour_poll.resolve_settings(config)


@pytest.mark.parametrize(
    ("dotted", "config"),
    [
        ("contour.timeout", {"contour": {"timeout": "5"}}),
        ("contour.vram.min_free_mib", {"contour": {"vram": {"min_free_mib": "200"}}}),
        ("contour.max_age", {"contour": {"max_age": "900"}}),
        ("contour.timeout", {"contour": {"timeout": True}}),
    ],
)
def test_a_quoted_number_is_the_same_typo_as_a_unit_suffix_and_is_refused_with_it(dotted, config):
    """``"5"`` used to resolve to 5.0 while ``"5s"`` was a 64 — one rule for the string that
    happens to parse and another for the one that does not, which is a vocabulary no operator can
    predict and which lets the quoted form go on being written. JSON has a number type; ``true``
    is refused with them, because Python reading it as 1 is an accident, not the config's meaning.
    """
    with pytest.raises(contour_poll.ConfigError, match="must be a JSON number"):
        contour_poll.resolve_settings(config)


def test_a_url_urllib_could_never_fetch_is_rejected_at_the_config_not_at_the_fetch():
    # Both shapes parse fine and then raise out of the OPENER — a UnicodeError and a ValueError
    # that used to leave main() as exit 1, the page code, for what is plainly a typo.
    assert contour_poll.normalize_service("http://a..b/health") is None
    assert contour_poll.normalize_service("http://x:notaport/health") is None
    assert contour_poll.normalize_service("http://:8355/health") is None  # a port and no host
    assert contour_poll.normalize_service("http://[::1/health") is None
    assert contour_poll.normalize_service("http://" + "x" * 300 + ".example/health") is None
    # …and the ordinary ones still resolve
    assert contour_poll.normalize_service("http://[::1]:8355/health")["name"] == "[::1]:8355"


# --- fetch_health: the bounded probe --------------------------------------------------------------


class _Resp:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *args) -> bool:
        return False

    def read(self, n: int) -> bytes:
        return self._body[:n]


def _opener(body: bytes):
    return lambda url, timeout: _Resp(body)


def test_a_health_body_parses_to_a_dict():
    assert contour_poll.fetch_health("http://x/health", 1.0, _opener(b'{"ok": true}')) == {"ok": True}


def test_an_http_error_status_is_an_availability_answer_not_a_crash():
    def raising(url, timeout):
        raise urllib.error.HTTPError(url, 500, "broken", {}, None)

    with pytest.raises(contour_poll.FetchError, match="http 500"):
        contour_poll.fetch_health("http://x/health", 1.0, raising)


def test_a_refused_connection_is_a_fetch_error():
    def raising(url, timeout):
        raise urllib.error.URLError(OSError("refused"))

    with pytest.raises(contour_poll.FetchError):
        contour_poll.fetch_health("http://x/health", 1.0, raising)


def test_a_framing_error_on_the_health_port_is_this_services_fetch_error_not_a_poll_abort():
    # http.client.HTTPException (BadStatusLine, LineTooLong, IncompleteRead) is neither URLError nor
    # OSError — before the catch widened (#40 Gate B finding 10546), one neighbour speaking garbage
    # on its health port escaped fetch_health, blew past poll()'s FetchError-only per-service try,
    # and cost every OTHER service and the VRAM sample their samples for the cycle.
    def raising(url, timeout):
        raise http.client.BadStatusLine("garbage on the wire")

    with pytest.raises(contour_poll.FetchError, match="BadStatusLine"):
        contour_poll.fetch_health("http://x/health", 1.0, raising)


def test_the_body_is_capped():
    over = b" " * (contour_poll.BODY_CAP + 1)
    with pytest.raises(contour_poll.FetchError, match="body over"):
        contour_poll.fetch_health("http://x/health", 1.0, _opener(over))


def test_a_body_that_is_not_a_json_object_is_a_fetch_error():
    with pytest.raises(contour_poll.FetchError):
        contour_poll.fetch_health("http://x/health", 1.0, _opener(b"not json"))
    with pytest.raises(contour_poll.FetchError):
        contour_poll.fetch_health("http://x/health", 1.0, _opener(b"[1, 2]"))


# --- sample_vram: the bounded subprocess ----------------------------------------------------------


class _Done:
    def __init__(self, stdout: str, returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode


def test_free_vram_is_the_tightest_card():
    def runner(argv, timeout):
        assert isinstance(argv, list)  # argv, never a shell string
        return _Done("2206\n1161\n")

    assert contour_poll.sample_vram("nvidia-smi --query-gpu=memory.free", 5.0, runner) == 1161


def test_an_empty_command_disables_the_probe():
    def never(argv, timeout):
        raise AssertionError("a disabled probe spawns nothing")

    assert contour_poll.sample_vram("", 5.0, never) is None


def test_no_answer_worth_having_is_none_not_an_alert():
    assert contour_poll.sample_vram("nvidia-smi", 5.0, lambda argv, timeout: _Done("", 1)) is None
    assert contour_poll.sample_vram("nvidia-smi", 5.0, lambda argv, timeout: _Done("garbage\n")) is None

    def missing(argv, timeout):
        raise FileNotFoundError("nvidia-smi")

    def wedged(argv, timeout):
        raise subprocess.TimeoutExpired(argv, timeout)

    assert contour_poll.sample_vram("nvidia-smi", 5.0, missing) is None
    assert contour_poll.sample_vram("nvidia-smi", 5.0, wedged) is None


def _stub_smi(tmp_path, body: str, name: str = "fake-smi.py") -> str:
    """A REAL executable command line for the default runner — a python script, never nvidia-smi."""
    script = tmp_path / name
    script.write_text(body, encoding="utf-8")
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"


def test_the_default_runner_really_spawns_the_probe(tmp_path):
    """``_default_runner`` — the one place this poller starts a process — was reached by no test
    and by no CI step (the gate configures ``vram.command: false``), so the whole real-subprocess
    path shipped unexecuted. This runs it for real, three ways, with a stub in place of nvidia-smi:
    the machine needs no GPU for the SPAWN to be the thing under test."""
    # a card that answers: the argv list, the pipe, the parse, the minimum
    assert contour_poll.sample_vram(_stub_smi(tmp_path, "print('2206')\nprint('1161')\n"), 10.0) == 1161
    # a probe that exits non-zero is no answer worth having, not an alert
    assert contour_poll.sample_vram(_stub_smi(tmp_path, "raise SystemExit(2)\n", "rc2.py"), 10.0) is None
    # and a binary that is not on this machine at all — the OSError arm, really raised by exec
    assert contour_poll.sample_vram("/nonexistent/nvidia-smi --query-gpu=memory.free", 10.0) is None


def test_a_wedged_probe_is_killed_by_its_own_timeout_for_real(tmp_path):
    """The mandatory wall-clock timeout, proven by a process that would otherwise never return —
    the bound on a poll's total time (see the module docstring) rests on this being real."""
    command = _stub_smi(tmp_path, "import time\ntime.sleep(30)\n", "wedged.py")
    assert contour_poll.sample_vram(command, 0.5) is None


# --- evaluate_service / evaluate_vram: the four rules ----------------------------------------------


def _service(**overrides):
    base = {"name": "rvc", "health": "http://x/health", "expect_device": ""}
    base.update(overrides)
    return base


def _sample(**overrides):
    base = {"reachable": True, "ok": True, "device": "cuda", "oom_overflows": 0, "detail": ""}
    base.update(overrides)
    return base


def test_an_unreachable_service_pages_once_and_nothing_more_can_be_known():
    alerts = contour_poll.evaluate_service(_service(expect_device="gpu"), _sample(reachable=False, detail="refused"), {})
    assert [a["kind"] for a in alerts] == ["unreachable"]
    assert "refused" in alerts[0]["message"]


def test_a_health_answer_of_not_ok_is_an_alert():
    alerts = contour_poll.evaluate_service(_service(), _sample(ok=False), {})
    assert [a["kind"] for a in alerts] == ["not-ok"]
    # …and ONLY an explicit false. A document that does not say is not a document that says no.
    assert contour_poll.evaluate_service(_service(), _sample(ok=None), {}) == []


def test_a_foreign_health_document_is_answering_not_down(tmp_path):
    """The README's own example config, run for real: the "converter" entry at 192.0.2.10:8358
    publishes {"status": "healthy", …} and no ``ok`` key at all. Reading a missing field as
    ok=false made every third-party /health a PERMANENT alert — a monitor crying wolf about a
    service that never had a fault, which is how a page stops being listened to."""
    settings = contour_poll.resolve_settings(
        {
            "contour": {
                "services": [
                    {"name": "speech", "health": "http://127.0.0.1:8355/health"},
                    {"name": "converter", "health": "http://192.0.2.10:8358/health", "expect_device": "gpu"},
                ],
                "vram": {"command": False},
            }
        }
    )
    bodies = {
        "http://127.0.0.1:8355/health": {"ok": True, "device": "cuda"},
        "http://192.0.2.10:8358/health": {"status": "healthy", "device": "gpu"},  # its own vocabulary
    }
    path = str(tmp_path / "contour.json")
    status = contour_poll.poll(settings, path, fetch=lambda url, timeout: bodies[url], clock=lambda: _T0)

    assert status["alerts"] == []
    # and it is recorded DISTINCTLY — None is "this document does not say", never False
    assert status["services"]["converter"]["ok"] is None
    assert status["services"]["converter"]["reachable"] is True
    # a non-boolean ok is the same answer: uninterpretable, not a diagnosis
    assert contour_poll.poll(
        settings, path, fetch=lambda url, timeout: {"ok": "yes", "device": "gpu"}, clock=lambda: _T0
    )["alerts"] == []


def test_the_status_file_carries_its_own_freshness_bound(tmp_path):
    # A reader cannot know this poller's cadence, so the bound travels WITH the file — it is what
    # lets the hook tell "the contour is fine" from "nobody has looked since Tuesday".
    settings = contour_poll.resolve_settings({"contour": {"services": ["http://a/health"], "vram": {"command": False}}})
    status = contour_poll.poll(
        settings, str(tmp_path / "c.json"), fetch=lambda url, timeout: {"ok": True}, clock=lambda: _T0
    )
    assert status["max_age"] == contour_poll.DEFAULT_MAX_AGE
    assert status["at"] == _T0.isoformat()
    tuned = contour_poll.resolve_settings({"contour": {"services": ["http://a/health"], "max_age": 60}})
    assert tuned["max_age"] == 60


def test_the_demotion_alert_fires_only_when_a_client_depends_on_the_fast_path():
    demoted = _sample(device="cpu")
    # expect_device set + a different actual device: THE alert of #40
    alerts = contour_poll.evaluate_service(_service(expect_device="gpu"), demoted, {})
    assert [a["kind"] for a in alerts] == ["device-demoted"]
    assert "cpu" in alerts[0]["message"] and "gpu" in alerts[0]["message"]
    # no expectation declared: recorded, never paged — the dependency is the operator's to declare
    assert contour_poll.evaluate_service(_service(), demoted, {}) == []
    # an expectation met is not an alert
    assert contour_poll.evaluate_service(_service(expect_device="cuda"), _sample(device="cuda"), {}) == []
    # a device the service did not report cannot demote
    assert contour_poll.evaluate_service(_service(expect_device="gpu"), _sample(device=None), {}) == []


@pytest.mark.parametrize(
    ("device_reported", "expect_device", "should_alert"),
    [
        # GPU aliases — all should be quiet when expecting "gpu" or another GPU alias
        ("cuda", "gpu", False),
        ("cuda:0", "gpu", False),
        ("cuda:1", "gpu", False),
        ("mps", "gpu", False),
        ("rocm", "gpu", False),
        ("hip", "gpu", False),
        # GPU expectation with real GPU device
        ("cuda", "cuda", False),
        ("mps", "mps", False),
        # CPU aliases — should page when expecting "gpu"
        ("cpu", "gpu", True),
        ("cpu:0", "gpu", True),
        # CPU expectation with CPU device
        ("cpu", "cpu", False),
        ("cpu:0", "cpu", False),
    ],
)
def test_device_aliases_are_resolved_before_comparison(device_reported, expect_device, should_alert):
    """Device strings are aliased so services reporting cuda/mps/rocm/hip match expect_device: "gpu"."""
    alerts = contour_poll.evaluate_service(
        _service(expect_device=expect_device), _sample(device=device_reported), {}
    )
    if should_alert:
        assert [a["kind"] for a in alerts] == ["device-demoted"]
    else:
        assert alerts == []


def test_an_unknown_device_string_compares_verbatim():
    """A service reporting an unknown device type (a typo or new type the table doesn't know)
    compares verbatim against the expectation — so a typo pages, and a genuinely new device
    type pages until the table is updated."""
    # A typo: "cud" instead of "cuda" should page
    alerts = contour_poll.evaluate_service(
        _service(expect_device="cuda"), _sample(device="cud"), {}
    )
    assert [a["kind"] for a in alerts] == ["device-demoted"]
    assert "cud" in alerts[0]["message"] and "cuda" in alerts[0]["message"]


def test_oom_overflows_page_on_the_rise_not_on_the_level():
    service = _service()
    # first sight of a non-zero counter: say it once
    assert [a["kind"] for a in contour_poll.evaluate_service(service, _sample(oom_overflows=2), {})] == ["oom-overflow"]
    # the same counter at the next poll: the condition is the RISE, not the number
    assert contour_poll.evaluate_service(service, _sample(oom_overflows=2), {"oom_overflows": 2}) == []
    # a rise pages again
    alerts = contour_poll.evaluate_service(service, _sample(oom_overflows=3), {"oom_overflows": 2})
    assert [a["kind"] for a in alerts] == ["oom-overflow"]
    # a counter of zero, or none reported, is not an alert
    assert contour_poll.evaluate_service(service, _sample(oom_overflows=0), {}) == []
    assert contour_poll.evaluate_service(service, _sample(oom_overflows=None), {}) == []


def test_a_boolean_oom_overflows_is_a_foreign_vocabulary_not_a_counter(tmp_path):
    """Same class as the ok tri-state: a foreign /health publishing "oom_overflows": true used to
    pass isinstance(int) — bool is an int — and page "rose to 1" about a service speaking a
    different language. bool is excluded at the sample builder exactly as _number excludes it."""
    settings = contour_poll.resolve_settings(
        {
            "contour": {
                "services": [{"name": "converter", "health": "http://192.0.2.10:8358/health"}],
                "vram": {"command": False},
            }
        }
    )
    path = str(tmp_path / "contour.json")
    status = contour_poll.poll(
        settings, path, fetch=lambda url, timeout: {"ok": True, "oom_overflows": True}, clock=lambda: _T0
    )
    assert status["services"]["converter"]["oom_overflows"] is None
    assert status["alerts"] == []


def test_a_counter_that_went_backwards_is_a_restart_and_pages_rather_than_going_quiet():
    """These are per-process counters. A service that died of OOM, came back at 0 and started
    overflowing again read as "3 < 9, no rise" and said NOTHING until it passed its own pre-restart
    high-water mark — the longest possible silence at the exact moment the contour is worst."""
    service = _service()
    alerts = contour_poll.evaluate_service(service, _sample(oom_overflows=3), {"oom_overflows": 9})
    assert [a["kind"] for a in alerts] == ["oom-overflow"]
    assert "after restarting" in alerts[0]["message"] and "9" in alerts[0]["message"]
    # …and it keeps climbing from there, one page per change and none while it holds steady
    assert [a["kind"] for a in contour_poll.evaluate_service(service, _sample(oom_overflows=4), {"oom_overflows": 3})] == [
        "oom-overflow"
    ]
    assert contour_poll.evaluate_service(service, _sample(oom_overflows=4), {"oom_overflows": 4}) == []
    # a restart that has NOT overflowed since is not a page: the condition is overflow, not restart
    assert contour_poll.evaluate_service(service, _sample(oom_overflows=0), {"oom_overflows": 9}) == []


def test_free_vram_below_the_floor_pages():
    assert contour_poll.evaluate_vram(150, 200)[0]["kind"] == "vram-low"
    assert "150" in contour_poll.evaluate_vram(150, 200)[0]["message"]
    assert contour_poll.evaluate_vram(200, 200) == []
    assert contour_poll.evaluate_vram(None, 200) == []  # no GPU here: no VRAM alert, ever


# --- the status file: atomic write, tolerant read ----------------------------------------------------


def test_write_then_read_is_a_round_trip_and_leaves_no_temp_file(tmp_path):
    path = str(tmp_path / "contour.json")
    contour_poll.write_status(path, {"alerts": [], "at": "t"})
    assert contour_poll.read_status(path) == {"alerts": [], "at": "t"}
    assert [p.name for p in tmp_path.iterdir()] == ["contour.json"]


def test_a_replaced_file_never_reads_as_truncated(tmp_path, monkeypatch):
    """The property a mid-turn reader depends on, pinned where it LIVES rather than by its outcome.

    Asserting only that the second write wins proves nothing: a plain ``open(path, "w").write(…)``
    satisfies that too, and a reader landing inside it sees an empty or half-written file accepted
    as valid JSON-that-is-not. So this watches ``os.replace`` — the swap must happen, exactly once,
    from a SIBLING temp file (same directory, therefore same filesystem, therefore atomic), and at
    the instant it happens the destination must still hold the whole PREVIOUS document.
    """
    path = tmp_path / "contour.json"
    old = {"v": 1, "pad": "x" * 200_000}
    contour_poll.write_status(str(path), old)

    swaps = []
    real_replace = os.replace

    def watched(src, dst):
        swaps.append((src, dst, json.loads(Path(dst).read_text(encoding="utf-8"))))
        return real_replace(src, dst)

    monkeypatch.setattr(contour_poll.os, "replace", watched)
    new = {"v": 2, "pad": "y" * 200_000}
    contour_poll.write_status(str(path), new)

    assert len(swaps) == 1, "the new content reached the file without an os.replace — not atomic"
    src, dst, seen_at_swap = swaps[0]
    assert seen_at_swap == old, "a reader could see the destination mid-write"
    assert src != dst and Path(src).parent == path.parent, "the temp file must be a sibling: rename is only atomic within one filesystem"
    assert contour_poll.read_status(str(path)) == new
    assert [p.name for p in tmp_path.iterdir()] == ["contour.json"]


def test_a_failed_write_whose_temp_file_is_also_gone_still_raises_the_real_failure(tmp_path, monkeypatch):
    # The innermost arm: cleanup of a temp file that is not there any more (a tmpreaper, a full
    # /tmp sweep between the two calls). It must not replace the failure it is cleaning up after —
    # a poll that died of a full disk has to report the full disk, not a stray FileNotFoundError.
    def dump_fails(status, fh, **kwargs):
        raise OSError("No space left on device")

    def unlink_fails(path):
        raise FileNotFoundError(path)

    monkeypatch.setattr(contour_poll.json, "dump", dump_fails)
    monkeypatch.setattr(contour_poll.os, "unlink", unlink_fails)
    with pytest.raises(OSError, match="No space left on device"):
        contour_poll.write_status(str(tmp_path / "contour.json"), {"v": 1})


def test_a_write_that_fails_half_way_leaves_no_temp_file_behind(tmp_path):
    # The cleanup arm: a poll every five minutes that littered on each failure would fill the state
    # dir with .contour-*.tmp, and the next read would still be the last good file.
    path = tmp_path / "contour.json"
    contour_poll.write_status(str(path), {"v": 1})
    with pytest.raises(TypeError):
        contour_poll.write_status(str(path), {"v": {1, 2}})  # a set: json.dump raises mid-write
    assert [p.name for p in tmp_path.iterdir()] == ["contour.json"]
    assert contour_poll.read_status(str(path)) == {"v": 1}  # and the last good file is untouched


def test_read_status_tolerates_a_missing_or_corrupt_file_but_never_silently(tmp_path, capsys):
    # Absent is the first run and says nothing at all; anything else costs this poll its baseline,
    # which is cheap but not free — and it used to happen with no line anywhere, so a file being
    # corrupted every single poll was indistinguishable from a healthy contour.
    assert contour_poll.read_status(str(tmp_path / "absent.json")) == {}
    assert capsys.readouterr().err == ""

    bad = tmp_path / "contour.json"
    bad.write_text("{not json", encoding="utf-8")
    assert contour_poll.read_status(str(bad)) == {}
    assert "not readable JSON" in capsys.readouterr().err

    bad.write_text("[1, 2]", encoding="utf-8")
    assert contour_poll.read_status(str(bad)) == {}
    assert "does not hold a JSON object" in capsys.readouterr().err

    unreadable = tmp_path / "adir"
    unreadable.mkdir()  # a directory where the file belongs: OSError, not ValueError
    assert contour_poll.read_status(str(unreadable)) == {}
    assert "could not be read" in capsys.readouterr().err


# --- poll: one full pass ------------------------------------------------------------------------------


def _poll_settings(**contour):
    config = {"contour": contour} if contour else {}
    return contour_poll.resolve_settings(config)


def test_one_poll_writes_availability_and_the_alerts_and_lands_on_disk(tmp_path):
    settings = _poll_settings(
        services=[
            {"name": "whisper", "health": "http://a/health", "expect_device": "cuda"},
            {"name": "rvc", "health": "http://b/health", "expect_device": "gpu"},
        ],
        vram={"command": False, "min_free_mib": 200},
    )

    bodies = {
        "http://a/health": {"ok": True, "device": "cuda"},
        "http://b/health": {"ok": True, "device": "cpu", "oom_overflows": 2},
    }

    def fetch(url, timeout):
        return bodies[url]

    path = str(tmp_path / "contour.json")
    status = contour_poll.poll(settings, path, fetch=fetch, clock=lambda: _T0)

    assert [a["key"] for a in status["alerts"]] == ["device-demoted:rvc", "oom-overflow:rvc"]
    assert status["services"]["whisper"]["reachable"] is True
    # and it is on disk, atomically, for the hook to read
    assert contour_poll.read_status(path)["alerts"] == status["alerts"]

    # a second poll over the same card: the oom counter held, so its alert does NOT refire —
    # the demotion (a persisting condition) still does
    second = contour_poll.poll(settings, path, fetch=fetch, clock=lambda: _T0)
    assert [a["key"] for a in second["alerts"]] == ["device-demoted:rvc"]


def test_the_status_file_is_the_alerts_and_the_last_sample_and_nothing_accumulated(tmp_path):
    """#40's file is a PAGE, not a dashboard, and the hook re-reads it on every tool call. Three
    services at the old 2016-sample window was 967 KB of history that no rule read and no SLO was
    written against, re-parsed each time to reach a key that is almost always empty — so the
    window is gone and this pins the shape that replaced it."""
    settings = _poll_settings(services=["http://a/health", "http://b/health"], vram={"command": False})
    path = str(tmp_path / "contour.json")
    for _ in range(3):
        status = contour_poll.poll(settings, path, fetch=lambda url, timeout: {"ok": True, "device": "cuda"}, clock=lambda: _T0)
    assert set(status) == {"at", "max_age", "alerts", "services", "vram"}
    assert set(status["services"]) == {"a", "b"}
    assert set(status["services"]["a"]) == {"reachable", "ok", "device", "oom_overflows", "detail"}
    # three polls, two services, and the file does not grow: it is a snapshot, not a window
    assert len(Path(path).read_bytes()) < 1000


def test_a_poll_is_bounded_by_one_timeout_per_service_plus_one_for_the_card(tmp_path):
    """The bound the module docstring states, pinned: (N + 1) x contour.timeout, serially, with no
    other wait in the path. Both waits are counted where they are SPENT, so a second request per
    service, or one that forgot to pass the timeout down, fails here rather than in production at
    the end of somebody's turn."""
    spent: list[float] = []

    def slow_fetch(url, timeout):
        spent.append(timeout)
        raise contour_poll.FetchError("timed out")

    def slow_runner(argv, timeout):
        spent.append(timeout)
        raise subprocess.TimeoutExpired(argv, timeout)

    settings = _poll_settings(
        services=["http://a/health", "http://b/health", "http://c/health"], timeout=4, vram={"command": "nvidia-smi"}
    )
    contour_poll.poll(settings, str(tmp_path / "c.json"), fetch=slow_fetch, runner=slow_runner, clock=lambda: _T0)

    assert spent == [4.0, 4.0, 4.0, 4.0]  # three services, then the card — each bounded, none free
    assert sum(spent) <= (len(settings["services"]) + 1) * settings["timeout"]


def test_an_unreachable_service_travels_all_the_way_through_a_real_poll(tmp_path):
    # The FetchError arm was covered at the seams and never through poll() itself — the branch that
    # turns a dead service into the sample the alert rules read.
    settings = _poll_settings(services=[{"name": "rvc", "health": "http://a/health", "expect_device": "gpu"}], vram={"command": False})

    def refused(url, timeout):
        raise contour_poll.FetchError("URLError")

    path = str(tmp_path / "contour.json")
    status = contour_poll.poll(settings, path, fetch=refused, clock=lambda: _T0)
    assert [a["key"] for a in status["alerts"]] == ["unreachable:rvc"]
    assert status["services"]["rvc"] == {
        "reachable": False,
        "ok": None,
        "device": None,
        "oom_overflows": None,
        "detail": "URLError",
    }
    # a service that did not answer cannot ALSO be demoted: nothing below reachability is known
    assert contour_poll.read_status(path)["alerts"] == status["alerts"]


def test_a_corrupt_previous_status_costs_one_cycles_deltas_and_says_which(tmp_path, capsys):
    """The docstring's claim, made true and then pinned. The delta that is lost is the oom baseline:
    a counter holding steady at 9 reads as first sight, so it pages once — and the poll says on
    stderr that it started from nothing, instead of resetting in silence."""
    path = str(tmp_path / "contour.json")
    Path(path).write_text("{corrupt", encoding="utf-8")
    settings = _poll_settings(services=["http://a/health"], vram={"command": False})

    def steady(url, timeout):
        return {"ok": True, "device": "cuda", "oom_overflows": 9}

    status = contour_poll.poll(settings, path, fetch=steady, clock=lambda: _T0)
    assert [a["kind"] for a in status["alerts"]] == ["oom-overflow"]  # the delta: LOST, for one cycle
    assert "not readable JSON" in capsys.readouterr().err  # …and not lost quietly

    # the next poll reads the file this one wrote, has its baseline back, and holds its peace
    again = contour_poll.poll(settings, path, fetch=steady, clock=lambda: _T0)
    assert again["alerts"] == []
    assert capsys.readouterr().err == ""


def test_a_poll_that_cannot_write_its_file_still_carries_its_diagnosis(tmp_path, monkeypatch):
    """An unwritable status file plus an ACTIVE alert. 64 there swallowed the page whole: the file
    the hook reads was not refreshed AND the exit code said "called wrong", so a caller branching
    on 1 never paged for as long as the disk stayed full. The poll ran; the diagnosis outranks the
    broken monitor; the exit code is the only channel left, so it carries it."""
    settings = _poll_settings(
        services=[{"name": "rvc", "health": "http://a/health", "expect_device": "gpu"}], vram={"command": False}
    )

    def full_disk(path, status):
        raise OSError("No space left on device")

    monkeypatch.setattr(contour_poll, "write_status", full_disk)
    with pytest.raises(contour_poll.StatusWriteError) as raised:
        contour_poll.poll(settings, str(tmp_path / "c.json"), fetch=lambda url, timeout: {"ok": True, "device": "cpu"}, clock=lambda: _T0)
    assert "No space left on device" in str(raised.value)
    assert [a["key"] for a in raised.value.status["alerts"]] == ["device-demoted:rvc"]


def test_vram_sampling_uses_the_runner_seam_and_pages_below_the_floor(tmp_path):
    settings = _poll_settings(services=["http://a/health"], vram={"min_free_mib": 200})
    status = contour_poll.poll(
        settings,
        str(tmp_path / "contour.json"),
        fetch=lambda url, timeout: {"ok": True, "device": "cuda"},
        runner=lambda argv, timeout: _Done("150\n"),
    )
    assert [a["kind"] for a in status["alerts"]] == ["vram-low"]
    assert status["vram"] == {"free_mib": 150, "min_free_mib": 200}


# --- main: the exit-code contract ----------------------------------------------------------------------


def _config_file(tmp_path, monkeypatch, config) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setenv("VOICE_LOOP_CONFIG", str(path))


def test_main_exit_0_when_quiet_1_when_paging_and_the_file_is_written(tmp_path, monkeypatch, capsys):
    _config_file(tmp_path, monkeypatch, {"contour": {"services": ["http://a/health"], "vram": {"command": False}}})
    monkeypatch.setattr(contour_poll, "fetch_health", lambda url, timeout: {"ok": True, "device": "cuda"})
    status_path = str(tmp_path / "contour.json")

    assert contour_poll.main(["--status", status_path]) == 0
    assert "contour ok — 1/1 services answered" in capsys.readouterr().out
    assert contour_poll.read_status(status_path)["services"]

    def demoted(url, timeout):
        return {"ok": True, "device": "cpu"}

    _config_file(
        tmp_path,
        monkeypatch,
        {"contour": {"services": [{"health": "http://a/health", "expect_device": "gpu"}], "vram": {"command": False}}},
    )
    monkeypatch.setattr(contour_poll, "fetch_health", demoted)
    assert contour_poll.main(["--status", status_path]) == 1
    assert "ALERT" in capsys.readouterr().out


def test_the_success_line_names_what_was_actually_polled(tmp_path, monkeypatch, capsys):
    """"contour ok" has to be a claim about the operator's contour, not about whatever this poller
    happened to fall back to — so the summary says when it is watching only the shipped default,
    and says when a service answered without publishing an ``ok`` it could read."""
    _config_file(tmp_path, monkeypatch, {})
    monkeypatch.setattr(contour_poll, "fetch_health", lambda url, timeout: {"status": "healthy"})
    monkeypatch.setattr(contour_poll, "sample_vram", lambda command, timeout, runner: None)
    assert contour_poll.main(["--status", str(tmp_path / "c.json")]) == 0
    out = capsys.readouterr().out
    assert "contour.services unset" in out and "1 publishing no ok field" in out


def test_main_json_prints_the_whole_status(tmp_path, monkeypatch, capsys):
    _config_file(tmp_path, monkeypatch, {"contour": {"services": ["http://a/health"], "vram": {"command": False}}})
    monkeypatch.setattr(contour_poll, "fetch_health", lambda url, timeout: {"ok": True, "device": "cuda"})
    assert contour_poll.main(["--json", "--status", str(tmp_path / "contour.json")]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["services"]["a"]["ok"] is True


def test_main_64_is_for_calling_it_wrong_never_a_diagnosis(tmp_path, monkeypatch, capsys):
    _config_file(tmp_path, monkeypatch, {})
    assert contour_poll.main(["--bogus"]) == 64
    assert contour_poll.main(["--status"]) == 64
    # a config that names services none of which is usable: a config error, not "contour ok"
    _config_file(tmp_path, monkeypatch, {"contour": {"services": ["not-a-url"]}})
    assert contour_poll.main(["--status", str(tmp_path / "c.json")]) == 64
    assert "no fetchable" in capsys.readouterr().err
    # …and an explicitly EMPTY list is a contour nobody watches, which is not "contour ok" either
    _config_file(tmp_path, monkeypatch, {"contour": {"services": []}})
    assert contour_poll.main(["--status", str(tmp_path / "c.json")]) == 64
    assert "no usable service" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("what", "config", "expected"),
    [
        ("a mistyped timeout", {"contour": {"timeout": "5s"}}, "contour.timeout"),
        ("a quoted max_age", {"contour": {"max_age": "900"}}, "contour.max_age"),
        ("a mistyped vram floor", {"contour": {"vram": {"min_free_mib": "200 MiB"}}}, "contour.vram.min_free_mib"),
        ("a services value that is not a list", {"contour": {"services": {"a": "http://a/h"}}}, "must be a list"),
        ("a host urllib cannot encode", {"contour": {"services": ["http://a..b/health"]}}, "no fetchable"),
        (
            "two entries with one name",
            {"contour": {"services": ["http://a:1/health", "http://a:1/healthz"]}},
            "two entries named",
        ),
    ],
)
def test_a_config_error_exits_64_never_1_and_still_writes_the_status_file(
    what, config, expected, tmp_path, monkeypatch, capsys
):
    """1 MEANS PAGE, and nothing else. Every one of these used to raise out of resolve_settings,
    hit Python's own handler, and exit 1 — a scheduler branching on the exit code was told an
    alert was active by a typo. And no status file was written, so the hook went on voicing (or
    not voicing) whatever the last successful poll had left behind."""
    status_path = str(tmp_path / "contour.json")
    contour_poll.write_status(
        status_path,
        {
            "at": _T0.isoformat(),
            "max_age": 900,
            "alerts": [],
            "services": {"a": {"reachable": True, "ok": True, "device": "cuda", "oom_overflows": None, "detail": ""}},
        },
    )

    _config_file(tmp_path, monkeypatch, config)
    assert contour_poll.main(["--status", status_path]) == 64, what
    assert expected in capsys.readouterr().err

    # the status file is refreshed, not left stale: it now carries the poller's own failure, which
    # is what the hook voices instead of reading an "all quiet" nobody has re-checked
    written = contour_poll.read_status(status_path)
    assert [a["key"] for a in written["alerts"]] == ["poller-error"]
    assert written["at"] != _T0.isoformat()
    # …and the last true thing it knew survives the failure: the samples are not wiped by it
    assert written["services"] == {"a": {"reachable": True, "ok": True, "device": "cuda", "oom_overflows": None, "detail": ""}}


def test_a_config_that_is_not_readable_json_is_64_not_contour_ok_over_loopback(tmp_path, monkeypatch, capsys):
    """A trailing comma used to be swallowed whole: load_config returned {}, the services fell
    back to DEFAULT_SERVICES, and the poller printed 'contour ok — 1/1 services answered' while
    none of the operator's three services had been looked at."""
    path = tmp_path / "config.json"
    path.write_text('{"contour": {"services": ["http://a/health",]}}', encoding="utf-8")
    monkeypatch.setenv("VOICE_LOOP_CONFIG", str(path))

    def never(url, timeout):
        raise AssertionError("a config that will not parse must not be polled against defaults")

    monkeypatch.setattr(contour_poll, "fetch_health", never)
    status_path = str(tmp_path / "contour.json")
    assert contour_poll.main(["--status", status_path]) == 64
    out = capsys.readouterr()
    assert "not valid JSON" in out.err and "contour ok" not in out.out
    assert [a["key"] for a in contour_poll.read_status(status_path)["alerts"]] == ["poller-error"]

    # a config file that is not there at all is still the documented default install, not an error
    monkeypatch.setenv("VOICE_LOOP_CONFIG", str(tmp_path / "absent.json"))
    assert contour_poll.load_config(str(tmp_path / "absent.json")) == {}
    # …and one that holds something other than an object is a typo somebody has to be told about
    path.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(contour_poll.ConfigError, match="must hold a JSON object"):
        contour_poll.load_config(str(path))


def test_an_unreadable_config_file_is_64_not_a_default(tmp_path, monkeypatch):
    unreadable = tmp_path / "config.json"
    unreadable.mkdir()  # a directory where a file belongs: open() raises OSError, not ValueError
    with pytest.raises(contour_poll.ConfigError, match="cannot read"):
        contour_poll.load_config(str(unreadable))


def test_the_status_path_is_one_knob_both_halves_read(tmp_path, monkeypatch, capsys):
    """The seam that used to be one-sided: the poller could be pointed anywhere with --status while
    the hook only ever read the default, so a cron line written with `--status /var/tmp/contour.json`
    polled correctly, exited 1 correctly, and paged NOBODY. contour.status_path is the knob both
    halves resolve; --status stays an override for a probe."""
    relocated = tmp_path / "elsewhere" / "contour.json"
    relocated.parent.mkdir()
    _config_file(
        tmp_path,
        monkeypatch,
        {"contour": {"services": ["http://a/health"], "vram": {"command": False}, "status_path": str(relocated)}},
    )
    monkeypatch.setattr(contour_poll, "fetch_health", lambda url, timeout: {"ok": True, "device": "cuda"})

    # no --status at all: the SCHEDULED shape, and it lands where the config says
    assert contour_poll.main([]) == 0
    assert contour_poll.read_status(str(relocated))["services"]
    assert not (tmp_path / "contour.json").exists()
    capsys.readouterr()

    # --status still wins, for the one-off probe it is for
    probe = str(tmp_path / "probe.json")
    assert contour_poll.main(["--status", probe]) == 0
    assert contour_poll.read_status(probe)["services"]

    # and the hook resolves the same key the same way — the other half of the seam
    speak_path = _POLL_PATH.parent / "speak.py"
    spec = importlib.util.spec_from_file_location("speak_for_contour", speak_path)
    speak = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(speak)
    config = {"contour": {"status_path": str(relocated)}}
    assert speak.contour_status_path(config) == str(relocated)
    assert speak.contour_status_path({}) == speak._CONTOUR_PATH
    assert contour_poll.resolve_status_path(config) == speak.contour_status_path(config)


def test_a_status_file_that_cannot_be_written_never_downgrades_a_live_page(tmp_path, monkeypatch, capsys):
    """1 MEANS PAGE — including when the file the page normally travels in could not be written.
    The old path exited 64 there, so an unwritable state dir left the hook reading a stale "all
    quiet" AND told the scheduler it had been called wrong. The alerts still reach stdout."""
    _config_file(
        tmp_path,
        monkeypatch,
        {"contour": {"services": [{"health": "http://a/health", "expect_device": "gpu"}], "vram": {"command": False}}},
    )
    monkeypatch.setattr(contour_poll, "fetch_health", lambda url, timeout: {"ok": True, "device": "cpu"})

    def full_disk(path, status):
        raise OSError("No space left on device")

    monkeypatch.setattr(contour_poll, "write_status", full_disk)
    assert contour_poll.main(["--status", str(tmp_path / "c.json")]) == 1
    out = capsys.readouterr()
    assert "ALERT a is serving on cpu, expected gpu" in out.out
    assert "could not be written" in out.err

    # a QUIET poll that cannot write is a broken monitor and nothing else: 64, not a page
    monkeypatch.setattr(contour_poll, "fetch_health", lambda url, timeout: {"ok": True, "device": "gpu"})
    assert contour_poll.main(["--status", str(tmp_path / "c.json")]) == 64
    assert "contour ok" in capsys.readouterr().out


def test_a_poll_that_blows_up_internally_exits_64_not_the_page_code(tmp_path, monkeypatch, capsys):
    """The vocabulary is CLOSED: 1 is reachable from exactly one line, the alert count. Anything
    else — including a bug in this file — leaves as 64 with the failure in the status file."""
    _config_file(tmp_path, monkeypatch, {"contour": {"services": ["http://a/health"], "vram": {"command": False}}})

    def broken(settings, status_path, **seams):
        raise RuntimeError("the poll itself is broken")

    monkeypatch.setattr(contour_poll, "poll", broken)
    status_path = str(tmp_path / "contour.json")
    assert contour_poll.main(["--status", status_path]) == 64
    assert "RuntimeError" in capsys.readouterr().err
    assert [a["key"] for a in contour_poll.read_status(status_path)["alerts"]] == ["poller-error"]


def test_a_state_dir_that_will_not_write_costs_the_page_not_a_traceback(tmp_path, monkeypatch):
    # record_poller_error is the last thing standing between a broken poller and silence; when it
    # cannot write either, it must not raise on the way out of an already-failing run.
    def unwritable(path, exist_ok=False):
        raise OSError("read-only state dir")

    monkeypatch.setattr(contour_poll.os, "makedirs", unwritable)
    contour_poll.record_poller_error(str(tmp_path / "nowhere" / "contour.json"), "boom")  # no raise


def test_main_help_is_free_and_exit_0_and_lists_every_flag_it_accepts(monkeypatch, capsys):
    """"the program name appears" was satisfied by usage text that listed no flag at all. What
    --help is FOR is the flags, so those are the assertion — and "free" is asserted too: it polls
    nothing, reads no config, and writes no status file."""

    def never(*args, **kwargs):
        raise AssertionError("--help polls nothing and reads nothing")

    monkeypatch.setattr(contour_poll, "fetch_health", never)
    monkeypatch.setattr(contour_poll, "load_config", never)
    monkeypatch.setattr(contour_poll, "write_status", never)

    for flag in ("--help", "-h"):
        assert contour_poll.main([flag]) == 0
        out = capsys.readouterr().out
        assert "contour-poll" in out
        for documented in ("--json", "--status", "--help"):
            assert documented in out, f"{documented} is accepted but {flag} does not list it"
