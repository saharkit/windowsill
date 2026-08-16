#!/usr/bin/env python3
"""voice-loop — the contour poller: SLIs and the two alerts the voice contour never had (#40).

Why it exists. The voice contour is a set of resident GPU services sharing one card and one node,
and until now every fault in it was detected by a human noticing the voice felt wrong — a dead
service stayed dead silently, and a service that quietly demoted itself from GPU to CPU kept
serving, correctly, at a fraction of the speed, until somebody read its /health by hand. There is
no monitoring substrate on the node and no sudo to install one, so this is the first honest step:
a small stdlib poller that writes a status file, plus a check in the existing hook path
(``speak.py`` voices an active alert once, at the end of a turn) — a page, not a dashboard.

What it does, per run:

* polls each configured service's ``/health`` (bounded timeout, bounded body) — availability and
  the ``device`` it is serving on;
* samples free VRAM via ``nvidia-smi`` (bounded subprocess, skipped cleanly where there is none);
* evaluates four alert rules — unreachable/not-ok, **device demoted** (a service reporting a
  device other than the one its config entry expects: the silent self-inflicted degradation that
  is the whole point of #40), **free VRAM below the floor**, and ``oom_overflows`` rising or
  restarting;
* writes it all to ``contour.json`` in the state dir, atomically (temp file, fsync, os.replace —
  a reader like the hook never sees a truncated file).

**How long a poll can take, as a bound rather than a hope.** The services are polled one after
another, so the whole thing costs at most ``(N + 1) × contour.timeout``: one bounded request per
service plus one bounded subprocess for the card. There is no other wait in the path. At the
shipped default that is 10 s for one service and under a minute for a contour of ten, against a
documented five-minute cadence — which is why ``timeout`` is per-request and deliberately small.

**What is NOT here, on purpose.** There is no latency history and no p95: the file holds the last
sample per service and the alerts, nothing accumulated. A week-long window that no rule reads and
no SLO is written against is not evidence, it is 967 KB the hook re-parses on every single tool
call — see README (#40 keeps the alerts; the SLI lands with the SLO that consumes it).

Configuration lives in ``~/.config/voice-loop/config.json`` under ``contour.*`` — nothing about
any real host is baked into this file:

  contour.services      list of entries; a bare URL string, or an object:
                        {name, health, expect_device}
                        Default: the local speech server only,
                        http://127.0.0.1:8355/health — loopback, no host named.
                        A value that is not a list, an entry with no usable health URL, and two
                        entries resolving to the SAME name are all CONFIG ERRORS (exit 64): a
                        service the operator configured and this poller silently never polls is a
                        monitoring hole wearing the word "ok".
    expect_device       the device a client depends on (e.g. "gpu"). Only when this is set does a
                        service reporting anything else raise the demotion alert — the alert means
                        "a client depends on the fast path", and that dependency is the operator's
                        to declare. Unset: the device is recorded, never alerted on.
  contour.timeout       per-request seconds (default 5). See the poll-time bound above.
  contour.vram.command  the free-VRAM probe (default nvidia-smi …); false disables it (no GPU,
                        macOS) — "" cannot, because cfg treats an empty string as unset.
  contour.vram.min_free_mib  the floor that pages (default 200 — below it the live contour's
                        transient allocations start being at risk).
  contour.status_path   where the status file is written AND where the hook reads it. One knob,
                        both halves: a poller pointed somewhere the hook does not read polls
                        correctly, exits 1 correctly, and pages nobody. ``--status`` overrides it
                        for a one-off probe; nothing else should.
  contour.max_age       seconds after which a status file is STALE — nobody is polling (default
                        900, three missed polls at the documented five-minute cadence). The
                        poller writes it into the file; the hook reads it and pages on it, so
                        "the contour is fine" and "nobody looked" are different answers.

Every numeric knob must be a JSON **number**. ``"5"`` is rejected exactly like ``"5s"``: JSON has
a number type, and a reader that guesses at one string and refuses the other gives the operator a
vocabulary nobody can predict.

Scheduling is the operator's cron or a systemd --user timer; the poller itself is one shot.

``ok`` in a health document is the service's own OPTIONAL self-assessment: True is healthy, False
is the not-ok alert, and ABSENT (or non-boolean) is "this document does not say" — recorded as
None, never paged on. A third-party /health that publishes {"status": "healthy"} and no ``ok`` is
answering, and a monitor that calls it down for using a different vocabulary cries wolf forever.

Stdlib only, Python 3.10+. ``contour-poll.sh`` is the thin entry point, exactly as ``speak.sh``
is for ``speak.py``.

Usage:
  contour-poll.sh [--json] [--status PATH] [--help]

  --json     print the whole status object instead of the one-line summary.
  --status   write somewhere other than ``contour.status_path`` (a probe, a test). A SCHEDULED
             poll should set that config key instead — the hook reads it, this flag it cannot see.

Exit codes, and the vocabulary is CLOSED — 1 means a page and nothing else, because a scheduler
branching on it must never be handed a config typo instead:
  0   polled, no alert active
  1   polled, at least one alert active (the status file says which). Including the case where
      the status file could not be WRITTEN: the poll still made its diagnosis, the exit code is
      then the only channel left to carry it, and 64 there would swallow a live page.
  64  called wrong, or the config cannot be read as a poller config (unparseable, a mistyped
      knob, a services value that is not a list, an unusable or duplicated entry), or the poll
      itself failed, or a quiet poll could not write its status file. Every one of those ALSO
      tries to write the status file, carrying a ``poller-error`` alert, so the hook pages about
      the broken monitor instead of reading a stale "all quiet".
"""

from __future__ import annotations

import http.client
import contracts
import json
import os
import shlex
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# Loopback only: the one service this plugin itself runs. Every other service is the operator's
# config — no hostname or endpoint of a real machine is ever written into the repo.
DEFAULT_SERVICES = [{"name": "voice-loop", "health": "http://127.0.0.1:8355/health"}]

# A poll must never be the slow part of anyone's turn: a service that has not answered in this
# long is down for our purposes, which is itself the signal.
DEFAULT_TIMEOUT = 5.0

# Below this the live contour's transient allocations start being at risk (#40: observed at
# 11 MiB during a shakedown; the floor pages long before that).
DEFAULT_VRAM_MIN_FREE_MIB = 200

# The card probe. Parsed as an argv list — never through a shell — and "" means "no GPU here".
DEFAULT_VRAM_COMMAND = "nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits"

# Three missed polls at the documented five-minute cadence. Written into the status file so the
# hook can tell a healthy contour from a poller nobody scheduled — the distinction #40 opens with.
DEFAULT_MAX_AGE = 900

# A /health body is small by design. The cap is for the day a neighbour answers with something
# that is not its health endpoint at all — the poller bounds what it reads rather than finding out.
BODY_CAP = 1 << 20

_STATE_DIR = os.path.join(os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")), "voice-loop")
STATUS_PATH = os.path.join(_STATE_DIR, "contour.json")

# The exit-code vocabulary, named rather than sprinkled — see the module docstring. EXIT_PAGE is
# the only code a scheduler may read as "an alert is active"; everything that is not a diagnosis
# of the contour leaves through EXIT_USAGE.
EXIT_QUIET = 0
EXIT_PAGE = 1
EXIT_USAGE = 64


# --- device alias table ---------------------------------------------------------------------------
#
# Real GPU services report different device strings (cuda, cuda:0, mps, rocm, hip) that all mean
# "the fast path". The operator should not need to guess the exact vocabulary the service prints;
# the README example uses "gpu", and a real service saying "cuda" should match it.
# The alias table normalizes both sides before comparison: known GPU types -> "gpu", CPU -> "cpu",
# unknown strings compare verbatim (to catch typos or new device types the table doesn't know yet).

_DEVICE_ALIASES = {
    # GPU types (all mean "the fast path")
    "cuda": "gpu",
    "cuda:0": "gpu",
    "cuda:1": "gpu",
    "cuda:2": "gpu",
    "cuda:3": "gpu",
    "mps": "gpu",
    "rocm": "gpu",
    "hip": "gpu",
    # CPU types (all mean "slow path")
    "cpu": "cpu",
    "cpu:0": "cpu",
    "cpu:1": "cpu",
    "cpu:2": "cpu",
    "cpu:3": "cpu",
}


def _normalize_device(device: str | None) -> str | None:
    """Normalize a device string through the alias table.

    Returns None for None, and a normalized string for known types.
    Unknown device strings are returned unchanged for verbatim comparison (to catch typos).
    """
    if device is None:
        return None
    return _DEVICE_ALIASES.get(device, device)


def _default_clock() -> datetime:
    """UTC now. Time is an INPUT here — injected so a test needs no sleep and no real wall clock."""
    return datetime.now(timezone.utc)


# --- config (standalone by design: each script in scripts/ carries its own reader, so a single
# --- file can be copied out and still run — keep the semantics in sync with speak.py) ------------


class ConfigError(Exception):
    """This config cannot be read as a poller config. A CALLER error (64), never a page (1).

    The one place this reader deliberately diverges from ``speak.py``'s: the hook swallows a bad
    config because it must never fail a turn, and falling back to defaults there costs a spoken
    line. Here the fallback costs the operator's whole contour — a poller that quietly narrows to
    the shipped default and then prints "contour ok" has claimed coverage it does not have.
    """


def load_config(path: str) -> dict:
    """The config file, or {} when there is none; malformed input remains a poller error."""
    try:
        return contracts.load_config(path, strict=True)
    except FileNotFoundError:
        return {}
    except OSError as err:
        raise ConfigError(f"cannot read {path}: {err}") from err
    except ValueError as err:
        raise ConfigError(f"{path} is not valid JSON: {err}") from err


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
    return contracts.config_path(environ)


def _name_from_url(url: str) -> str:
    """A service nobody named is known by where it lives — host:port, never the path."""
    return urllib.parse.urlsplit(url).netloc or url


def _url_is_fetchable(parts: urllib.parse.SplitResult) -> bool:
    """Can urllib actually turn this into a request at all?

    Beyond scheme and netloc there are two shapes that parse fine and then raise out of the
    OPENER rather than out of the config: a port that is not a number, and a host the idna codec
    refuses (an empty label, a label over 63 bytes, non-ASCII that will not encode). Both are
    config typos; asked at fetch time they surface as an uncaught UnicodeError/ValueError, which
    is how a typo used to exit 1 — the page code. Asked HERE they are what they are: exit 64.
    """
    try:
        host = parts.hostname
        parts.port  # noqa: B018 — the parse IS the check; a bad port raises ValueError
    except ValueError:
        return False
    if not host:
        return False
    try:
        host.encode("idna")
    except UnicodeError:
        return False
    return True


def normalize_service(entry) -> dict | None:
    """One config entry → {name, health, expect_device}, or None when unusable.

    A bare string is a health URL; an object takes the keys in the docstring. None means the
    entry cannot ever be polled (no usable URL) — the caller treats that as a ConfigError (exit
    64), because a service that is configured but never polled is a monitoring hole that looks
    like coverage, and silently skipping it would be exactly that hole.
    """
    if isinstance(entry, str):
        entry = {"health": entry}
    if not isinstance(entry, dict):
        return None
    health = str(entry.get("health") or "")
    try:
        parts = urllib.parse.urlsplit(health)
    except ValueError:  # an unbalanced IPv6 literal never even parses
        return None
    if parts.scheme not in ("http", "https") or not parts.netloc or not _url_is_fetchable(parts):
        return None
    # Control characters and newlines are stripped from the NAME because it is embedded verbatim in
    # line-based surfaces: the announced-ledger key ("<kind>:<name>", one per line) and the single-line
    # log format report_bug's cut markers match on. A "\n" in a name would split the ledger entry on
    # read-back — the same truncate-and-re-voice failure the whitespace fix closed for spaces.
    raw_name = str(entry.get("name") or "")
    name = " ".join("".join(ch if ch.isprintable() else " " for ch in raw_name).split())
    return {
        "name": name or _name_from_url(health),
        "health": health,
        "expect_device": str(entry.get("expect_device") or ""),
    }


def _number(config: dict, dotted: str, default, cast):
    """One numeric knob, or a ConfigError NAMING it. A mistyped scalar used to raise a bare
    ValueError out of here and out of main, and Python's exit for an uncaught exception is 1 —
    the page code. The knob's own name is what the operator needs, so it rides the message.

    A JSON **number**, strictly: ``"5"`` is refused exactly like ``"5s"``. Accepting the one that
    happens to parse gave the operator a rule nobody can predict — a quoted number is the same
    typo in both cases, and the one that is silently accepted is the one that goes on being
    written. ``true`` is refused with them: Python would read it as 1, JSON does not mean it.
    """
    value = cfg(config, dotted, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{dotted} must be a JSON number, not {value!r}")
    return cast(value)


def _resolve_services(raw) -> list[dict]:
    """The configured services, or a ConfigError. Nothing here EVER falls back silently.

    Three shapes are errors rather than defaults, and all three have the same failure: the poller
    would go on printing "contour ok" for a contour it is not watching — a mis-shaped
    ``contour.services``, an entry with no fetchable health URL, and two entries that collapse to
    one name (same slot, same key, one history sample lost per poll, and the page says "X; X").
    """
    if raw is None:
        return [normalize_service(entry) for entry in DEFAULT_SERVICES]
    if not isinstance(raw, list):
        raise ConfigError(f"contour.services must be a list of entries, not {type(raw).__name__}")
    services = []
    seen: dict[str, str] = {}
    for entry in raw:
        service = normalize_service(entry)
        if service is None:
            raise ConfigError(f"contour.services entry {entry!r} has no fetchable http(s) health URL")
        name = service["name"]
        if name in seen:
            raise ConfigError(
                f"contour.services has two entries named {name!r} ({seen[name]} and "
                f"{service['health']}) — give one of them its own \"name\""
            )
        seen[name] = service["health"]
        services.append(service)
    return services


def resolve_settings(config: dict) -> dict:
    """Every knob, one place, same precedence as the rest of the plugin. Raises ConfigError."""
    raw = cfg(config, "contour.services", None)
    # cfg() treats "" as unset (bash-cfg parity), so the probe is disabled with JSON false —
    # the one knob where "nothing" is a meaningful answer rather than a missing one.
    vram_command = cfg(config, "contour.vram.command", DEFAULT_VRAM_COMMAND)
    return {
        "services": _resolve_services(raw),
        "services_configured": raw is not None,
        "timeout": _number(config, "contour.timeout", DEFAULT_TIMEOUT, float),
        "vram_command": "" if vram_command is False else str(vram_command),
        "vram_min_free_mib": _number(config, "contour.vram.min_free_mib", DEFAULT_VRAM_MIN_FREE_MIB, int),
        "max_age": max(1, _number(config, "contour.max_age", DEFAULT_MAX_AGE, int)),
        # The one path both halves resolve the same way — see resolve_status_path() and speak.py's
        # contour_status_path(), which is the same lookup against the same key.
        "status_path": resolve_status_path(config),
    }


def resolve_status_path(config: dict) -> str:
    """Where contour.json lives, for BOTH the poller that writes it and the hook that reads it.

    The seam this closes: the poller could be pointed anywhere (``--status``) while the hook only
    ever read the default, so a cron line written with ``--status /var/tmp/contour.json`` polled
    correctly, exited 1 correctly, and paged nobody. Now one config key answers for both, and
    ``--status`` stays what it always was — an override for a one-off probe or a test.
    """
    return str(cfg(config, "contour.status_path", STATUS_PATH))


# --- the probes ----------------------------------------------------------------------------------


class FetchError(Exception):
    """The health endpoint did not answer with a parseable JSON object — the availability signal."""


def fetch_health(url: str, timeout: float, opener=urllib.request.urlopen) -> dict:
    """GET a health endpoint: bounded wait, bounded body, must parse to a JSON object.

    HTTPError IS one of the answers — a neighbour that replies 500 to /health is telling us it is
    not healthy, so the status code rides the exception into the availability alert.
    """
    try:
        with opener(url, timeout=timeout) as resp:
            body = resp.read(BODY_CAP + 1)
    except urllib.error.HTTPError as err:
        raise FetchError(f"http {err.code}") from err
    except (urllib.error.URLError, OSError, http.client.HTTPException) as err:
        # HTTPException covers the FRAMING failures (BadStatusLine, LineTooLong, IncompleteRead …)
        # that are neither URLError nor OSError — a service speaking garbage on its health port is
        # an unreachable-shaped answer for THIS service, and must not abort the whole poll (#40
        # Gate B finding 10546: one malformed neighbour left every other service and the VRAM
        # sample unsampled for the cycle).
        raise FetchError(type(err).__name__) from err
    if len(body) > BODY_CAP:
        raise FetchError(f"body over {BODY_CAP} bytes")
    try:
        parsed = json.loads(body)
    except ValueError as err:
        raise FetchError("body was not JSON") from err
    if not isinstance(parsed, dict):
        raise FetchError("body was not a JSON object")
    return parsed


def _default_runner(argv: list[str], timeout: float) -> "subprocess.CompletedProcess[str]":
    """The one subprocess policy in this repo: an argv LIST (never a shell string), a mandatory
    wall-clock timeout, and check=False — the caller turns the exit code into a result."""
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)


def sample_vram(command: str, timeout: float, runner=_default_runner) -> int | None:
    """Free MiB on the tightest card, or None when there is no answer worth having.

    None is not an alert: a machine with no GPU (or no nvidia-smi) has no VRAM floor to page on,
    and a probe that cannot run must never cry wolf — the services' own samples still say plenty.
    With several cards the MINIMUM free is the number that matters: the tightest card is where the
    next allocation fails.
    """
    argv = shlex.split(command)
    if not argv:
        return None
    try:
        done = runner(argv, timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if done.returncode != 0:
        return None
    values = []
    for line in done.stdout.splitlines():
        try:
            values.append(int(line.strip()))
        except ValueError:
            pass  # a stray line (a header, a blank) is not a card
    return min(values) if values else None


# --- the alerts -----------------------------------------------------------------------------------


def evaluate_service(service: dict, sample: dict, previous: dict) -> list[dict]:
    """The per-service alert rules, evaluated against THIS poll and the previous status file.

    Every alert is {key, kind, service, message} — the key is what the hook's announced-ledger
    dedups on, so it names the condition, never the moment.
    """
    name = service["name"]
    alerts = []

    def add(kind: str, message: str) -> None:
        alerts.append({"key": f"{kind}:{name}", "kind": kind, "service": name, "message": message})

    if not sample["reachable"]:
        add("unreachable", f"{name} is not answering its health endpoint ({sample['detail']})")
        return alerts  # nothing below can be known of a service that did not answer
    if sample["ok"] is False:
        # ONLY an explicit false. None means the document published no ``ok`` we can read — a
        # foreign /health with its own vocabulary is answering, and a monitor that reads "does not
        # use my key" as "is down" pages forever about a service that never had a fault.
        add("not-ok", f"{name} answers its health endpoint but reports ok=false")
    # Normalize both sides through the alias table before comparison: known GPU types
    # (cuda, mps, rocm, hip) -> "gpu", CPU types -> "cpu", unknown strings compare verbatim.
    expected = service["expect_device"]
    device = sample["device"]
    if expected and device and _normalize_device(device) != _normalize_device(expected):
        # THE alert of #40: a service that demoted itself off the fast path keeps serving
        # correctly — nothing breaks loudly, it just gets an order of magnitude slower and stays
        # that way until a human looks. expect_device is set exactly when a client depends on it.
        add("device-demoted", f"{name} is serving on {device}, expected {expected}")
    current_oom = sample["oom_overflows"]
    if isinstance(current_oom, (int, float)) and current_oom > 0:
        previous_oom = previous.get("oom_overflows")
        if not isinstance(previous_oom, (int, float)):
            # First sight of a non-zero counter: the card is oversubscribed and the fast path is
            # not what the caller thinks it is. A counter that holds steady does NOT re-page —
            # the announced condition is the CHANGE, not the number.
            add("oom-overflow", f"{name} oom_overflows rose to {int(current_oom)} — the card is oversubscribed")
        elif current_oom > previous_oom:
            add("oom-overflow", f"{name} oom_overflows rose to {int(current_oom)} — the card is oversubscribed")
        elif current_oom < previous_oom:
            # The counter went BACKWARDS: these are per-process counters, so the service restarted
            # and is already overflowing again. Comparing only ``>`` made exactly this case silent
            # — a service that died of OOM, came back at 0 and climbed again said nothing until it
            # passed its own pre-restart high-water mark, which is the moment it most needs saying.
            add(
                "oom-overflow",
                f"{name} oom_overflows is {int(current_oom)} after restarting (it was "
                f"{int(previous_oom)}) — the card is oversubscribed",
            )
    return alerts


def evaluate_vram(free_mib: int | None, floor: int) -> list[dict]:
    """The second alert of #40: free VRAM under the floor the live contour's transients need."""
    if free_mib is None or free_mib >= floor:
        return []
    return [
        {
            "key": "vram-low:gpu",
            "kind": "vram-low",
            "service": "",
            "message": f"free VRAM is {free_mib} MiB, below the {floor} MiB floor",
        }
    ]


# --- one poll ------------------------------------------------------------------------------------


def _warn(message: str) -> None:
    """The poller's one diagnostic channel: stderr. cron mails it, a shell shows it."""
    print(f"contour-poll: {message}", file=sys.stderr)


class StatusData(dict):
    """Previous status payload plus the verdict established while reading it."""

    def __init__(self, data: dict, *, status: str, detail: str = "") -> None:
        super().__init__(data)
        self.status = status
        self.detail = detail


def read_status(path: str, warn=_warn) -> StatusData:
    """Read the previous status and distinguish missing, unreadable and malformed files."""
    try:
        with open(path, encoding="utf-8") as fh:
            loaded = json.load(fh)
    except FileNotFoundError:
        return StatusData({}, status="missing")
    except OSError as err:
        detail = f"{type(err).__name__}: {err}"
        warn(f"the previous status file at {path} could not be read ({detail}) — this poll has no baseline")
        return StatusData({}, status="unreadable", detail=detail)
    except (ValueError, UnicodeDecodeError) as err:
        detail = f"{type(err).__name__}: {err}"
        warn(f"the previous status file at {path} is not readable JSON ({detail}) — this poll has no baseline")
        return StatusData({}, status="malformed", detail=detail)
    if not isinstance(loaded, dict):
        detail = "status must be a JSON object"
        warn(f"the previous status file at {path} does not hold a JSON object — this poll has no baseline")
        return StatusData({}, status="malformed", detail=detail)
    return StatusData(loaded, status="ok")


class StatusWriteError(Exception):
    """The poll ran and made its diagnosis; only the file it writes it to could not be written.

    It carries the ``status`` so main() can still report what the poll found and, crucially, still
    exit 1 when an alert is active: an unwritable state dir that downgraded a live page to 64
    meant a caller branching on 1 never paged for as long as the disk stayed full.
    """

    def __init__(self, message: str, status: dict) -> None:
        super().__init__(message)
        self.status = status


def write_status(path: str, status: dict) -> None:
    """Write-then-replace: a sibling temp file, fsynced, os.replace()d into place — a reader (the
    hook, mid-turn) sees the old content or the new, never a truncated file accepted as valid."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=".contour-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(status, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def poll(settings: dict, status_path: str, *, fetch=None, runner=None, clock=None) -> dict:
    """One full pass: poll every service, sample the card, evaluate the rules, write the file.

    The previous status is read BEFORE the new one is written — the oom_overflows alert is a delta
    against it. A corrupt or absent previous file costs the deltas of one cycle, nothing more, and
    ``read_status`` says so on stderr rather than starting over in silence.

    BOUNDED: one ``settings["timeout"]`` per service, serially, plus one for the card probe — so
    at most ``(N + 1) × timeout`` for the whole call, with no other wait in the path.

    Raises StatusWriteError (carrying the finished status) when everything was polled and only the
    write failed — the caller still has a diagnosis to report and an exit code to carry it.

    The seams default to None and resolve HERE, inside the call — not in the signature, where a
    default would bind the original function object at import and be invisible to a monkeypatch.
    """
    fetch = fetch_health if fetch is None else fetch
    runner = _default_runner if runner is None else runner
    clock = _default_clock if clock is None else clock
    previous = read_status(status_path)
    previous_services = previous.get("services", {}) if isinstance(previous.get("services"), dict) else {}

    at = clock().isoformat()
    services: dict[str, dict] = {}
    alerts: list[dict] = []

    for service in settings["services"]:
        name = service["name"]
        try:
            health = fetch(service["health"], settings["timeout"])
        except FetchError as err:
            sample = {"reachable": False, "ok": None, "device": None, "oom_overflows": None, "detail": str(err)}
        else:
            sample = {
                "reachable": True,
                # Tri-state on purpose: True healthy, False the not-ok alert, None "this document
                # does not say". A bool() here is what turned every third-party /health into a
                # permanent page — see the module docstring.
                "ok": health["ok"] if isinstance(health.get("ok"), bool) else None,
                "device": str(health["device"]) if health.get("device") is not None else None,
                # bool is EXCLUDED exactly as _number excludes it: a foreign /health publishing
                # "oom_overflows": true would otherwise pass isinstance(int) and page "rose to 1" —
                # the same different-vocabulary trap the ok tri-state above closes.
                "oom_overflows": (
                    health.get("oom_overflows")
                    if isinstance(health.get("oom_overflows"), (int, float))
                    and not isinstance(health.get("oom_overflows"), bool)
                    else None
                ),
                "detail": "",
            }
        services[name] = sample
        alerts.extend(evaluate_service(service, sample, previous_services.get(name, {})))

    free_mib = sample_vram(settings["vram_command"], settings["timeout"], runner)
    alerts.extend(evaluate_vram(free_mib, settings["vram_min_free_mib"]))

    status = {
        "at": at,
        # The freshness bound travels WITH the file: a reader cannot know this poller's cadence,
        # and a status file with no expiry is why a poller nobody scheduled reads as a green
        # contour forever. The hook pages on it (speak.py: contour_alerts).
        "max_age": settings["max_age"],
        "alerts": alerts,
        "services": services,
        "vram": {"free_mib": free_mib, "min_free_mib": settings["vram_min_free_mib"]},
    }
    try:
        write_status(status_path, status)
    except OSError as err:
        raise StatusWriteError(f"the poll finished but {status_path} could not be written: {err}", status) from err
    return status


# --- the shell ------------------------------------------------------------------------------------


USAGE = (
    "contour-poll [--json] [--status PATH] [--help]\n"
    "  --json    print the whole status object instead of the one-line summary\n"
    "  --status  write somewhere other than contour.status_path (a probe, a test) — note that the\n"
    "            speaking hook reads contour.status_path, so a SCHEDULED poll should set that\n"
    "            config key rather than pass this flag\n"
    "  --help    this text"
)


def record_poller_error(status_path: str, message: str, clock=None) -> None:
    """A poller that could not poll says so IN THE STATUS FILE, then leaves through 64.

    Without this, every 64 left the previous file untouched and the hook went on reading an "all
    quiet" nobody had refreshed. The previous samples are kept verbatim — they are still the last
    true thing known — while ``at`` and ``alerts`` become this run's: a ``poller-error`` page the
    hook voices exactly once, keyed on the condition like every other.
    """
    clock = _default_clock if clock is None else clock
    status = read_status(status_path)
    status["at"] = clock().isoformat()
    status.setdefault("max_age", DEFAULT_MAX_AGE)
    status["alerts"] = [
        {
            "key": "poller-error",
            "kind": "poller-error",
            "service": "",
            "message": f"the contour poller could not run: {message}",
        }
    ]
    try:
        os.makedirs(os.path.dirname(status_path) or ".", exist_ok=True)
        write_status(status_path, status)
    except OSError:
        pass  # an unwritable state dir costs the page, never a second traceback on the way out


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    as_json = False
    # None until the config has been read: --status wins, then contour.status_path, then the
    # documented default. Until then the default is the best guess for where a poller-error goes.
    cli_status: str | None = None
    while args:
        arg = args.pop(0)
        if arg == "--json":
            as_json = True
        elif arg == "--status":
            if not args:
                print("contour-poll: --status needs a path", file=sys.stderr)
                return EXIT_USAGE
            cli_status = args.pop(0)
        elif arg in ("--help", "-h"):
            print(USAGE)
            return EXIT_QUIET
        else:
            print(f"contour-poll: unknown argument {arg!r} (--help lists them)", file=sys.stderr)
            return EXIT_USAGE

    status_path = cli_status or STATUS_PATH
    settings: dict = {}

    def report(status: dict) -> None:
        """What one poll says out loud. Shared by the ordinary tail and the unwritable-file path,
        because a poll whose file could not be written still has a diagnosis to deliver."""
        if as_json:
            print(json.dumps(status, indent=2, sort_keys=True))
        elif status["alerts"]:
            for alert in status["alerts"]:
                print(f"ALERT {alert['message']}")
        else:
            # The success line names WHAT was polled, not just how many answered: "contour ok" over
            # the shipped default while the operator believes their own list is being watched is
            # the coverage lie this poller exists to prevent, so services_configured is said aloud.
            reachable = sum(1 for sample in status["services"].values() if sample["reachable"])
            scope = "" if settings["services_configured"] else " (contour.services unset — the default local service only)"
            silent = sum(1 for sample in status["services"].values() if sample["reachable"] and sample["ok"] is None)
            unread = f", {silent} publishing no ok field" if silent else ""
            print(f"contour ok — {reachable}/{len(status['services'])} services answered{unread}{scope}")

    # THE closed vocabulary, and this is the whole of it: everything from here down that is not a
    # diagnosis of the contour — a config that will not read, a knob that is not a number, an
    # entry urllib cannot fetch, a state dir that will not write, any internal error at all —
    # leaves as 64 with the status file updated. Only a poll that RAN can return 1.
    try:
        config = load_config(config_path())
        status_path = cli_status or resolve_status_path(config)
        settings = resolve_settings(config)
        if not settings["services"]:
            # A contour.services key that names nothing pollable is a config error, not an empty
            # contour: answering "ok" here would page nobody about a contour nobody watches.
            raise ConfigError("contour.services names no usable service (each needs an http(s) health URL)")
        os.makedirs(os.path.dirname(status_path) or ".", exist_ok=True)
        status = poll(settings, status_path)
    except StatusWriteError as err:
        # The poll RAN. Only its file did not land, so the exit code is the last channel the
        # diagnosis has — and downgrading a live page to 64 here is how an unwritable state dir
        # silenced a caller that branches on 1. The page outranks the broken monitor.
        print(f"contour-poll: {err}", file=sys.stderr)
        report(err.status)
        return EXIT_PAGE if err.status["alerts"] else EXIT_USAGE
    except ConfigError as err:
        print(f"contour-poll: {err}", file=sys.stderr)
        record_poller_error(status_path, str(err))
        return EXIT_USAGE
    except Exception as err:  # noqa: BLE001 — an uncaught one would exit 1, and 1 means PAGE
        print(f"contour-poll: the poll failed: {type(err).__name__}: {err}", file=sys.stderr)
        record_poller_error(status_path, f"{type(err).__name__}: {err}")
        return EXIT_USAGE

    report(status)
    return EXIT_PAGE if status["alerts"] else EXIT_QUIET


if __name__ == "__main__":
    sys.exit(main())
