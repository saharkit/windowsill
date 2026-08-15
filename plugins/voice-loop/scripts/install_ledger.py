#!/usr/bin/env python3
"""voice-loop — install step ledger: deterministic lifecycle with resume / restart / cancel.

Why it exists. The voice-setup skill installs the voice contour step by step, and until now
there was no durable record of progress. An interrupted install left no trace of what had
been done, so re-running setup either repeated work or, worse, assumed a fresh machine and
started from scratch — potentially leaving a half-written config or an orphaned service.

This module is the install's ground truth: a step ledger file at
``~/.local/state/voice-loop/install.ledger`` that records every completed step, so re-entry
can detect the interrupted state and offer exactly three choices — resume (continue from the
last completed step), restart (clean slate after undoing partial state), or cancel (leave
everything as-is). Running the installer over a completed install is a no-op: the ledger says
"done" and the skill reports that fact rather than re-doing work.

Every step logs a durable checkpoint marker so resume has ground truth — a step ledger file,
not memory.

Stdlib only, Python 3.10+. Used by the /voice-setup skill via its CLI; also importable by
tests so the interrupted-then-resume and interrupted-then-restart paths are covered.

Usage:
  python3 install_ledger.py check            — print JSON state; exit 0 if none/complete, 1 otherwise
  python3 install_ledger.py start             — create a fresh ledger (idempotent: no-op if one exists)
  python3 install_ledger.py step-begin STEP   — mark a step as started (before side effects)
  python3 install_ledger.py step-done STEP    — mark a step as complete (after side effects succeeded)
  python3 install_ledger.py finish            — mark the whole install as complete
  python3 install_ledger.py reset             — delete the ledger (for a restart)
  python3 install_ledger.py completed-steps   — print JSON array of completed step ids

Exit codes: 0 on success (check: none/complete/cancelled), 1 on in_progress, 2 on error.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from typing import Any, Callable

# ---------------------------------------------------------------------------
# Step vocabulary — every step the voice-setup skill walks through, in order.
# A step id that isn't in this list is rejected (fail-closed: typo protection).
# ---------------------------------------------------------------------------

INSTALL_STEPS: tuple[str, ...] = (
    "step-0-probe",
    "step-1-language",
    "step-2-backends",
    "step-3-install-deps",
    "step-4-write-config",
    "step-5-paste-behaviour",
    "step-6-hotkey",
    "step-7-speak-convention",
    "step-8-verify",
)

_STEP_INDEX: dict[str, int] = {sid: i for i, sid in enumerate(INSTALL_STEPS)}

# ---------------------------------------------------------------------------
# injected-clock — time is an input, never an ambient read
# ---------------------------------------------------------------------------


def _default_clock() -> datetime:
    """The production clock — UTC, timezone-aware."""
    return datetime.now(timezone.utc)


def _ts(clock: Callable[[], datetime]) -> str:
    """An ISO-8601 string with timezone, from the injected clock."""
    return clock().isoformat()


# ---------------------------------------------------------------------------
# Ledger path — the single file on disk
# ---------------------------------------------------------------------------


def _default_ledger_path() -> str:
    """The ledger lives in the state directory, side by side with speak.log and the spoken-ledger."""
    xdg_state = os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state"))
    return os.path.join(xdg_state, "voice-loop", "install.ledger")


# ---------------------------------------------------------------------------
# Atomic write-then-replace — a crash mid-write leaves the old content or the
# new, never a TRUNCATED file a reader would accept as valid-but-partial.
# ---------------------------------------------------------------------------


def _atomic_write(path: str, content: str) -> None:
    """Write *content* to *path* atomically: temp sibling, fsync, os.replace."""
    dirname = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(prefix="voice-loop-install-", dir=dirname)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        # Best-effort cleanup of the temp file on failure — the real ledger
        # hasn't been touched because os.replace hasn't run yet.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Ledger read / write
# ---------------------------------------------------------------------------


class LedgerData(dict):
    """Parsed ledger data with a non-payload read verdict."""

    def __init__(self, data: dict[str, Any], *, status: str, detail: str = "") -> None:
        super().__init__(data)
        self.status = status
        self.detail = detail


def read_ledger(ledger_path: str) -> LedgerData | None:
    """Read the ledger while distinguishing missing, unreadable and malformed files."""
    try:
        with open(ledger_path, encoding="utf-8") as fh:
            loaded = json.load(fh)
    except FileNotFoundError:
        return LedgerData({}, status="missing")
    except OSError as err:
        return LedgerData({}, status="unreadable", detail=f"{type(err).__name__}: {err}")
    except (json.JSONDecodeError, UnicodeDecodeError) as err:
        return LedgerData({}, status="malformed", detail=f"{type(err).__name__}: {err}")
    if not isinstance(loaded, dict):
        return LedgerData({}, status="malformed", detail="ledger must be a JSON object")
    state = loaded.get("state", "none")
    steps = loaded.get("steps", {})
    if not isinstance(state, str) or state not in {"none", "in_progress", "complete", "cancelled"}:
        return LedgerData({}, status="malformed", detail=f"invalid ledger state: {state!r}")
    if not isinstance(steps, dict) or any(not isinstance(entry, dict) for entry in steps.values()):
        return LedgerData({}, status="malformed", detail="ledger steps must be an object of objects")
    return LedgerData(loaded, status="ok")


def write_ledger(
    ledger_path: str,
    ledger: dict[str, Any],
    *,
    clock: Callable[[], datetime] | None = None,
) -> None:
    """Write *ledger* to *ledger_path* atomically, stamping ``updated_at`` from the clock."""
    if clock is None:
        clock = _default_clock
    ledger["updated_at"] = _ts(clock)
    _atomic_write(ledger_path, json.dumps(ledger, indent=2, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# State machine — the core operations
# ---------------------------------------------------------------------------


def check_state(
    ledger_path: str | None = None,
) -> dict[str, Any]:
    """Return the current install state as a JSON-serialisable dict.

    The returned dict always has a ``state`` key: ``"none"``, ``"in_progress"``,
    ``"complete"``, or ``"cancelled"``.  A present but unreadable or malformed
    ledger is reported as ``state: "none"`` with ``read_status`` and
    ``read_detail`` fields, so the CLI contract remains closed while preserving
    the reason for recovery.  When the state is ``"in_progress"`` or
    ``"complete"``, ``completed_steps`` lists the step ids that finished.
    ``"in_progress"`` also carries ``current_step`` if a step was begun but not
    yet completed.
    """
    if ledger_path is None:
        ledger_path = _default_ledger_path()
    ledger = read_ledger(ledger_path)
    if ledger is None or ledger.status == "missing":
        return {"state": "none"}
    if ledger.status != "ok":
        return {"state": "none", "read_status": ledger.status, "read_detail": ledger.detail}

    state = ledger.get("state", "none")
    result: dict[str, Any] = {"state": state}

    if "started_at" in ledger:
        result["started_at"] = ledger["started_at"]
    if "completed_at" in ledger:
        result["completed_at"] = ledger["completed_at"]
    if "cancelled_at" in ledger:
        result["cancelled_at"] = ledger["cancelled_at"]

    # Which steps are done?  A step is "complete" when its status is "complete".
    steps = ledger.get("steps", {})
    completed = [sid for sid in INSTALL_STEPS if steps.get(sid, {}).get("status") == "complete"]
    result["completed_steps"] = completed

    # Is a step currently in flight?
    in_flight = [sid for sid in INSTALL_STEPS if steps.get(sid, {}).get("status") == "in_progress"]
    if in_flight:
        result["current_step"] = in_flight[0]  # at most one at a time

    # Next pending step — only meaningful for in_progress (where resume is
    # the option).  cancelled is a terminal state: the user chose "leave
    # everything as-is and exit", so there is nothing to resume toward.
    if state == "in_progress":
        next_step = _next_pending(steps)
        if next_step is not None:
            result["next_step"] = next_step

    return result


def _next_pending(steps: dict[str, Any]) -> str | None:
    """The first step in order whose status is not ``"complete"``."""
    for sid in INSTALL_STEPS:
        status = steps.get(sid, {}).get("status", "pending")
        if status != "complete":
            return sid
    return None


def start_install(
    ledger_path: str | None = None,
    *,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Create a fresh ledger if none exists. Idempotent: returns the existing one if present.

    Returns the current state dict (``check_state`` format).
    """
    if ledger_path is None:
        ledger_path = _default_ledger_path()
    if clock is None:
        clock = _default_clock

    existing = read_ledger(ledger_path)
    if existing is not None and existing.status == "ok":
        state = existing.get("state", "none")
        if state == "cancelled":
            # Auto-restart from cancelled — the user is re-running setup after
            # previously choosing "leave everything as-is."  Starting fresh is
            # the natural intent; the skill does not need to call reset first.
            pass  # fall through to create a fresh ledger below
        else:
            # in_progress or complete — return existing; the skill must
            # decide whether to resume, restart, or do nothing.
            return check_state(ledger_path)

    now_ts = _ts(clock)
    ledger: dict[str, Any] = {
        "version": 1,
        "started_at": now_ts,
        "state": "in_progress",
        "steps": {sid: {"status": "pending"} for sid in INSTALL_STEPS},
        "step_order": list(INSTALL_STEPS),
    }
    ledger["updated_at"] = now_ts

    os.makedirs(os.path.dirname(ledger_path), exist_ok=True)
    write_ledger(ledger_path, ledger, clock=clock)
    return check_state(ledger_path)


def step_begin(
    step_id: str,
    ledger_path: str | None = None,
    *,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Mark *step_id* as started (``in_progress``).

    Only one step can be in-flight at a time.  A step that is already complete
    is left alone (idempotent).  Returns the updated state dict.
    """
    if ledger_path is None:
        ledger_path = _default_ledger_path()
    if clock is None:
        clock = _default_clock

    _validate_step(step_id)
    ledger = _require_ledger(ledger_path)
    now_ts = _ts(clock)

    steps: dict[str, Any] = ledger.setdefault("steps", {})
    entry = steps.setdefault(step_id, {})
    if entry.get("status") == "complete":
        return check_state(ledger_path)
    entry["status"] = "in_progress"
    entry["started_at"] = now_ts

    write_ledger(ledger_path, ledger, clock=clock)
    return check_state(ledger_path)


def step_done(
    step_id: str,
    ledger_path: str | None = None,
    *,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Mark *step_id* as complete.  Idempotent — calling twice is harmless.

    Only the named step is marked complete; earlier steps must already be done
    but they are not re-checked here (the skill drives the order).  Returns the
    updated state dict.
    """
    if ledger_path is None:
        ledger_path = _default_ledger_path()
    if clock is None:
        clock = _default_clock

    _validate_step(step_id)
    ledger = _require_ledger(ledger_path)
    now_ts = _ts(clock)

    steps: dict[str, Any] = ledger.setdefault("steps", {})
    entry = steps.setdefault(step_id, {})
    if entry.get("status") == "complete":
        return check_state(ledger_path)  # already done — truly idempotent
    entry["status"] = "complete"
    entry["completed_at"] = now_ts

    write_ledger(ledger_path, ledger, clock=clock)
    return check_state(ledger_path)


def finish_install(
    ledger_path: str | None = None,
    *,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Mark the install as complete.  Idempotent.

    Returns the updated state dict.  **Refuses** (returns an error dict
    with ``"success": False`` and the list of incomplete steps) when any
    step in ``step_order`` is not complete — the ground truth must not
    call itself complete while a step is unfinished (D4).
    """
    if ledger_path is None:
        ledger_path = _default_ledger_path()
    if clock is None:
        clock = _default_clock

    ledger = _require_ledger(ledger_path)
    if ledger.get("state") == "complete":
        return check_state(ledger_path)  # already complete — truly idempotent

    steps: dict[str, Any] = ledger.get("steps", {})
    step_order: list[str] = ledger.get("step_order", list(INSTALL_STEPS))
    incomplete = [
        sid
        for sid in step_order
        if steps.get(sid, {}).get("status") != "complete"
    ]
    if incomplete:
        return {
            "success": False,
            "state": ledger.get("state", "in_progress"),
            "incomplete_steps": incomplete,
            "message": (
                f"refusing to finish: {len(incomplete)} step(s) are not complete "
                f"({', '.join(incomplete)})"
            ),
        }

    now_ts = _ts(clock)
    ledger["state"] = "complete"
    ledger["completed_at"] = now_ts
    write_ledger(ledger_path, ledger, clock=clock)
    return check_state(ledger_path)


def cancel_install(
    ledger_path: str | None = None,
    *,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Mark the install as cancelled (user chose "leave everything as-is and exit").

    Returns the updated state dict.
    """
    if ledger_path is None:
        ledger_path = _default_ledger_path()
    if clock is None:
        clock = _default_clock

    ledger = _require_ledger(ledger_path)
    now_ts = _ts(clock)
    ledger["state"] = "cancelled"
    ledger["cancelled_at"] = now_ts
    write_ledger(ledger_path, ledger, clock=clock)
    return check_state(ledger_path)


def reset_install(
    ledger_path: str | None = None,
) -> None:
    """Delete the ledger file — the first half of a restart (undo of partial state
    is the skill's responsibility, guided by the ledger contents before this call).

    Idempotent: if no ledger exists, this is a silent no-op.
    """
    if ledger_path is None:
        ledger_path = _default_ledger_path()
    try:
        os.unlink(ledger_path)
    except FileNotFoundError:
        pass


def completed_steps(
    ledger_path: str | None = None,
) -> list[str]:
    """Return the list of completed step ids, in install order.  Empty if no ledger."""
    if ledger_path is None:
        ledger_path = _default_ledger_path()
    ledger = read_ledger(ledger_path)
    if ledger is None or ledger.status != "ok":
        return []
    steps = ledger.get("steps", {})
    return [sid for sid in INSTALL_STEPS if steps.get(sid, {}).get("status") == "complete"]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _validate_step(step_id: str) -> None:
    """Raise ValueError if *step_id* is not one of the known install steps."""
    if step_id not in _STEP_INDEX:
        raise ValueError(
            f"unknown step {step_id!r}; known steps: {', '.join(INSTALL_STEPS)}"
        )


def _require_ledger(ledger_path: str) -> dict[str, Any]:
    """Return the parsed ledger, or raise RuntimeError if none exists yet."""
    ledger = read_ledger(ledger_path)
    if ledger is None or ledger.status == "missing":
        raise RuntimeError(
            f"no install ledger at {ledger_path!r} — run 'start' first"
        )
    if ledger.status != "ok":
        raise RuntimeError(
            f"install ledger at {ledger_path!r} is {ledger.status}: {ledger.detail}"
        )
    return ledger


# ---------------------------------------------------------------------------
# Thin CLI — parse args, call ONE function, print JSON on stdout
# ---------------------------------------------------------------------------

_USAGE = """\
usage: install_ledger.py <command> [<arg>]

commands:
  check              print JSON state dict to stdout
  start              create a fresh ledger (idempotent)
  step-begin STEP    mark a step as started
  step-done STEP     mark a step as complete
  finish             mark the install as complete
  cancel             mark the install as cancelled
  reset              delete the ledger file
  completed-steps    print JSON array of completed step ids
  steps              print JSON array of all known step ids (in order)

exit codes: 0 on success (none/complete/cancelled), 1 for in_progress (check only), 2 on error
"""


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv

    if len(argv) < 2:
        print(_USAGE, end="", file=sys.stderr)
        return 2

    cmd = argv[1]
    ledger_path = os.environ.get("VOICE_LOOP_INSTALL_LEDGER", "")

    try:
        if cmd == "check":
            result = check_state(ledger_path or None)
            _print_json(result)
            state = result.get("state", "none")
            # Exit 0 for none/complete/cancelled (all are terminal states)
            # Exit 1 only for in_progress (install is mid-flight)
            return 0 if state in ("none", "complete", "cancelled") else 1

        elif cmd == "start":
            result = start_install(ledger_path or None)
            _print_json(result)
            return 0

        elif cmd == "step-begin":
            if len(argv) < 3:
                print("missing step id", file=sys.stderr)
                return 2
            result = step_begin(argv[2], ledger_path or None)
            _print_json(result)
            return 0

        elif cmd == "step-done":
            if len(argv) < 3:
                print("missing step id", file=sys.stderr)
                return 2
            result = step_done(argv[2], ledger_path or None)
            _print_json(result)
            return 0

        elif cmd == "finish":
            result = finish_install(ledger_path or None)
            _print_json(result)
            # D4: finish refuses when steps are incomplete — exit 2
            return 2 if result.get("success") is False else 0

        elif cmd == "cancel":
            result = cancel_install(ledger_path or None)
            _print_json(result)
            return 0

        elif cmd == "reset":
            reset_install(ledger_path or None)
            print("{}")
            return 0

        elif cmd == "completed-steps":
            steps = completed_steps(ledger_path or None)
            _print_json(steps)
            return 0

        elif cmd == "steps":
            _print_json(list(INSTALL_STEPS))
            return 0

        else:
            print(f"unknown command {cmd!r}", file=sys.stderr)
            print(_USAGE, end="", file=sys.stderr)
            return 2

    except (ValueError, RuntimeError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def _print_json(obj: object) -> None:
    """Write *obj* as one line of JSON to stdout."""
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, sort_keys=False) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
