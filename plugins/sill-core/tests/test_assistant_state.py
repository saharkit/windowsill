"""Tests for sill_core.assistant_state — no network, no models, no hardware.

Acceptance criteria from the brief:
- concurrent-write test (two processes) never corrupts the file
- kill -9 mid-write leaves old or new state, never partial
- remind-once scenario test: choice made → one reminder → silence → explicit ask → allowed again
- schema-migration test (v1 file read by v2 code)
"""

from __future__ import annotations

import json
import multiprocessing
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from sill_core import assistant_state
from sill_core.assistant_state import (
    CURRENT_SCHEMA,
    AssistantState,
    _default_clock,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FROZEN_NOW = datetime(2026, 8, 2, 14, 30, 0, tzinfo=timezone.utc)


def frozen_clock() -> datetime:
    """A clock that always returns the same moment so timestamps are assertion-friendly."""
    return FROZEN_NOW


def json_on_disk(path: Path) -> dict[str, Any]:
    """Read and parse the state file, failing loudly if it is not valid JSON."""
    raw = path.read_text(encoding="utf-8")
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Basic read / write
# ---------------------------------------------------------------------------

class TestBasicReadWrite:
    """The store reads and writes a versioned JSON file at the expected path."""

    def test_path_is_under_state_home(self, tmp_path: Path) -> None:
        store = AssistantState("voice-loop", state_home=tmp_path, clock=frozen_clock)
        assert store.path == tmp_path / "voice-loop" / "assistant-state.json"

    def test_default_clock_returns_utc(self) -> None:
        now = _default_clock()
        assert now.tzinfo is timezone.utc

    def test_write_creates_file_with_schema(self, tmp_path: Path) -> None:
        store = AssistantState("test-plugin", state_home=tmp_path, clock=frozen_clock)
        store.reminder_mark_shown("paste-auto")
        data = json_on_disk(store.path)
        assert data["schema"] == CURRENT_SCHEMA
        assert "reminders" in data

    def test_read_empty_when_no_file(self, tmp_path: Path) -> None:
        store = AssistantState("no-file", state_home=tmp_path, clock=frozen_clock)
        assert not store.path.exists()
        assert store.read_full() == {}

    def test_read_handles_corrupt_json(self, tmp_path: Path) -> None:
        store = AssistantState("corrupt", state_home=tmp_path, clock=frozen_clock)
        store.path.parent.mkdir(parents=True, exist_ok=True)
        store.path.write_text("not valid json {{{", encoding="utf-8")
        # Should not raise — corrupt file treated as empty
        assert store.read_full() == {}
        assert store.reminder_should_show("any-key") is True

    def test_state_dir_created_on_write(self, tmp_path: Path) -> None:
        store = AssistantState("fresh", state_home=tmp_path, clock=frozen_clock)
        assert not store.path.parent.exists()
        store.reminder_mark_shown("key")
        assert store.path.parent.is_dir()


# ---------------------------------------------------------------------------
# Remind-once scenario
# ---------------------------------------------------------------------------

class TestRemindOnce:
    """The remind-once etiquette: show once → silent → explicit unmute → show again."""

    def test_never_shown_returns_true(self, tmp_path: Path) -> None:
        store = AssistantState("test", state_home=tmp_path, clock=frozen_clock)
        assert store.reminder_should_show("paste-auto") is True

    def test_after_mark_shown_returns_false(self, tmp_path: Path) -> None:
        store = AssistantState("test", state_home=tmp_path, clock=frozen_clock)
        store.reminder_mark_shown("paste-auto")
        assert store.reminder_should_show("paste-auto") is False

    def test_stays_muted_across_new_store_instance(self, tmp_path: Path) -> None:
        store = AssistantState("test", state_home=tmp_path, clock=frozen_clock)
        store.reminder_mark_shown("paste-auto")
        # A fresh instance reading the same file
        store2 = AssistantState("test", state_home=tmp_path, clock=frozen_clock)
        assert store2.reminder_should_show("paste-auto") is False

    def test_explicit_unmute_allows_again(self, tmp_path: Path) -> None:
        store = AssistantState("test", state_home=tmp_path, clock=frozen_clock)
        store.reminder_mark_shown("paste-auto")
        assert store.reminder_should_show("paste-auto") is False

        store.reminder_unmute("paste-auto")
        assert store.reminder_should_show("paste-auto") is True

    def test_full_remind_once_scenario(self, tmp_path: Path) -> None:
        """The acceptance scenario: choice made → one reminder → silence →
        explicit ask → reminder allowed again."""
        store = AssistantState("voice-loop", state_home=tmp_path, clock=frozen_clock)

        # User chose manual paste. Later, system thinks auto would help.
        assert store.reminder_should_show("paste-auto") is True

        # Reminder IS shown (simulated). Mark it.
        store.reminder_mark_shown("paste-auto", reason="user has been using manual paste for 3 sessions")

        # Now the system checks again — must stay silent.
        assert store.reminder_should_show("paste-auto") is False

        # Days pass, another check — still silent.
        store3 = AssistantState("voice-loop", state_home=tmp_path, clock=frozen_clock)
        assert store3.reminder_should_show("paste-auto") is False

        # User explicitly asks "remind me about paste mode again"
        store3.reminder_unmute("paste-auto")
        assert store3.reminder_should_show("paste-auto") is True

        # The record reflects what happened.
        record = store3.reminder_get("paste-auto")
        assert record is not None
        assert record["shown_at"] == FROZEN_NOW.isoformat()
        assert record["muted"] is False
        assert record["mute_reason"] == "explicitly unmuted"

    def test_unmute_nonexistent_key_is_noop(self, tmp_path: Path) -> None:
        store = AssistantState("test", state_home=tmp_path, clock=frozen_clock)
        # Should not raise
        store.reminder_unmute("never-set")
        assert store.reminder_get("never-set") is None

    def test_reminder_get_returns_none_for_unknown(self, tmp_path: Path) -> None:
        store = AssistantState("test", state_home=tmp_path, clock=frozen_clock)
        assert store.reminder_get("nope") is None

    def test_multiple_independent_reminders(self, tmp_path: Path) -> None:
        store = AssistantState("test", state_home=tmp_path, clock=frozen_clock)
        store.reminder_mark_shown("reminder-a")
        store.reminder_mark_shown("reminder-b")
        # Both muted
        assert store.reminder_should_show("reminder-a") is False
        assert store.reminder_should_show("reminder-b") is False
        # Unmute only one
        store.reminder_unmute("reminder-a")
        assert store.reminder_should_show("reminder-a") is True
        assert store.reminder_should_show("reminder-b") is False

    def test_reminder_record_shape(self, tmp_path: Path) -> None:
        """Reminder records contain only flags, counters, timestamps — never content."""
        store = AssistantState("test", state_home=tmp_path, clock=frozen_clock)
        store.reminder_mark_shown("key", reason="user chose manual paste")

        record = store.reminder_get("key")
        assert set(record.keys()) == {"shown_at", "muted", "mute_reason"}
        assert isinstance(record["shown_at"], str)
        assert isinstance(record["muted"], bool)
        assert isinstance(record["mute_reason"], str)


# ---------------------------------------------------------------------------
# kill -9 resilience (atomic write)
# ---------------------------------------------------------------------------

class TestAtomicWrite:
    """A crash mid-write leaves old state or new state, never a truncated file."""

    def test_os_replace_is_atomic(self, tmp_path: Path, monkeypatch) -> None:
        """The real writer leaves old state intact when replacement fails.

        Closes the mutation gap where replacing ``_write_atomic`` with an
        in-place truncating write would pass a test of a copied implementation.
        """
        store = AssistantState("test", state_home=tmp_path, clock=frozen_clock)
        store.reminder_mark_shown("first", reason="initial")

        replace_calls: list[tuple[str, str]] = []
        real_replace = os.replace

        def crashing_replace(src: str | os.PathLike, dst: str | os.PathLike) -> None:
            replace_calls.append((str(src), str(dst)))
            raise RuntimeError("simulated kill -9 before replace")

        monkeypatch.setattr(assistant_state.os, "replace", crashing_replace)
        with pytest.raises(RuntimeError, match="simulated kill -9"):
            store.reminder_mark_shown("second", reason="should never land")

        surviving = json_on_disk(store.path)
        assert surviving["reminders"]["first"]["muted"] is True
        assert "second" not in surviving["reminders"]
        assert len(replace_calls) == 1
        temp_path, target_path = replace_calls[0]
        assert Path(temp_path).parent == store._state_dir
        assert Path(target_path) == store.path
        assert not Path(temp_path).exists()
        assert real_replace is not assistant_state.os.replace

    def test_successful_write_replaces_old_state(self, tmp_path: Path) -> None:
        """Normal write path: the file is replaced atomically with new content."""
        store = AssistantState("test", state_home=tmp_path, clock=frozen_clock)
        store.reminder_mark_shown("old")
        old_data = json_on_disk(store.path)

        store.reminder_mark_shown("new", reason="second write")
        new_data = json_on_disk(store.path)

        # New state has both records (unknown "old" key? No — same namespace)
        assert "old" in new_data["reminders"]
        assert "new" in new_data["reminders"]
        # Schema version is stamped
        assert new_data["schema"] == CURRENT_SCHEMA

    def test_temp_file_not_left_behind_after_success(self, tmp_path: Path) -> None:
        """After a successful write, no .tmp files litter the state directory."""
        store = AssistantState("test", state_home=tmp_path, clock=frozen_clock)
        store.reminder_mark_shown("key")
        tmp_files = list(store._state_dir.glob(".assistant-state-*.tmp"))
        assert len(tmp_files) == 0

    def test_exception_cleans_up_temp_file(self, tmp_path: Path, monkeypatch) -> None:
        """When os.replace fails, the temp file is cleaned up and the exception re-raised."""
        store = AssistantState("test", state_home=tmp_path, clock=frozen_clock)

        # Let the first write succeed to create a baseline.
        store.reminder_mark_shown("baseline")

        # Now make os.replace fail with an OSError.
        def failing_replace(src: str, dst: str | os.PathLike) -> None:
            raise OSError("simulated disk full")

        monkeypatch.setattr(os, "replace", failing_replace)

        with pytest.raises(OSError, match="simulated disk full"):
            store.reminder_mark_shown("doomed")

        # The temp file must have been cleaned up.
        tmp_files = list(store._state_dir.glob(".assistant-state-*.tmp"))
        assert len(tmp_files) == 0

        # The old state is still intact.
        surviving = json_on_disk(store.path)
        assert surviving["reminders"]["baseline"]["muted"] is True

    def test_cleanup_oserror_is_suppressed(self, tmp_path: Path, monkeypatch) -> None:
        """When even the temp-file cleanup fails, the original exception is still raised."""
        store = AssistantState("test", state_home=tmp_path, clock=frozen_clock)
        store.reminder_mark_shown("baseline")

        # Make os.replace fail.
        def failing_replace(src: str, dst: str | os.PathLike) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(os, "replace", failing_replace)

        # Also make os.unlink fail so the except's inner try/except is exercised.
        def failing_unlink(path: str) -> None:
            raise PermissionError("cannot delete")

        monkeypatch.setattr(os, "unlink", failing_unlink)

        # The original OSError must still propagate.
        with pytest.raises(OSError, match="disk full"):
            store.reminder_mark_shown("doomed")


# ---------------------------------------------------------------------------
# Concurrent-write test (two processes)
# ---------------------------------------------------------------------------

class TestConcurrentWrite:
    """Two processes writing concurrently never corrupt the state file."""

    def test_two_writers_never_corrupt(self, tmp_path: Path) -> None:
        """Two processes hammer separate plugin namespaces; files must remain valid JSON."""
        state_dir = str(tmp_path)

        iterations = 50
        ctx = multiprocessing.get_context("spawn")
        result_queue: multiprocessing.Queue = ctx.Queue()

        p1 = ctx.Process(
            target=_writer_worker_per_plugin,
            args=(state_dir, iterations, 1, result_queue),
        )
        p2 = ctx.Process(
            target=_writer_worker_per_plugin,
            args=(state_dir, iterations, 2, result_queue),
        )
        p1.start()
        p2.start()
        p1.join(timeout=30)
        p2.join(timeout=30)

        # Neither process should still be alive.
        assert not p1.is_alive(), "worker 1 did not finish"
        assert not p2.is_alive(), "worker 2 did not finish"
        assert p1.exitcode == 0, f"worker 1 exited {p1.exitcode}"
        assert p2.exitcode == 0, f"worker 2 exited {p2.exitcode}"

        # Both workers reported success.
        results = []
        while not result_queue.empty():
            results.append(result_queue.get())
        assert len(results) == 2, f"expected 2 results, got {results}"
        for r in results:
            assert r[0] == "ok", f"worker reported: {r}"

        # Each worker's file must be valid JSON.
        for worker_id in (1, 2):
            wstore = AssistantState(f"concurrent-{worker_id}", state_home=tmp_path)
            assert wstore.path.exists()
            data = json_on_disk(wstore.path)
            assert isinstance(data, dict)
            assert data.get("schema") == CURRENT_SCHEMA

    def test_two_writers_same_plugin_file(self, tmp_path: Path) -> None:
        """Two processes writing to the SAME plugin name — the real race.

        Closes the mutation gap where read-modify-write has no lock and
        silently loses one worker's keys.

        Each process writes its own reminder keys; after both finish the file
        must be valid JSON containing keys from both workers.
        """
        ctx = multiprocessing.get_context("spawn")
        state_dir = str(tmp_path)

        p1 = ctx.Process(target=_writer_worker_same_plugin, args=(state_dir, 1))
        p2 = ctx.Process(target=_writer_worker_same_plugin, args=(state_dir, 2))
        p1.start()
        p2.start()
        p1.join(timeout=30)
        p2.join(timeout=30)

        assert p1.exitcode == 0, f"worker 1 exited {p1.exitcode}"
        assert p2.exitcode == 0, f"worker 2 exited {p2.exitcode}"

        store = AssistantState("shared-plugin", state_home=tmp_path)
        assert store.path.exists()
        data = json_on_disk(store.path)
        assert isinstance(data, dict)
        assert "reminders" in data
        assert data["schema"] == CURRENT_SCHEMA
        assert len(data["reminders"]) == 100
        assert all(
            f"worker{worker_id}-key{i}" in data["reminders"]
            for worker_id in (1, 2)
            for i in range(50)
        )


# ---------------------------------------------------------------------------
# Schema migration
# ---------------------------------------------------------------------------

class TestSchemaMigration:
    """A v1 file read by v2 code (CURRENT_SCHEMA bumped) is handled correctly."""

    def test_v1_file_read_by_v2_code(self, tmp_path: Path, monkeypatch) -> None:
        """Write a schema-1 file by hand, then read it as if CURRENT_SCHEMA were 2.

        The v2 code must:
        - read reminders from the v1 file correctly
        - preserve the v1 reminders on write
        - stamp the file with schema 2 on its next write
        """
        store = AssistantState("migrate", state_home=tmp_path, clock=frozen_clock)

        # Write a v1 file manually (as if an older version wrote it).
        store.path.parent.mkdir(parents=True, exist_ok=True)
        v1_state: dict[str, Any] = {
            "schema": 1,
            "reminders": {
                "old-reminder": {
                    "shown_at": "2025-01-15T10:00:00+00:00",
                    "muted": True,
                    "mute_reason": "shown once by v1",
                },
            },
            "step_ledger": {"install": "v1-checkpoint"},
        }
        store.path.write_text(json.dumps(v1_state, indent=2), encoding="utf-8")

        # Now bump the schema version to 2 (simulating a migration).
        monkeypatch.setattr(assistant_state, "CURRENT_SCHEMA", 2)

        # v2 code reads the v1 file — reminders must work.
        store2 = AssistantState("migrate", state_home=tmp_path, clock=frozen_clock)
        assert store2.reminder_should_show("old-reminder") is False
        assert store2.reminder_get("old-reminder") is not None
        assert store2.reminder_get("old-reminder")["mute_reason"] == "shown once by v1"

        # The full state still has the v1 keys.
        full = store2.read_full()
        assert full["step_ledger"]["install"] == "v1-checkpoint"
        assert full["schema"] == 1  # read from disk, not rewritten yet

        # v2 code writes a new reminder — the file must now carry schema 2
        # and preserve the v1 data.
        store2.reminder_mark_shown("new-reminder", reason="written by v2")
        data = json_on_disk(store2.path)
        assert data["schema"] == 2
        assert data["reminders"]["old-reminder"]["muted"] is True
        assert data["reminders"]["new-reminder"]["muted"] is True
        assert data["step_ledger"]["install"] == "v1-checkpoint"

    def test_v1_no_schema_key_is_tolerated(self, tmp_path: Path) -> None:
        """A file with no schema key at all (pre-versioning) is treated as empty."""
        store = AssistantState("legacy", state_home=tmp_path, clock=frozen_clock)
        store.path.parent.mkdir(parents=True, exist_ok=True)
        store.path.write_text('{"reminders": {"old": {"muted": true}}}', encoding="utf-8")

        store2 = AssistantState("legacy", state_home=tmp_path, clock=frozen_clock)
        # The legacy reminder is still readable.
        assert store2.reminder_get("old") is not None
        assert store2.reminder_get("old")["muted"] is True


# ---------------------------------------------------------------------------
# Unknown-key preservation
# ---------------------------------------------------------------------------

class TestUnknownKeyPreservation:
    """Keys the current code does not recognise are preserved across writes."""

    def test_future_schema_keys_survive_reminder_write(self, tmp_path: Path) -> None:
        """A key added by a future schema version survives a reminder write."""
        store = AssistantState("fwd", state_home=tmp_path, clock=frozen_clock)

        # Write initial state with an unknown key (future schema).
        store.merge({"reminders": {}, "future_feature": {"enabled": True, "version": 3}})

        # Now write a reminder through the normal API.
        store.reminder_mark_shown("test-key")

        data = json_on_disk(store.path)
        assert data["future_feature"] == {"enabled": True, "version": 3}
        assert "test-key" in data["reminders"]

    def test_future_schema_keys_survive_merge(self, tmp_path: Path) -> None:
        """merge() does not delete keys it doesn't mention."""
        store = AssistantState("fwd2", state_home=tmp_path, clock=frozen_clock)

        store.path.parent.mkdir(parents=True, exist_ok=True)
        store.path.write_text(
            json.dumps({
                "schema": 1,
                "reminders": {},
                "future_ns": {"flag": True},
            }),
            encoding="utf-8",
        )

        store.merge({"reminders": {"key": {"shown_at": "x", "muted": True, "mute_reason": "t"}}})
        data = json_on_disk(store.path)
        assert data["future_ns"] == {"flag": True}

    def test_merge_preserves_sibling_keys_in_owned_namespace(self, tmp_path: Path) -> None:
        """A second patch in one namespace must not erase its first field.

        Closes the mutation gap where ``merge`` assigns mapping namespaces
        wholesale instead of preserving sibling fields.
        """
        store = AssistantState("same-namespace", state_home=tmp_path, clock=frozen_clock)
        store.merge({"doctor": {"throttle_count": 3}})
        store.merge({"doctor": {"last_run": "2026-08-05T00:00:00+00:00"}})
        data = json_on_disk(store.path)
        assert data["doctor"] == {
            "throttle_count": 3,
            "last_run": "2026-08-05T00:00:00+00:00",
        }


# ---------------------------------------------------------------------------
# merge API
# ---------------------------------------------------------------------------

class TestMergeAPI:
    """The generic merge() method for other consumers."""

    def test_merge_sets_top_level_keys(self, tmp_path: Path) -> None:
        store = AssistantState("test", state_home=tmp_path, clock=frozen_clock)
        store.merge({"step_ledger": {"install": "done"}})
        data = json_on_disk(store.path)
        assert data["step_ledger"] == {"install": "done"}
        assert data["schema"] == CURRENT_SCHEMA

    def test_merge_preserves_unrelated_keys(self, tmp_path: Path) -> None:
        store = AssistantState("test", state_home=tmp_path, clock=frozen_clock)
        store.reminder_mark_shown("key-a")
        store.merge({"doctor": {"throttle_count": 3}})
        data = json_on_disk(store.path)
        assert "reminders" in data
        assert data["doctor"] == {"throttle_count": 3}

    def test_read_full_returns_copy(self, tmp_path: Path) -> None:
        store = AssistantState("test", state_home=tmp_path, clock=frozen_clock)
        store.reminder_mark_shown("key")
        full = store.read_full()
        assert isinstance(full, dict)
        # It's a copy — mutating it does not affect the store.
        full["extra"] = "noise"
        assert "extra" not in store.read_full()


# ---------------------------------------------------------------------------
# Environment / XDG
# ---------------------------------------------------------------------------

class TestEnvironment:
    """XDG_STATE_HOME is respected."""

    def test_xdg_state_home_env_var(self, tmp_path: Path, monkeypatch) -> None:
        custom = tmp_path / "custom-state"
        monkeypatch.setenv("XDG_STATE_HOME", str(custom))
        store = AssistantState("env-test", clock=frozen_clock)
        assert str(custom) in str(store.path)
        assert store.path.parent.parent == custom


# ---------------------------------------------------------------------------
# Module-level workers for multiprocessing (must be picklable)
# ---------------------------------------------------------------------------

def _writer_worker_per_plugin(
    state_dir: str,
    iterations: int,
    worker_id: int,
    result_queue: multiprocessing.Queue,
) -> None:
    """Target for the child process: write reminders in a tight loop to a
    per-worker plugin namespace."""
    store = AssistantState(
        f"concurrent-{worker_id}",
        state_home=Path(state_dir),
    )
    try:
        for i in range(iterations):
            store.reminder_mark_shown(
                f"w{worker_id}-r{i}",
                reason=f"worker {worker_id} round {i}",
            )
        result_queue.put(("ok", worker_id))
    except Exception as exc:
        result_queue.put(("error", worker_id, str(exc)))


def _writer_worker_same_plugin(state_dir: str, worker_id: int) -> None:
    """Target for the child process: write to a SHARED plugin namespace."""
    store = AssistantState(
        "shared-plugin",
        state_home=Path(state_dir),
    )
    for i in range(50):
        store.reminder_mark_shown(
            f"worker{worker_id}-key{i}",
            reason=f"worker {worker_id} round {i}",
        )


# ---------------------------------------------------------------------------
# kill -9 live process test
# ---------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform == "win32", reason="SIGKILL is not supported on Windows")
class TestKill9Live:
    """A real SIGKILL mid-write leaves the old state or new state intact.

    This test forks a child process that writes a large state dict, and sends
    SIGKILL at a random point during the write.  The parent then verifies the
    file is either the old state, the new state, or absent — never partial.
    """

    def test_kill9_leaves_valid_state(self, tmp_path: Path) -> None:
        """Fork a child, kill -9 it mid-write, verify the file is never corrupt."""
        state_dir = tmp_path / "kill9-test"
        state_dir.mkdir(parents=True)
        state_file = state_dir / "assistant-state.json"

        # Seed with old state.
        old_state = {
            "schema": 1,
            "reminders": {
                "old-key": {
                    "shown_at": "2026-01-01T00:00:00+00:00",
                    "muted": True,
                    "mute_reason": "old state",
                },
            },
        }
        state_file.write_text(json.dumps(old_state), encoding="utf-8")

        child_script = (
            f"""
import json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path.cwd()))
from sill_core import assistant_state
from sill_core.assistant_state import AssistantState

state_dir = {str(state_dir)!r}
store = AssistantState("", state_home=Path(state_dir))
real_dump = assistant_state.json.dump

def paused_dump(*args, **kwargs):
    real_dump(*args, **kwargs)
    print("READY", flush=True)
    time.sleep(60)

assistant_state.json.dump = paused_dump
store.merge({{"reminders": {{"new-key": {{"muted": True}}}}, "padding": "x" * 500000}})
"""
        )

        # Run the child.
        proc = subprocess.Popen(
            [sys.executable, "-c", child_script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        # Wait for the READY signal.
        ready = proc.stdout.readline() if proc.stdout else ""
        assert "READY" in ready, f"child did not signal ready: {ready}"

        # Kill the child before it does os.replace.
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)

        # The file MUST be the old state intact (the replace never happened).
        data = json_on_disk(state_file)
        assert data["schema"] == 1
        assert data["reminders"]["old-key"]["mute_reason"] == "old state"
        # The new key must NOT be present.
        assert "new-key" not in data.get("reminders", {})
        assert "padding" not in data

    def test_kill9_after_replace_leaves_new_state(self, tmp_path: Path) -> None:
        """If kill -9 lands AFTER os.replace, the new state is on disk, intact."""
        state_dir = tmp_path / "kill9-after"
        state_dir.mkdir(parents=True)
        state_file = state_dir / "assistant-state.json"

        old_state: dict[str, Any] = {"schema": 1, "reminders": {}}
        state_file.write_text(json.dumps(old_state), encoding="utf-8")

        child_script = (
            f"""
import time
from pathlib import Path
from sill_core import assistant_state
from sill_core.assistant_state import AssistantState

store = AssistantState("", state_home=Path({str(state_dir)!r}))
real_replace = assistant_state.os.replace

def replaced_then_pause(src, dst):
    real_replace(src, dst)
    print("REPLACED", flush=True)
    time.sleep(60)

assistant_state.os.replace = replaced_then_pause
store.merge({{"reminders": {{"new-key": {{"muted": True, "mute_reason": "new state"}}}}}})
"""
        )

        proc = subprocess.Popen(
            [sys.executable, "-c", child_script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        ready = proc.stdout.readline() if proc.stdout else ""
        assert "REPLACED" in ready, f"child did not signal replaced: {ready}"

        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)

        # The file MUST be the new state intact.
        data = json_on_disk(state_file)
        assert data["reminders"]["new-key"]["mute_reason"] == "new state"


# ---------------------------------------------------------------------------
# Stress: many rapid writes
# ---------------------------------------------------------------------------

class TestStress:
    """Many rapid writes from a single process — file stays valid."""

    def test_many_rapid_writes(self, tmp_path: Path) -> None:
        store = AssistantState("stress", state_home=tmp_path, clock=frozen_clock)
        for i in range(200):
            store.reminder_mark_shown(f"key-{i}", reason=f"round {i}")
        data = json_on_disk(store.path)
        assert data["schema"] == CURRENT_SCHEMA
        assert len(data["reminders"]) == 200
