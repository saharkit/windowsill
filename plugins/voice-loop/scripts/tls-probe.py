#!/usr/bin/env python3
"""voice-loop — the TLS probe: can THIS interpreter verify an HTTPS certificate?

Why it exists. A Mac running python.org-installer Python ships a bundled OpenSSL with an
**empty certificate store** — it is populated only when the user runs
``/Applications/Python 3.x/Install Certificates.command``, which the installer drops beside the
app and nothing runs for them. Until it is run, *every* https call from that interpreter dies with
``SSL: CERTIFICATE_VERIFY_FAILED — unable to get local issuer certificate``: the cloud TTS request,
the model download ``torch.hub`` does, ``pip install`` itself. The error is loud and names no fix,
which is how a working setup reads as a broken plugin (QA, 2026-08-02).

So ``/voice-setup`` probes for it before it installs anything, and this script is that probe: one
https request, three outcomes, and a failure message that names the exact command that repairs it —
with ``--fix``, having asked the user first, it runs that command and re-probes, so a cert-less
install goes red to green inside setup.

Stdlib only, Python 3.10+, no network state and nothing written to disk. ``tls-probe.sh`` is the
thin entry point, exactly as ``speak.sh`` is for ``speak.py``.

Usage:
  tls-probe.sh [--url URL] [--timeout SECONDS] [--fix] [--json]

  --url      what to probe; default is derived from the config (an https endpoint it will really
             talk to) and falls back to the host `pip` itself needs.
  --fix      on a certificate failure, RUN the repair when it is a runnable one (macOS
             Install Certificates.command) and probe again. Consent belongs to the caller: setup
             asks before passing this.
  --json     one JSON object on stdout instead of prose, for a caller that wants to branch on it.

Exit codes: 0 verified (or repaired by --fix) · 1 certificate verification failed · 2 the host was
not reachable at all, so TLS is neither proven nor disproven · 64 this script was called wrong
(unknown flag, missing value, a non-https URL) — a caller that branches on "1 is the trap" must
never be handed a typo instead.
"""

from __future__ import annotations

import json
import os
import platform
import re
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

# The one module in scripts/ this file imports: the provider registry (see providers.py). Which
# host is worth probing is a per-provider fact — "does this provider have a remote default host?"
# — and it belongs in the entry, not in a special case here.
import providers

# The fallback probe target: a host with a perfectly ordinary public certificate that this install
# genuinely depends on — it is where `pip install -r requirements.txt` gets the server's wheels
# from. Probing it therefore predicts the next step of setup rather than testing an unrelated host
# — for everything that does not go through a proxy, which this probe deliberately bypasses and
# says so on the way past (see PROXY_VARS).
DEFAULT_URL = "https://pypi.org/"

# A probe must never be the slow part of setup: a handshake that has not happened in this long is
# an unreachable host, which is a different answer from a rejected certificate.
DEFAULT_TIMEOUT = 10.0

# Repairing the store is a pip install plus a symlink, over the network, on a slow morning.
FIX_TIMEOUT = 300.0

# The python.org installer's own repair script, dropped beside the app it installs. The version in
# the path is the framework version, not the full X.Y.Z.
INSTALL_CERTIFICATES = "/Applications/Python {version}/Install Certificates.command"

_FRAMEWORK = re.compile(r"/Library/Frameworks/Python\.framework/Versions/(\d+\.\d+)")

# The substring OpenSSL puts in the message for the trap this script exists to name. Matching on it
# is the last resort — the typed exception is checked first — but it survives an exception this
# python version happens to wrap differently.
CERT_MARKER = "CERTIFICATE_VERIFY_FAILED"

# A proxy carries its own certificate, so this probe bypasses proxies to ask about THIS
# interpreter's store (see _default_prober). But `pip` and the model download do honour them, so a
# green here does not predict them — when one of these is set the OK message says so rather than
# letting a bypassed proxy read as "everything verifies".
PROXY_VARS = ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy")


# --- config (standalone by design: each script in scripts/ carries its own reader, so a single
# --- file can be copied out and still run — keep the semantics in sync with speak.py) ------------


def load_config(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            loaded = json.load(fh)
        return loaded if isinstance(loaded, dict) else {}
    except (OSError, ValueError):
        return {}


def cfg(config: dict, dotted: str, default):
    """Walk a dotted path; absent, null and empty-string values all fall back."""
    node = config
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    if node is None or node == "":
        return default
    return node


def config_path(environ=os.environ) -> str:
    return environ.get(
        "VOICE_LOOP_CONFIG",
        os.path.join(
            environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
            "voice-loop",
            "config.json",
        ),
    )


def resolve_url(config: dict) -> str:
    """The https host worth probing: whatever THIS install will actually talk to.

    A local or LAN endpoint is plain http and carries no certificate at all, so it is skipped
    rather than reported as fine — what still has to verify on that path is the model download and
    ``pip install``, which is why the fallback is the host pip itself uses.
    """
    for dotted in ("tts.endpoint", "stt.endpoint"):
        value = str(cfg(config, dotted, ""))
        if value.startswith("https://"):
            return value.rstrip("/")
    # Some providers have a remote DEFAULT host — speak.py falls back to it when no endpoint is
    # configured — and some do not: the OpenAI-compatible path falls back to the LOCAL server on
    # http, so a configured https OpenAI endpoint is already caught by the loop above. Which is
    # which is the entry's ``default_host``, never a name compared here; an unknown provider name
    # has no host to offer, and the pip fallback below is the honest answer for it.
    backend = str(cfg(config, "tts.backend", ""))
    entry = providers.tts_provider(str(cfg(config, "tts.cloud.provider", providers.DEFAULT_TTS)))
    if backend == "cloud" and entry is not None and entry.default_host:
        return entry.default_host
    return DEFAULT_URL


def configured_proxy(environ=os.environ) -> str:
    """The name of the proxy variable in force, or "" — see PROXY_VARS."""
    for name in PROXY_VARS:
        if environ.get(name):
            return name
    return ""


def display_url(url: str) -> str:
    """The URL as it is safe to PRINT. A configured endpoint may carry credentials in its userinfo
    or an API key in its query string, and this message lands in an agent transcript and in the
    issue someone pastes it into — so neither is ever rendered back."""
    split = urllib.parse.urlsplit(url)
    if not split.netloc:  # not something we can take apart; leave it exactly as given
        return url
    return urllib.parse.urlunsplit((split.scheme, split.netloc.rsplit("@", 1)[-1], split.path, "", ""))


# --- the probe ----------------------------------------------------------------------------------


def is_cert_failure(err: BaseException) -> bool:
    """True for "the certificate did not verify", through urllib's wrapping.

    ``urllib`` hands back ``URLError(reason=SSLCertVerificationError(...))``, and the nesting has
    not always been one level deep, so the chain of ``reason`` attributes is unwrapped before the
    message itself is consulted. The message fallback is narrowed to ``OSError`` (which
    ``URLError`` and every ssl error already are): text that merely CONTAINS the marker — a proxy's
    error body, a wrapped tunnel failure — must not be answered with "run the installer"."""
    seen: BaseException = err
    for _ in range(4):
        if isinstance(seen, ssl.SSLCertVerificationError):
            return True
        reason = getattr(seen, "reason", None)
        if not isinstance(reason, BaseException):
            break
        seen = reason
    return isinstance(err, OSError) and CERT_MARKER in str(err)


def _default_prober(url: str, timeout: float) -> None:
    """One https request, proxies bypassed — the same path speak.py's synthesis call takes (an
    intercepting proxy has its own certificate, and diagnosing the interpreter's store means not
    going through it). Returns on success; raises whatever urllib raised otherwise."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(urllib.request.Request(url, method="GET"), timeout=timeout) as resp:
        resp.read(1)


def probe(url: str, timeout: float = DEFAULT_TIMEOUT, prober=_default_prober) -> tuple[str, str]:
    """``(result, detail)`` where result is ``ok`` / ``certificate`` / ``unreachable``.

    An HTTP error status is a SUCCESS here: 401 from a cloud API and 404 from a bare endpoint both
    mean the handshake completed and the certificate verified, which is the only question asked.
    """
    try:
        prober(url, timeout)
    except urllib.error.HTTPError as err:
        return "ok", f"HTTP {err.code} (the certificate verified; the status is not our business)"
    except Exception as err:  # noqa: BLE001 — every failure is classified below, none may escape
        if is_cert_failure(err):
            return "certificate", str(getattr(err, "reason", err))
        return "unreachable", str(getattr(err, "reason", err))
    return "ok", "verified"


# --- the fix ------------------------------------------------------------------------------------


def framework_version(base_prefix: str) -> str | None:
    """The X.Y of a python.org framework install, or None for any other layout (Homebrew, pyenv,
    a distro python) — this is what "detect the python.org layout" comes down to."""
    match = _FRAMEWORK.search(base_prefix)
    return match.group(1) if match else None


def remedy(
    *,
    system: str = "",
    base_prefix: str = "",
    executable: str = "",
    environ=os.environ,
    exists=os.path.exists,
) -> dict:
    """What to actually do about a certificate failure on THIS machine.

    ``runnable`` is the whole point of the distinction: only the installer's own
    ``Install Certificates.command`` is something this script may run for the user (one argv, no
    shell, no sudo). Everything else is printed — a shell pipeline or a package manager is the
    user's call, and the message still names it exactly."""
    system = system or platform.system()
    base_prefix = base_prefix or sys.base_prefix
    executable = executable or sys.executable
    override = [name for name in ("SSL_CERT_FILE", "SSL_CERT_DIR") if environ.get(name)]
    if override:
        # An empty or stale override beats every store below it, so it is the diagnosis whenever it
        # is set — repairing the interpreter's own store would change nothing while it stands.
        return {
            "kind": "env-override",
            "runnable": False,
            "command": f"unset {' '.join(override)}",
            "why": f"{' and '.join(override)} {'are' if len(override) > 1 else 'is'} set and "
            "override this interpreter's own certificate store",
        }
    version = framework_version(base_prefix)
    if version is not None:
        installer = INSTALL_CERTIFICATES.format(version=version)
        if exists(installer):
            return {
                "kind": "install-certificates-command",
                "runnable": True,
                "command": installer,
                "why": "this is python.org-installer Python, whose certificate store is empty until "
                "its own Install Certificates.command has been run once",
            }
        # Same layout, installer gone (a trimmed image, a moved /Applications). The command below is
        # what that script does: install certifi, then point OpenSSL's cafile at it.
        return {
            "kind": "certifi-relink",
            "runnable": False,
            "command": (
                f'"{executable}" -m pip install --upgrade certifi && '
                f'ln -sf "$("{executable}" -m certifi)" '
                f"\"$(\"{executable}\" -c 'import ssl; print(ssl.get_default_verify_paths().openssl_cafile)')\""
            ),
            "why": "this is python.org-installer Python, but its Install Certificates.command is "
            f"not at {INSTALL_CERTIFICATES.format(version=version)}",
        }
    if system == "Darwin":
        return {
            "kind": "homebrew-certifi",
            "runnable": False,
            "command": f'"{executable}" -m pip install --upgrade certifi',
            "why": "this is not python.org-installer Python, so the empty-store trap does not apply "
            "— an intercepting proxy's own CA is the other common cause on macOS",
        }
    return {
        "kind": "system-trust-store",
        "runnable": False,
        "command": "sudo apt install ca-certificates && sudo update-ca-certificates   # (or dnf/pacman)",
        "why": "the system trust store is missing or stale (or a proxy is intercepting TLS with a "
        "CA this machine does not trust)",
    }


def _default_runner(argv: list[str], timeout: float):
    """Bounded, argv-only spawn: never a shell string (the installer's path contains a space and is
    passed as one argument), always a wall-clock timeout, and the exit code is the caller's to
    interpret rather than an exception."""
    return subprocess.run(argv, capture_output=True, check=False, timeout=timeout)


def apply_fix(command: str, runner=_default_runner, timeout: float = FIX_TIMEOUT) -> tuple[bool, str]:
    """Run a runnable remedy. ``(ok, detail)`` — a non-zero exit, a timeout and an unspawnable
    command are all ordinary results here, never an exception out of the probe."""
    try:
        completed = runner([command], timeout)
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout:.0f}s"
    except OSError as err:
        return False, f"{type(err).__name__}: {err}"
    if completed.returncode != 0:
        tail = (completed.stderr or b"")[-300:]
        return False, f"exit {completed.returncode}: {tail.decode('utf-8', 'replace').strip()}"
    return True, "the command itself exited 0"


# --- reporting ----------------------------------------------------------------------------------


def render(report: dict) -> str:
    """The human message. On a certificate failure it NAMES THE FIX — that requirement is the
    reason this script exists, and the shape of the message is the acceptance criterion."""
    lines = [f"tls-probe: {report['url']}  (python: {report['python']})"]
    if report["result"] == "ok":
        lines.append(f"OK: certificates verify from this interpreter — {report['detail']}")
        if report.get("fixed"):
            lines.insert(1, f"    repaired by: {report['fix']['command']}")
        if report.get("proxy"):
            # The one thing this green does NOT cover: `pip` and the model download go through the
            # proxy this probe stepped around, and its CA has to be trusted separately.
            lines.append(
                f"    Note: {report['proxy']} is set and this probe bypassed it — pip and the model "
                "download will not, so a proxy with an untrusted CA can still fail from here."
            )
        return "\n".join(lines)
    if report["result"] == "unreachable":
        lines.append(f"UNKNOWN: {report['url']} could not be reached — {report['detail']}")
        lines.append("         TLS is neither proven nor disproven; check the network and re-run.")
        return "\n".join(lines)
    fix = report["fix"]
    lines.append(f"FAIL: certificate verification failed — {report['detail']}")
    lines.append(f"      Why: {fix['why']}.")
    lines.append("      Fix, exactly:")
    lines.append(f"          {fix['command']}")
    if fix["runnable"]:
        lines.append("      Or re-run this probe with --fix and it will run that for you.")
    if report.get("fix_detail"):
        # --fix ran and the probe is STILL red: say which half failed — the command, or the repair
        # it was supposed to be — rather than leaving the user to re-run it hopefully.
        lines.append(f"      (--fix already tried this: {report['fix_detail']})")
    lines.append(f"      Store in use: cafile={report['cafile']} capath={report['capath']}")
    return "\n".join(lines)


def run(url: str, timeout: float, do_fix: bool, prober=_default_prober, runner=_default_runner) -> dict:
    """Probe, optionally repair, and — having repaired — probe AGAIN. The second probe is what
    turns "we ran something" into "it is green now", which is the only claim worth making."""
    paths = ssl.get_default_verify_paths()
    report: dict = {
        "url": display_url(url),  # printed and JSON-serialised: never the credentials it may carry
        "python": sys.executable,
        "cafile": paths.cafile,
        "capath": paths.capath,
        "proxy": configured_proxy(),
        "fixed": False,
    }
    result, detail = probe(url, timeout, prober)
    report["result"], report["detail"] = result, detail
    if result != "certificate":
        report["fix"] = None
        return report
    report["fix"] = remedy()
    if do_fix and report["fix"]["runnable"]:
        ok, fix_detail = apply_fix(report["fix"]["command"], runner)
        report["fix_detail"] = fix_detail
        if ok:
            result, detail = probe(url, timeout, prober)
            report["result"], report["detail"] = result, detail
            report["fixed"] = result == "ok"
    return report


EXIT_CODES = {"ok": 0, "certificate": 1, "unreachable": 2}

# A mis-invocation is NOT a diagnosis. /voice-setup is told "exit 1 is the trap, run what it names",
# so a typo'd flag must never come back as 1 — that is how an agent ends up offering to run an
# installer for a problem that is a missing argument. 64 is sysexits' EX_USAGE.
EXIT_USAGE = 64


def main(argv: list[str] | None = None, prober=_default_prober, runner=_default_runner) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    url = ""
    timeout = DEFAULT_TIMEOUT
    do_fix = False
    as_json = False
    while argv:
        arg = argv.pop(0)
        if arg in ("--url", "--timeout") and not argv:
            # a known flag left dangling is a missing VALUE, not an unknown flag — say which
            print(f"{arg} wants a value after it", file=sys.stderr)
            return EXIT_USAGE
        if arg == "--url":
            url = argv.pop(0)
        elif arg == "--timeout":
            try:
                timeout = float(argv.pop(0))
            except ValueError:
                print("--timeout wants a number of seconds", file=sys.stderr)
                return EXIT_USAGE
        elif arg == "--fix":
            do_fix = True
        elif arg == "--json":
            as_json = True
        elif arg in ("-h", "--help"):
            print(__doc__.strip())
            return 0
        else:
            print(f"unknown argument: {arg}", file=sys.stderr)
            return EXIT_USAGE

    if not url:
        url = resolve_url(load_config(config_path()))
    if not url.startswith("https://"):
        print(f"{display_url(url)} is not an https URL — there is no certificate to verify", file=sys.stderr)
        return EXIT_USAGE

    report = run(url, timeout, do_fix, prober, runner)
    print(json.dumps(report, ensure_ascii=False) if as_json else render(report))
    return EXIT_CODES[report["result"]]


if __name__ == "__main__":
    sys.exit(main())
