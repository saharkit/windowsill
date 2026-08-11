"""Assistant state store — durable, designed state for plugins.

One JSON file per plugin under ``$XDG_STATE_HOME/<plugin>/assistant-state.json``.
Separate from config: config = user intent; state = assistant memory about the
user's session history.

Schema versioned (``schema`` key), unknown keys preserved on rewrite,
atomic write-then-replace (temp file, fsync, os.replace).

Consumers:
- voice-loop: remind-once etiquette, /doctor offer throttling
- #48: install lifecycle checkpoints
- /doctor (#57): read/write through this store

Pattern bricks consulted:
- ``injected-clock`` — injected ``clock: Callable[[], datetime]`` defaulting to
  UTC now; testable without sleep.
- ``atomic-replace-write`` — temp in same directory, fsync, os.replace; crash
  leaves old or new state, never partial.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

if sys.platform == "win32":          # pragma: no cover
    import msvcrt as _flock

    def _acquire_lock(lock_path: Path) -> int:
        """Acquire a blocking exclusive advisory lock, returning an fd to release."""
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
        try:
            _flock.locking(fd, _flock.LK_LOCK, 1)
        except BaseException:
            os.close(fd)
            raise
        return fd

    def _release_lock(fd: int) -> None:
        """Release the lock and close the fd."""
        try:
            _flock.locking(fd, _flock.LK_UNLCK, 1)
        finally:
            os.close(fd)

else:                                # pragma: no cover
    import fcntl as _flock

    def _acquire_lock(lock_path: Path) -> object:
        """Acquire a blocking exclusive advisory lock, returning a file object."""
        lock_file = lock_path.open("a+")
        _flock.flock(lock_file.fileno(), _flock.LOCK_EX)
        return lock_file

    def _release_lock(lock_handle: object) -> None:
        """Release the lock and close the file."""
        _flock.flock(lock_handle.fileno(), _flock.LOCK_UN)
        lock_handle.close()

CURRENT_SCHEMA = 1
"""The schema version stamped into every written state file.

Bumped when the on-disk format changes in a way older readers cannot ignore.
Readers MUST tolerate unknown keys (forward compat); writers MUST preserve
keys they do not recognise.
"""


def _default_clock() -> datetime:
    """Default clock: UTC now.  Injected so tests can pin time without sleeping."""
    return datetime.now(timezone.utc)


def _default_state_home() -> Path:
    """XDG state home for assistant state.

    Respects ``$XDG_STATE_HOME``; falls back to ``~/.local/state`` per the
    XDG Base Directory Specification.
    """
    xdg = os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state"))
    return Path(xdg)


class AssistantState:
    """Namespaced state store for one plugin.

    Reads and writes a single versioned JSON file.  Unknown keys (from a
    future schema version) are preserved across writes.  Every write is atomic
    (temp in the same directory, fsync, ``os.replace``) — a crash or ``kill -9``
    mid-write leaves the old state or the new, never a truncated partial file.

    All timestamps are recorded through an injected *clock* callable so tests
    can pin time without sleeping.  Content (the text of a reminder, the body
    of a diagnostic) is NEVER stored — only flags, counters and timestamps.

    Usage::

        store = AssistantState("voice-loop")
        if store.reminder_should_show("paste-auto"):
            show_reminder("You chose manual paste — want to switch to auto?")
            store.reminder_mark_shown("paste-auto")
    """

    def __init__(
        self,
        plugin_name: str,
        *,
        clock: Callable[[], datetime] | None = None,
        state_home: Path | None = None,
    ) -> None:
        self._plugin_name = plugin_name
        self._clock = clock if clock is not None else _default_clock
        _home = state_home if state_home is not None else _default_state_home()
        self._state_dir = _home / plugin_name
        self._path = self._state_dir / "assistant-state.json"

    # -- path (public for diagnostics) ---------------------------------------

    @property
    def path(self) -> Path:
        """Filesystem path of this plugin's state file."""
        return self._path

    # -- internal read / write -----------------------------------------------

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """Hold the per-plugin lock across a read-modify-write transaction."""
        self._state_dir.mkdir(parents=True, exist_ok=True)
        lock_path = self._state_dir / "assistant-state.lock"
        lock_handle = _acquire_lock(lock_path)
        try:
            yield
        finally:
            _release_lock(lock_handle)

    def _read(self) -> dict[str, Any]:
        """Read the current state dict from disk.

        Returns an empty dict when the file does not exist or is unparseable.
        The caller is responsible for populating ``schema`` on write.
        """
        try:
            raw = self._path.read_text(encoding="utf-8")
            return json.loads(raw)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _write_atomic(self, state: dict[str, Any]) -> None:
        """Atomically write *state* to the state file.

        Writes to a temp file beside the target, ``fsync``\\ s it, then
        ``os.replace``\\ s it into place — on the same filesystem the rename
        is atomic, so a reader always sees old bytes or new bytes, never a
        half-written file.

        The current ``CURRENT_SCHEMA`` version is stamped into *state* before
        writing.
        """
        state["schema"] = CURRENT_SCHEMA
        self._state_dir.mkdir(parents=True, exist_ok=True)

        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=str(self._state_dir),
            prefix=".assistant-state-",
            suffix=".tmp",
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                json.dump(state, fh, indent=2, ensure_ascii=False, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, self._path)
        except BaseException:
            # Best-effort cleanup — don't leave a temp file around
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    # -- reminder API --------------------------------------------------------

    def reminder_should_show(self, key: str) -> bool:
        """Return *True* if a reminder for *key* should be shown to the user.

        A reminder is shown **once** and then muted.  It stays muted until
        the user explicitly asks for it to be re-allowed.  A key that has
        never been recorded returns ``True`` (never shown → show it).
        """
        state = self._read()
        entry = state.get("reminders", {}).get(key)
        if entry is None:
            return True
        return not entry.get("muted", False)

    def reminder_mark_shown(self, key: str, *, reason: str = "") -> None:
        """Record that a reminder was shown, muting it for the future.

        Call this **after** the reminder has actually been displayed to the
        user.  The *reason* is recorded in the state for diagnostics (what
        triggered the reminder).
        """
        now = self._clock().isoformat()
        with self._locked():
            state = self._read()
            reminders: dict[str, Any] = state.setdefault("reminders", {})
            reminders[key] = {
                "shown_at": now,
                "muted": True,
                "mute_reason": reason or "shown once",
            }
            state["reminders"] = reminders
            self._write_atomic(state)

    def reminder_unmute(self, key: str) -> None:
        """Explicitly unmute a reminder so it may be shown again.

        Only call this on an **explicit user request** — never automatically.
        The mute reason is updated to record the unmute.
        """
        with self._locked():
            state = self._read()
            reminders: dict[str, Any] = state.setdefault("reminders", {})
            entry = reminders.get(key)
            if entry is not None:
                entry["muted"] = False
                entry["mute_reason"] = "explicitly unmuted"
                state["reminders"] = reminders
                self._write_atomic(state)

    def reminder_get(self, key: str) -> dict[str, Any] | None:
        """Return the reminder record for *key*, or *None* if it has never been set."""
        state = self._read()
        return state.get("reminders", {}).get(key)

    # -- generic read / merge API for other consumers -----------------------

    def read_full(self) -> dict[str, Any]:
        """Return a shallow copy of the complete state dict.

        Callers that need to migrate or inspect the whole state use this;
        callers that only need one key should use the specific accessors
        above.
        """
        return dict(self._read())

    def merge(self, patch: dict[str, Any]) -> None:
        """Merge *patch* into the current state and write atomically.

        Unknown keys already in the current state (e.g. from a future schema
        version) are **preserved** — only keys present in *patch* are updated.
        Mapping values are merged within their top-level namespace, so separate
        consumers can update fields owned by the same namespace without loss.
        This is the recommended write path for consumers like ``#48``'s
        step-ledger or ``/doctor``'s throttle counters that own a top-level
        namespace.

        Usage::

            store.merge({"step_ledger": {"install": "checkpoint-done"}})
        """
        with self._locked():
            state = self._read()
            for key, value in patch.items():
                current = state.get(key)
                if isinstance(current, dict) and isinstance(value, dict):
                    current.update(value)
                else:
                    state[key] = value
            self._write_atomic(state)
