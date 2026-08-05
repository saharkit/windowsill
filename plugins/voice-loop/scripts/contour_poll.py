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
  is the whole point of #40), **free VRAM below the floor**, and ``oom_overflows`` on the rise;
* appends each sample to a bounded per-service history and computes the latency SLI the issue
  asks for: **p95 of any configured numeric health field, split by device** — the CPU/GPU split
  is a 30–90x cliff, so an average across both is meaningless;
* writes it all to ``contour.json`` in the state dir, atomically (temp file, fsync, os.replace —
  a reader like the hook never sees a truncated file).

Configuration lives in ``~/.config/voice-loop/config.json`` under ``contour.*`` — nothing about
any real host is baked into this file:

  contour.services      list of entries; a bare URL string, or an object:
                        {name, health, expect_device, latency_fields}
                        Default: the local speech server only,
                        http://127.0.0.1:8355/health — loopback, no host named.
    expect_device       the device a client depends on (e.g. "gpu"). Only when this is set does a
                        service reporting anything else raise the demotion alert — the alert means
                        "a client depends on the fast path", and that dependency is the operator's
                        to declare. Unset: the device is recorded, never alerted on.
    latency_fields      numeric /health fields to keep as latency samples (e.g. an RTF gauge the
                        service publishes). p95 is computed per field PER DEVICE over the history.
  contour.timeout       per-request seconds (default 5).
  contour.vram.command  the free-VRAM probe (default nvidia-smi …); false disables it (no GPU,
                        macOS) — "" cannot, because cfg treats an empty string as unset.
  contour.vram.min_free_mib  the floor that pages (default 200 — below it the live contour's
                        transient allocations start being at risk).
  contour.history       samples kept per service (default 2016 — a week at a 5-minute cadence).

Scheduling is the operator's cron or a systemd --user timer; the poller itself is one shot.

Stdlib only, Python 3.10+. ``contour-poll.sh`` is the thin entry point, exactly as ``speak.sh``
is for ``speak.py``.

Usage:
  contour-poll.sh [--json] [--status PATH] [--help]

  --json     print the whole status object instead of the one-line summary.
  --status   write somewhere other than the default state-dir path (a probe, a test).

Exit codes: 0 polled, no alert active · 1 polled, at least one alert active (the status file says
which) · 64 called wrong or the config names services none of which is usable — a caller that
branches on "1 means page" must never be handed a typo instead.
"""

from __future__ import annotations

import json
import math
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

# 2016 = one sample every five minutes for a week: enough history to know what "normal" is before
# anyone writes an SLO, small enough that the status file stays a file.
DEFAULT_HISTORY = 2016

# A /health body is small by design. The cap is for the day a neighbour answers with something
# that is not its health endpoint at all — the poller bounds what it reads rather than finding out.
BODY_CAP = 1 << 20

_STATE_DIR = os.path.join(os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")), "voice-loop")
STATUS_PATH = os.path.join(_STATE_DIR, "contour.json")


def _default_clock() -> datetime:
    """UTC now. Time is an INPUT here — injected so a test needs no sleep and no real wall clock."""
    return datetime.now(timezone.utc)


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
        os.path.join(environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "voice-loop/config.json"),
    )


def _name_from_url(url: str) -> str:
    """A service nobody named is known by where it lives — host:port, never the path."""
    return urllib.parse.urlsplit(url).netloc or url


def normalize_service(entry) -> dict | None:
    """One config entry → {name, health, expect_device, latency_fields}, or None when unusable.

    A bare string is a health URL; an object takes the keys in the docstring. None means the
    entry cannot ever be polled (no usable URL) — the caller skips it loudly, because a service
    that is configured but never polled is a monitoring hole that looks like coverage.
    """
    if isinstance(entry, str):
        entry = {"health": entry}
    if not isinstance(entry, dict):
        return None
    health = str(entry.get("health") or "")
    parts = urllib.parse.urlsplit(health)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    fields = entry.get("latency_fields") or []
    return {
        "name": str(entry.get("name") or _name_from_url(health)),
        "health": health,
        "expect_device": str(entry.get("expect_device") or ""),
        "latency_fields": [str(field) for field in fields if isinstance(field, (str, int))],
    }


def resolve_settings(config: dict) -> dict:
    """Every knob, one place, same precedence as the rest of the plugin."""
    raw = cfg(config, "contour.services", None)
    entries = raw if isinstance(raw, list) else DEFAULT_SERVICES
    services = [normalized for entry in entries if (normalized := normalize_service(entry)) is not None]
    # cfg() treats "" as unset (bash-cfg parity), so the probe is disabled with JSON false —
    # the one knob where "nothing" is a meaningful answer rather than a missing one.
    vram_command = cfg(config, "contour.vram.command", DEFAULT_VRAM_COMMAND)
    return {
        "services": services,
        "services_configured": isinstance(raw, list),
        "timeout": float(cfg(config, "contour.timeout", DEFAULT_TIMEOUT)),
        "vram_command": "" if vram_command is False else str(vram_command),
        "vram_min_free_mib": int(cfg(config, "contour.vram.min_free_mib", DEFAULT_VRAM_MIN_FREE_MIB)),
        "history": max(1, int(cfg(config, "contour.history", DEFAULT_HISTORY))),
    }


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
    except (urllib.error.URLError, OSError) as err:
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


# --- the SLI -------------------------------------------------------------------------------------


def p95(values: list[float]) -> float | None:
    """Nearest-rank p95 — None for an empty window, the value itself for a window of one."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[math.ceil(0.95 * len(ordered)) - 1]


def latency_p95(history: list[dict], fields: list[str]) -> dict:
    """The issue's first SLI: p95 per configured field, SPLIT BY DEVICE.

    The CPU/GPU split is a cliff, not a gradient, so a number averaged across both would describe
    a service that never exists. The split IS the signal: {device: {field: p95}}.
    """
    by_device: dict[str, dict[str, list[float]]] = {}
    for entry in history:
        device = str(entry.get("device") or "unknown")
        for field in fields:
            value = entry.get("fields", {}).get(field)
            if isinstance(value, (int, float)):
                by_device.setdefault(device, {}).setdefault(field, []).append(float(value))
    return {
        device: {field: p95(values) for field, values in fields_map.items()}
        for device, fields_map in by_device.items()
    }


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
        add("not-ok", f"{name} answers its health endpoint but reports ok=false")
    expected = service["expect_device"]
    device = sample["device"]
    if expected and device and device != expected:
        # THE alert of #40: a service that demoted itself off the fast path keeps serving
        # correctly — nothing breaks loudly, it just gets an order of magnitude slower and stays
        # that way until a human looks. expect_device is set exactly when a client depends on it.
        add("device-demoted", f"{name} is serving on {device}, expected {expected}")
    current_oom = sample["oom_overflows"]
    if isinstance(current_oom, (int, float)) and current_oom > 0:
        previous_oom = previous.get("oom_overflows")
        if not isinstance(previous_oom, (int, float)) or current_oom > previous_oom:
            # First sight of a non-zero counter, or a rise since the last poll: the card is
            # oversubscribed and the fast path is not what the caller thinks it is. A counter
            # that holds steady does NOT re-page — the announced condition is the rise.
            add("oom-overflow", f"{name} oom_overflows rose to {int(current_oom)} — the card is oversubscribed")
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


def read_status(path: str) -> dict:
    """The previous status file, tolerantly: anything unreadable or unparseable is no history."""
    try:
        with open(path, encoding="utf-8") as fh:
            loaded = json.load(fh)
        return loaded if isinstance(loaded, dict) else {}
    except (OSError, ValueError):
        return {}


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
    against it, and the history accumulates across runs. A corrupt or absent previous file costs
    the deltas of one cycle, nothing more.

    The seams default to None and resolve HERE, inside the call — not in the signature, where a
    default would bind the original function object at import and be invisible to a monkeypatch.
    """
    fetch = fetch_health if fetch is None else fetch
    runner = _default_runner if runner is None else runner
    clock = _default_clock if clock is None else clock
    previous = read_status(status_path)
    previous_services = previous.get("services", {}) if isinstance(previous.get("services"), dict) else {}
    previous_history = previous.get("history", {}) if isinstance(previous.get("history"), dict) else {}

    at = clock().isoformat()
    services: dict[str, dict] = {}
    history: dict[str, list] = {}
    alerts: list[dict] = []

    for service in settings["services"]:
        name = service["name"]
        try:
            health = fetch(service["health"], settings["timeout"])
        except FetchError as err:
            sample = {"reachable": False, "ok": None, "device": None, "oom_overflows": None, "fields": {}, "detail": str(err)}
        else:
            fields = {
                field: health[field]
                for field in service["latency_fields"]
                if isinstance(health.get(field), (int, float))
            }
            sample = {
                "reachable": True,
                "ok": health.get("ok") is True,
                "device": str(health["device"]) if health.get("device") is not None else None,
                "oom_overflows": health.get("oom_overflows") if isinstance(health.get("oom_overflows"), (int, float)) else None,
                "fields": fields,
                "detail": "",
            }
        services[name] = sample
        alerts.extend(evaluate_service(service, sample, previous_services.get(name, {})))

        entries = list(previous_history.get(name, [])) if isinstance(previous_history.get(name), list) else []
        entries.append({"at": at, "ok": bool(sample["ok"]), "device": sample["device"], "fields": sample["fields"]})
        history[name] = entries[-settings["history"] :]

    free_mib = sample_vram(settings["vram_command"], settings["timeout"], runner)
    alerts.extend(evaluate_vram(free_mib, settings["vram_min_free_mib"]))

    status = {
        "at": at,
        "alerts": alerts,
        "services": services,
        "vram": {"free_mib": free_mib, "min_free_mib": settings["vram_min_free_mib"]},
        "history": history,
        "p95": {
            service["name"]: latency_p95(history[service["name"]], service["latency_fields"])
            for service in settings["services"]
            if service["latency_fields"]
        },
    }
    write_status(status_path, status)
    return status


# --- the shell ------------------------------------------------------------------------------------


USAGE = "contour-poll [--json] [--status PATH]  (--help describes what a poll does)"


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    as_json = False
    status_path = STATUS_PATH
    while args:
        arg = args.pop(0)
        if arg == "--json":
            as_json = True
        elif arg == "--status":
            if not args:
                print("contour-poll: --status needs a path", file=sys.stderr)
                return 64
            status_path = args.pop(0)
        elif arg in ("--help", "-h"):
            print(USAGE)
            return 0
        else:
            print(f"contour-poll: unknown argument {arg!r} (--help lists them)", file=sys.stderr)
            return 64

    settings = resolve_settings(load_config(config_path()))
    if not settings["services"]:
        # A contour.services key that names nothing pollable is a config error, not an empty
        # contour: answering "ok" here would page nobody about a contour nobody watches.
        print("contour-poll: contour.services names no usable service (each needs an http(s) health URL)", file=sys.stderr)
        return 64

    try:
        os.makedirs(os.path.dirname(status_path) or ".", exist_ok=True)
        status = poll(settings, status_path)
    except OSError as err:
        print(f"contour-poll: cannot write the status file: {err}", file=sys.stderr)
        return 64

    if as_json:
        print(json.dumps(status, indent=2, sort_keys=True))
    elif status["alerts"]:
        for alert in status["alerts"]:
            print(f"ALERT {alert['message']}")
    else:
        reachable = sum(1 for sample in status["services"].values() if sample["reachable"])
        print(f"contour ok — {reachable}/{len(status['services'])} services answered")
    return 1 if status["alerts"] else 0


if __name__ == "__main__":
    sys.exit(main())
