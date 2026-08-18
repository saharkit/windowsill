"""Tests for the install step ledger — deterministic lifecycle, resume/restart/cancel.

No network, no models, no audio hardware — ever.  The ledger is a JSON file on disk;
these tests point it at ``tmp_path`` and inject a frozen clock so every timestamp is
predictable.
"""

from __future__ import annotations

import builtins
import importlib.util
import json
import os
import runpy
from datetime import datetime, timezone

import pytest

# The module under test lives in scripts/; add it to the path.
import sys

_scripts_dir = os.path.join(os.path.dirname(__file__), "..", "scripts")
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)

import install_ledger


# ---------------------------------------------------------------------------
# Frozen clock — predictable timestamps, no ambient reads
# ---------------------------------------------------------------------------


class FrozenClock:
    """A clock that returns the same moment every call, and can be advanced by hand."""

    def __init__(self, iso: str = "2026-08-05T12:00:00+00:00") -> None:
        self._dt = datetime.fromisoformat(iso)

    def __call__(self) -> datetime:
        return self._dt

    def advance(self, seconds: float = 1.0) -> None:
        from datetime import timedelta

        self._dt += timedelta(seconds=seconds)


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock()


@pytest.fixture
def ledger_path(tmp_path) -> str:
    return str(tmp_path / "install.ledger")


# ---------------------------------------------------------------------------
# Fresh start — no ledger exists
# ---------------------------------------------------------------------------


class TestFreshStart:
    def test_no_ledger_means_none(self, ledger_path):
        result = install_ledger.check_state(ledger_path)
        assert result == {"state": "none"}

    def test_completed_steps_empty_when_no_ledger(self, ledger_path):
        assert install_ledger.completed_steps(ledger_path) == []

    def test_reset_is_idempotent_when_no_ledger(self, tmp_path):
        # A genuinely fresh machine: not just no ledger — no state DIRECTORY either
        # (only tmp_path exists; "voice-loop" is never created by the fixture).  The
        # lock file is opened inside that directory before reset reaches its no-op
        # unlink, so open-before-makedirs fails right here instead of on the user's
        # first checkpoint.
        ledger_path = str(tmp_path / "voice-loop" / "install.ledger")
        install_ledger.reset_install(ledger_path)  # must not raise
        assert install_ledger.check_state(ledger_path)["state"] == "none"


# ---------------------------------------------------------------------------
# Start install
# ---------------------------------------------------------------------------


class TestPlatformAndDefaults:
    def test_default_clock_is_utc(self):
        now = install_ledger._default_clock()
        assert now.tzinfo is timezone.utc

    def test_default_path_uses_xdg_state_home(self, monkeypatch, tmp_path):
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        assert install_ledger._default_ledger_path() == str(tmp_path / "voice-loop" / "install.ledger")

    def test_default_path_falls_back_to_home(self, monkeypatch):
        monkeypatch.delenv("XDG_STATE_HOME", raising=False)
        # The expanduser stub is recorded so the test pins both the call AND the
        # path the code under test asked expanduser to expand — otherwise a
        # regression changing ~/.local/state to ~/.config would still pass.
        calls = []

        def record(path):
            calls.append(path)
            return "/home/tester/state"

        monkeypatch.setattr(install_ledger.os.path, "expanduser", record)
        expected = os.path.join("/home/tester/state", "voice-loop", "install.ledger")
        assert install_ledger._default_ledger_path() == expected
        assert calls == ["~/.local/state"]

    def test_optional_platform_imports_are_absent(self, monkeypatch):
        source = os.path.join(_scripts_dir, "install_ledger.py")
        real_import = builtins.__import__

        def without_platforms(name, *args, **kwargs):
            if name in {"fcntl", "msvcrt"}:
                raise ImportError(name)
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", without_platforms)
        spec = importlib.util.spec_from_file_location("ledger_no_platform", source)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert module.fcntl is None
        assert module.msvcrt is None

    def test_windows_lock_branch(self, monkeypatch, tmp_path):
        class FakeMsvcrt:
            LK_LOCK = 1
            calls = []

            @classmethod
            def locking(cls, fd, mode, length):
                cls.calls.append(("lock", fd, mode, length))

        class FakeFcntl:
            LOCK_EX = 1
            LOCK_UN = 2

            def flock(self, *args):
                raise AssertionError("fcntl branch must not run")

        monkeypatch.setattr(install_ledger, "fcntl", None)
        monkeypatch.setattr(install_ledger, "msvcrt", FakeMsvcrt)
        path = str(tmp_path / "ledger")
        with install_ledger._ledger_lock(path):
            assert os.path.exists(path + ".lock")
        assert [call[0] for call in FakeMsvcrt.calls] == ["lock"]

    def test_lock_directory_failure_is_best_effort(self, monkeypatch, tmp_path):
        path = str(tmp_path / "missing" / "ledger")
        real_makedirs = install_ledger.os.makedirs
        monkeypatch.setattr(install_ledger.os, "makedirs", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("blocked")))
        with pytest.raises(FileNotFoundError):
            with install_ledger._ledger_lock(path):
                pass
        monkeypatch.setattr(install_ledger.os, "makedirs", real_makedirs)

    def test_lock_unlock_and_close_failure_cleanup(self, monkeypatch, tmp_path):
        class FakeFcntl:
            LOCK_EX = 1
            LOCK_UN = 2
            calls = []

            @classmethod
            def flock(cls, fd, mode):
                cls.calls.append((fd, mode))

        monkeypatch.setattr(install_ledger, "fcntl", FakeFcntl)
        monkeypatch.setattr(install_ledger, "msvcrt", None)
        path = str(tmp_path / "ledger")
        with install_ledger._ledger_lock(path):
            pass
        assert [mode for _, mode in FakeFcntl.calls] == [FakeFcntl.LOCK_EX, FakeFcntl.LOCK_UN]

    def test_lock_without_platform_locking(self, monkeypatch, tmp_path):
        monkeypatch.setattr(install_ledger, "fcntl", None)
        monkeypatch.setattr(install_ledger, "msvcrt", None)
        path = str(tmp_path / "ledger")
        with install_ledger._ledger_lock(path):
            assert os.path.exists(path + ".lock")

    def test_atomic_write_rejects_symlink(self, ledger_path, tmp_path):
        target = tmp_path / "target"
        target.write_text("old")
        os.symlink(target, ledger_path)
        with pytest.raises(OSError, match="symlink"):
            install_ledger._atomic_write(ledger_path, "new")

    def test_lock_rejects_symlinked_lock_path(self, monkeypatch, tmp_path):
        # Sibling of test_atomic_write_rejects_symlink: the lock path is opened
        # with O_NOFOLLOW so an attacker can't redirect the lock to a chosen
        # file. A regression that drops the flag would let _ledger_lock follow
        # the symlink and create-or-open the target as a lockfile — the test
        # catches that by failing the open with OSError instead.
        target = tmp_path / "attacker_target"
        target.write_text("held by attacker")
        ledger = tmp_path / "ledger"
        lock = tmp_path / "ledger.lock"
        os.symlink(target, lock)
        monkeypatch.setattr(install_ledger, "fcntl", None)
        monkeypatch.setattr(install_ledger, "msvcrt", None)
        with pytest.raises(OSError):
            with install_ledger._ledger_lock(str(ledger)):
                pass

    def test_lock_symlink_pre_check_fires_before_open(self, monkeypatch, tmp_path):
        # The islink pre-check is the cross-platform defense — it rejects a
        # symlinked lock_path BEFORE os.open runs.  O_NOFOLLOW is the POSIX
        # hardening layered on top; on Windows it does not exist, so without
        # the pre-check the open FOLLOWS the symlink and redirects the lock
        # onto an attacker-chosen file.  On POSIX O_NOFOLLOW would mask the
        # absence of the pre-check by raising on its own, so this test stubs
        # os.open to assert it is NOT called — if the pre-check is dropped,
        # the stub is reached and the assertions fail.
        target = tmp_path / "attacker_target"
        target.write_text("held by attacker")
        lock = tmp_path / "ledger.lock"
        os.symlink(target, lock)
        monkeypatch.setattr(install_ledger, "fcntl", None)
        monkeypatch.setattr(install_ledger, "msvcrt", None)
        opened = []
        monkeypatch.setattr(
            os,
            "open",
            lambda path, flags, *args, **kwargs: opened.append((path, flags)) or -1,
        )
        with pytest.raises(OSError, match="symlink"):
            with install_ledger._ledger_lock(str(tmp_path / "ledger")):
                pass
        assert opened == [], (
            f"os.open must not run for a symlinked lock_path; saw calls={opened!r}"
        )


class TestStartInstall:
    def test_start_creates_ledger(self, ledger_path, clock):
        result = install_ledger.start_install(ledger_path, clock=clock)
        assert result["state"] == "in_progress"
        assert result["started_at"] == "2026-08-05T12:00:00+00:00"
        assert result["completed_steps"] == []
        assert os.path.exists(ledger_path)

    def test_start_is_idempotent(self, ledger_path, clock):
        first = install_ledger.start_install(ledger_path, clock=clock)
        clock.advance(10)
        second = install_ledger.start_install(ledger_path, clock=clock)
        # Timestamp unchanged — ledger was NOT re-created
        assert second["started_at"] == first["started_at"]

    def test_start_writes_all_steps_as_pending(self, ledger_path, clock):
        install_ledger.start_install(ledger_path, clock=clock)
        raw = install_ledger.read_ledger(ledger_path)
        assert raw is not None
        for sid in install_ledger.INSTALL_STEPS:
            assert raw["steps"][sid]["status"] == "pending"

    def test_start_uses_keyword_ledger_path_for_lock(self, tmp_path, clock):
        # _serialized_mutation resolves the path from kwargs.get('ledger_path')
        # when no positional arg is supplied — a regression that only handled
        # the positional branch would still pass every other start_install test
        # but break callers passing `ledger_path=` as a keyword.
        keyword_path = str(tmp_path / "keyword.ledger")
        install_ledger.start_install(ledger_path=keyword_path, clock=clock)
        # The lock path is the ledger path + ".lock"; if _serialized_mutation
        # resolved the wrong branch, the lock would sit elsewhere and the file
        # would never appear at this exact location.
        assert os.path.exists(keyword_path + ".lock")
        assert install_ledger.check_state(keyword_path)["state"] == "in_progress"


# ---------------------------------------------------------------------------
# Step tracking — begin, done, ordering
# ---------------------------------------------------------------------------


class TestStepTracking:
    def test_step_begin_and_done(self, ledger_path, clock):
        install_ledger.start_install(ledger_path, clock=clock)
        clock.advance(1)
        result = install_ledger.step_begin("step-0-probe", ledger_path, clock=clock)
        assert result["current_step"] == "step-0-probe"

        clock.advance(1)
        result = install_ledger.step_done("step-0-probe", ledger_path, clock=clock)
        assert result["completed_steps"] == ["step-0-probe"]
        assert "current_step" not in result

    def test_step_done_is_idempotent(self, ledger_path, clock):
        install_ledger.start_install(ledger_path, clock=clock)
        install_ledger.step_begin("step-0-probe", ledger_path, clock=clock)
        first = install_ledger.step_done("step-0-probe", ledger_path, clock=clock)
        clock.advance(5)
        second = install_ledger.step_done("step-0-probe", ledger_path, clock=clock)
        assert second["completed_steps"] == ["step-0-probe"]
        # Calling step_done twice is harmless and completed_at from first call survives
        assert second["completed_steps"] == first["completed_steps"]
        # Verify the ledger has the first timestamp (idempotent step_done)
        raw = install_ledger.read_ledger(ledger_path)
        assert raw is not None
        # step_done preserves the first call's completed_at (12:00:00 from step_begin's clock)
        assert raw["steps"]["step-0-probe"]["completed_at"] == "2026-08-05T12:00:00+00:00"

    def test_step_begin_on_already_complete_is_noop(self, ledger_path, clock):
        install_ledger.start_install(ledger_path, clock=clock)
        install_ledger.step_done("step-0-probe", ledger_path, clock=clock)
        clock.advance(5)
        result = install_ledger.step_begin("step-0-probe", ledger_path, clock=clock)
        assert result["completed_steps"] == ["step-0-probe"]
        assert "current_step" not in result

    def test_unknown_step_raises(self, ledger_path, clock):
        install_ledger.start_install(ledger_path, clock=clock)
        with pytest.raises(ValueError, match="unknown step"):
            install_ledger.step_begin("step-99-nope", ledger_path, clock=clock)
        with pytest.raises(ValueError, match="unknown step"):
            install_ledger.step_done("nonexistent", ledger_path, clock=clock)

    def test_step_begin_without_explicit_path_uses_default_path(self, monkeypatch, tmp_path):
        path = str(tmp_path / "ledger")
        fixed = datetime(2026, 8, 5, 12, tzinfo=timezone.utc)
        monkeypatch.setattr(install_ledger, "_default_ledger_path", lambda: path)
        monkeypatch.setattr(install_ledger, "_default_clock", lambda: fixed)
        install_ledger.start_install(path, clock=lambda: fixed)
        result = install_ledger.step_begin("step-0-probe")
        assert result["current_step"] == "step-0-probe"

    def test_step_begin_default_clock_branch(self, monkeypatch, tmp_path):
        path = str(tmp_path / "ledger")
        fixed = datetime(2026, 8, 5, 12, tzinfo=timezone.utc)
        monkeypatch.setattr(install_ledger, "_default_ledger_path", lambda: path)
        monkeypatch.setattr(install_ledger, "_default_clock", lambda: fixed)
        install_ledger.start_install(path, clock=lambda: fixed)
        install_ledger.step_begin("step-0-probe")
        assert install_ledger.read_ledger(path)["steps"]["step-0-probe"]["started_at"] == fixed.isoformat()

    def test_step_begin_without_start_raises(self, ledger_path, clock):
        with pytest.raises(RuntimeError, match="no install ledger"):
            install_ledger.step_begin("step-0-probe", ledger_path, clock=clock)

    def test_step_done_uses_default_path_and_clock(self, monkeypatch, tmp_path):
        path = str(tmp_path / "ledger")
        fixed = datetime(2026, 8, 5, 12, tzinfo=timezone.utc)
        monkeypatch.setattr(install_ledger, "_default_ledger_path", lambda: path)
        monkeypatch.setattr(install_ledger, "_default_clock", lambda: fixed)
        install_ledger.start_install(path, clock=lambda: fixed)
        result = install_ledger.step_done("step-0-probe")
        assert result["completed_steps"] == ["step-0-probe"]
        # Pin that the patched clock actually drove the timestamp — removing
        # the patch would change completed_at to "now" instead of fixed.
        assert install_ledger.read_ledger(path)["steps"]["step-0-probe"]["completed_at"] == fixed.isoformat()

    def test_step_done_without_start_raises(self, ledger_path, clock):
        with pytest.raises(RuntimeError, match="no install ledger"):
            install_ledger.step_done("step-0-probe", ledger_path, clock=clock)

    def test_completed_steps_in_order(self, ledger_path, clock):
        install_ledger.start_install(ledger_path, clock=clock)
        # Complete steps out of order — completed_steps returns in definition order
        install_ledger.step_done("step-2-backends", ledger_path, clock=clock)
        install_ledger.step_done("step-0-probe", ledger_path, clock=clock)
        install_ledger.step_done("step-1-language", ledger_path, clock=clock)
        steps = install_ledger.completed_steps(ledger_path)
        assert steps == ["step-0-probe", "step-1-language", "step-2-backends"]

    def test_next_step_after_partial(self, ledger_path, clock):
        install_ledger.start_install(ledger_path, clock=clock)
        install_ledger.step_done("step-0-probe", ledger_path, clock=clock)
        install_ledger.step_done("step-1-language", ledger_path, clock=clock)
        result = install_ledger.check_state(ledger_path)
        assert result["next_step"] == "step-2-backends"


# ---------------------------------------------------------------------------
# Interrupted state (the core scenario)
# ---------------------------------------------------------------------------


class TestInterruptedState:
    def test_interrupted_mid_install_shows_in_progress(self, ledger_path, clock):
        install_ledger.start_install(ledger_path, clock=clock)
        install_ledger.step_done("step-0-probe", ledger_path, clock=clock)
        install_ledger.step_done("step-1-language", ledger_path, clock=clock)
        install_ledger.step_begin("step-2-backends", ledger_path, clock=clock)
        # Simulate interruption — no more calls, just check state
        result = install_ledger.check_state(ledger_path)
        assert result["state"] == "in_progress"
        assert result["completed_steps"] == ["step-0-probe", "step-1-language"]
        assert result["current_step"] == "step-2-backends"
        assert result["next_step"] == "step-2-backends"  # was started but not done

    def test_interrupted_before_any_step(self, ledger_path, clock):
        install_ledger.start_install(ledger_path, clock=clock)
        result = install_ledger.check_state(ledger_path)
        assert result["state"] == "in_progress"
        assert result["completed_steps"] == []
        assert result["next_step"] == "step-0-probe"


# ---------------------------------------------------------------------------
# Resume path — continue from last completed step
# ---------------------------------------------------------------------------


class TestResumePath:
    def test_resume_continues_from_next_step(self, ledger_path, clock):
        # First run: complete steps 0 and 1, then get interrupted
        install_ledger.start_install(ledger_path, clock=clock)
        install_ledger.step_done("step-0-probe", ledger_path, clock=clock)
        install_ledger.step_done("step-1-language", ledger_path, clock=clock)
        # — interrupted —

        # Second run: detect state, resume
        state = install_ledger.check_state(ledger_path)
        assert state["state"] == "in_progress"
        assert state["next_step"] == "step-2-backends"

        # Resume: continue from step-2-backends
        clock.advance(10)
        install_ledger.step_done("step-2-backends", ledger_path, clock=clock)
        install_ledger.step_done("step-3-install-deps", ledger_path, clock=clock)
        result = install_ledger.check_state(ledger_path)
        assert result["completed_steps"] == [
            "step-0-probe", "step-1-language", "step-2-backends", "step-3-install-deps",
        ]

    def test_resume_after_full_interruption_skips_done_steps(self, ledger_path, clock):
        # First run completed 0, 1, 2; interrupted during 3
        install_ledger.start_install(ledger_path, clock=clock)
        for sid in ("step-0-probe", "step-1-language", "step-2-backends"):
            install_ledger.step_done(sid, ledger_path, clock=clock)
        install_ledger.step_begin("step-3-install-deps", ledger_path, clock=clock)
        # — interrupted mid-step-3 —

        # Re-entry: check shows step-3 was in progress
        state = install_ledger.check_state(ledger_path)
        assert state["current_step"] == "step-3-install-deps"
        # The skill sees this and re-runs step-3 from scratch
        # (the ledger's in_progress marker tells it the step might be partial)

        # Complete step-3 (the re-run succeeded)
        install_ledger.step_done("step-3-install-deps", ledger_path, clock=clock)
        assert install_ledger.completed_steps(ledger_path) == [
            "step-0-probe", "step-1-language", "step-2-backends", "step-3-install-deps",
        ]

    def test_resume_already_complete_is_noop(self, ledger_path, clock):
        # Complete the whole install
        install_ledger.start_install(ledger_path, clock=clock)
        for sid in install_ledger.INSTALL_STEPS:
            install_ledger.step_done(sid, ledger_path, clock=clock)
        install_ledger.finish_install(ledger_path, clock=clock)

        # Re-entry: detect complete state
        state = install_ledger.check_state(ledger_path)
        assert state["state"] == "complete"
        # The skill reports "already installed" and skips to verification


# ---------------------------------------------------------------------------
# Restart path — clean slate, undo partial state
# ---------------------------------------------------------------------------


class TestRestartPath:
    def test_completed_steps_uses_default_path(self, monkeypatch, tmp_path):
        path = str(tmp_path / "ledger")
        monkeypatch.setattr(install_ledger, "_default_ledger_path", lambda: path)
        assert install_ledger.completed_steps() == []

    def test_restart_deletes_ledger(self, ledger_path, clock):
        install_ledger.start_install(ledger_path, clock=clock)
        install_ledger.step_done("step-0-probe", ledger_path, clock=clock)
        install_ledger.step_done("step-1-language", ledger_path, clock=clock)

        # Restart: read what was done (for cleanup), then reset
        completed = install_ledger.completed_steps(ledger_path)
        assert completed == ["step-0-probe", "step-1-language"]

        install_ledger.reset_install(ledger_path)
        assert install_ledger.check_state(ledger_path)["state"] == "none"
        assert not os.path.exists(ledger_path)

        # Fresh start after restart
        install_ledger.start_install(ledger_path, clock=clock)
        assert install_ledger.completed_steps(ledger_path) == []

    def test_reset_uses_default_path(self, monkeypatch, tmp_path):
        path = str(tmp_path / "ledger")
        monkeypatch.setattr(install_ledger, "_default_ledger_path", lambda: path)
        with open(path, "w") as fh:
            fh.write("{}")
        install_ledger.reset_install()
        assert not os.path.exists(path)

    def test_restart_then_complete(self, ledger_path, clock):
        # First run: partial
        install_ledger.start_install(ledger_path, clock=clock)
        install_ledger.step_done("step-0-probe", ledger_path, clock=clock)
        # — user chooses restart —

        # Read completed for cleanup, then reset
        install_ledger.reset_install(ledger_path)

        # Fresh run
        clock.advance(60)
        install_ledger.start_install(ledger_path, clock=clock)
        for sid in install_ledger.INSTALL_STEPS:
            install_ledger.step_done(sid, ledger_path, clock=clock)
        install_ledger.finish_install(ledger_path, clock=clock)

        state = install_ledger.check_state(ledger_path)
        assert state["state"] == "complete"


# ---------------------------------------------------------------------------
# Cancel path — leave everything as-is and exit
# ---------------------------------------------------------------------------


class TestCancelPath:
    def test_cancel_marks_ledger_cancelled(self, ledger_path, clock):
        install_ledger.start_install(ledger_path, clock=clock)
        install_ledger.step_done("step-0-probe", ledger_path, clock=clock)

        result = install_ledger.cancel_install(ledger_path, clock=clock)
        assert result["state"] == "cancelled"
        assert result["completed_steps"] == ["step-0-probe"]
        assert "cancelled_at" in result

    def test_cancel_then_reentry_shows_cancelled(self, ledger_path, clock):
        install_ledger.start_install(ledger_path, clock=clock)
        install_ledger.cancel_install(ledger_path, clock=clock)

        state = install_ledger.check_state(ledger_path)
        assert state["state"] == "cancelled"

    def test_cancel_uses_default_path_and_clock(self, monkeypatch, tmp_path):
        path = str(tmp_path / "ledger")
        fixed = datetime(2026, 8, 5, 12, tzinfo=timezone.utc)
        monkeypatch.setattr(install_ledger, "_default_ledger_path", lambda: path)
        monkeypatch.setattr(install_ledger, "_default_clock", lambda: fixed)
        install_ledger.start_install(path, clock=lambda: fixed)
        result = install_ledger.cancel_install()
        assert result["state"] == "cancelled"
        # Pin that the patched clock drove the cancelled timestamp — without
        # this, removing the monkeypatch.setattr above would still pass.
        assert install_ledger.read_ledger(path)["cancelled_at"] == fixed.isoformat()

    def test_cancel_requires_ledger(self, ledger_path, clock):
        with pytest.raises(RuntimeError, match="no install ledger"):
            install_ledger.cancel_install(ledger_path, clock=clock)


# ---------------------------------------------------------------------------
# Complete install — idempotence
# ---------------------------------------------------------------------------


class TestCompleteInstall:
    def test_finish_marks_complete(self, ledger_path, clock):
        install_ledger.start_install(ledger_path, clock=clock)
        for sid in install_ledger.INSTALL_STEPS:
            install_ledger.step_done(sid, ledger_path, clock=clock)
        result = install_ledger.finish_install(ledger_path, clock=clock)
        assert result["state"] == "complete"
        assert "completed_at" in result
        assert result["completed_steps"] == list(install_ledger.INSTALL_STEPS)

    def test_finish_is_idempotent(self, ledger_path, clock):
        install_ledger.start_install(ledger_path, clock=clock)
        for sid in install_ledger.INSTALL_STEPS:
            install_ledger.step_done(sid, ledger_path, clock=clock)
        first = install_ledger.finish_install(ledger_path, clock=clock)
        clock.advance(10)
        second = install_ledger.finish_install(ledger_path, clock=clock)
        assert second["completed_at"] == first["completed_at"]

    def test_finish_uses_default_path_and_clock(self, monkeypatch, tmp_path):
        path = str(tmp_path / "ledger")
        fixed = datetime(2026, 8, 5, 12, tzinfo=timezone.utc)
        monkeypatch.setattr(install_ledger, "_default_ledger_path", lambda: path)
        monkeypatch.setattr(install_ledger, "_default_clock", lambda: fixed)
        install_ledger.start_install(path, clock=lambda: fixed)
        for sid in install_ledger.INSTALL_STEPS:
            install_ledger.step_done(sid, path, clock=lambda: fixed)
        result = install_ledger.finish_install()
        assert result["state"] == "complete"
        # Pin that the patched clock drove the completed timestamp — without
        # this, removing the monkeypatch.setattr above would still pass.
        assert result["completed_at"] == fixed.isoformat()

    def test_check_on_complete_returns_complete(self, ledger_path, clock):
        install_ledger.start_install(ledger_path, clock=clock)
        for sid in install_ledger.INSTALL_STEPS:
            install_ledger.step_done(sid, ledger_path, clock=clock)
        install_ledger.finish_install(ledger_path, clock=clock)
        assert install_ledger.check_state(ledger_path)["state"] == "complete"

    def test_full_lifecycle_timestamps(self, ledger_path, clock):
        install_ledger.start_install(ledger_path, clock=clock)
        assert install_ledger.check_state(ledger_path)["started_at"] == "2026-08-05T12:00:00+00:00"

        # D4: finish_install now requires all steps to be complete first —
        # a "full lifecycle" MUST walk every step.
        for sid in install_ledger.INSTALL_STEPS:
            install_ledger.step_done(sid, ledger_path, clock=clock)

        clock.advance(30)
        install_ledger.finish_install(ledger_path, clock=clock)
        assert install_ledger.check_state(ledger_path)["completed_at"] == "2026-08-05T12:00:30+00:00"


# ---------------------------------------------------------------------------
# Atomic write — a crash mid-write leaves the old content intact
# ---------------------------------------------------------------------------


class TestAtomicWrite:
    def test_atomic_write_cleanup_failure_is_ignored(self, ledger_path, monkeypatch):
        os.makedirs(os.path.dirname(ledger_path), exist_ok=True)
        real_unlink = os.unlink

        def failing_replace(src, dst):
            raise OSError("replace failed")

        def failing_unlink(path):
            if path != ledger_path:
                raise OSError("cleanup failed")
            return real_unlink(path)

        monkeypatch.setattr(os, "replace", failing_replace)
        monkeypatch.setattr(os, "unlink", failing_unlink)
        with pytest.raises(OSError, match="replace failed"):
            install_ledger._atomic_write(ledger_path, "content")
        assert not os.path.exists(ledger_path)

    def test_atomic_write_leaves_old_content_on_crash(self, ledger_path, clock, monkeypatch):
        install_ledger.start_install(ledger_path, clock=clock)
        orig = install_ledger.read_ledger(ledger_path)
        assert orig is not None
        assert orig["state"] == "in_progress"

        # Make os.replace fail to simulate a crash before the atomic swap
        real_replace = os.replace

        def _failing_replace(src, dst):
            raise OSError("simulated crash before replace")

        monkeypatch.setattr(os, "replace", _failing_replace)

        with pytest.raises(OSError, match="simulated crash"):
            install_ledger.step_done("step-0-probe", ledger_path, clock=clock)

        # The ledger on disk is STILL the original — not truncated, not half-written
        after = install_ledger.read_ledger(ledger_path)
        assert after is not None
        assert after["state"] == "in_progress"
        assert after["steps"]["step-0-probe"]["status"] == "pending"

    def test_atomic_write_survives_mid_write_crash(self, ledger_path, clock, monkeypatch):
        install_ledger.start_install(ledger_path, clock=clock)

        # Make the fdopen/write fail partway through
        orig_fdopen = os.fdopen

        def _failing_fdopen(fd, *args, **kwargs):
            fh = orig_fdopen(fd, *args, **kwargs)
            orig_write = fh.write

            def _write_once_then_crash(s):
                orig_write(s[: len(s) // 2])
                raise OSError("simulated mid-write crash")

            fh.write = _write_once_then_crash
            return fh

        monkeypatch.setattr(os, "fdopen", _failing_fdopen)

        with pytest.raises(OSError, match="simulated mid-write crash"):
            install_ledger.step_done("step-0-probe", ledger_path, clock=clock)

        # The ledger on disk is still the original
        after = install_ledger.read_ledger(ledger_path)
        assert after is not None
        assert after["state"] == "in_progress"
        assert after["steps"]["step-0-probe"]["status"] == "pending"


# ---------------------------------------------------------------------------
# CLI — thin, but every exit code matters
# ---------------------------------------------------------------------------


class TestCLI:
    def test_cli_default_ledger_path(self, monkeypatch, tmp_path, capsys):
        monkeypatch.delenv("VOICE_LOOP_INSTALL_LEDGER", raising=False)
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        rc = install_ledger.main(["install_ledger.py", "check"])
        assert rc == 0
        assert json.loads(capsys.readouterr().out) == {"state": "none"}

    def test_check_none_exit_0(self, ledger_path, monkeypatch):
        monkeypatch.setenv("VOICE_LOOP_INSTALL_LEDGER", ledger_path)
        rc = install_ledger.main(["install_ledger.py", "check"])
        assert rc == 0

    def test_check_in_progress_exit_1(self, ledger_path, clock, monkeypatch):
        monkeypatch.setenv("VOICE_LOOP_INSTALL_LEDGER", ledger_path)
        install_ledger.start_install(ledger_path, clock=clock)
        rc = install_ledger.main(["install_ledger.py", "check"])
        assert rc == 1

    def test_check_complete_exit_0(self, ledger_path, clock, monkeypatch):
        monkeypatch.setenv("VOICE_LOOP_INSTALL_LEDGER", ledger_path)
        install_ledger.start_install(ledger_path, clock=clock)
        for sid in install_ledger.INSTALL_STEPS:
            install_ledger.step_done(sid, ledger_path, clock=clock)
        install_ledger.finish_install(ledger_path, clock=clock)
        rc = install_ledger.main(["install_ledger.py", "check"])
        assert rc == 0

    def test_check_cancelled_is_terminal_exit_0(self, ledger_path, clock, monkeypatch):
        """Cancelled exits 0 because it is a terminal state, not an actionable one.

        The user deliberately chose "leave everything as-is and exit" — the install
        is stopped by choice, not interrupted.  There is no ``next_step`` to resume
        toward (cancelled is not ``in_progress``), and ``start_install`` auto-restarts
        from cancelled anyway.  Only ``in_progress`` (mid-flight, still actionable)
        exits 1; every other state — none, complete, cancelled — exits 0.
        """
        monkeypatch.setenv("VOICE_LOOP_INSTALL_LEDGER", ledger_path)
        install_ledger.start_install(ledger_path, clock=clock)
        install_ledger.cancel_install(ledger_path, clock=clock)
        rc = install_ledger.main(["install_ledger.py", "check"])
        assert rc == 0

    def test_start_and_step_flow(self, ledger_path, clock, monkeypatch, capsys):
        monkeypatch.setenv("VOICE_LOOP_INSTALL_LEDGER", ledger_path)

        rc = install_ledger.main(["install_ledger.py", "start"])
        assert rc == 0
        out = capsys.readouterr().out
        result = json.loads(out)
        assert result["state"] == "in_progress"

        rc = install_ledger.main(["install_ledger.py", "step-done", "step-0-probe"])
        assert rc == 0
        _ = capsys.readouterr().out  # consume the step-done output

        rc = install_ledger.main(["install_ledger.py", "completed-steps"])
        assert rc == 0
        out = capsys.readouterr().out
        assert json.loads(out) == ["step-0-probe"]

    def test_reset_and_recreate(self, ledger_path, clock, monkeypatch, capsys):
        monkeypatch.setenv("VOICE_LOOP_INSTALL_LEDGER", ledger_path)
        install_ledger.start_install(ledger_path, clock=clock)

        install_ledger.main(["install_ledger.py", "reset"])
        _ = capsys.readouterr().out  # consume reset output

        rc = install_ledger.main(["install_ledger.py", "check"])
        assert rc == 0
        out = capsys.readouterr().out
        assert json.loads(out)["state"] == "none"

    def test_unknown_command_exit_2(self, ledger_path, monkeypatch):
        monkeypatch.setenv("VOICE_LOOP_INSTALL_LEDGER", ledger_path)
        rc = install_ledger.main(["install_ledger.py", "bogus"])
        assert rc == 2

    def test_step_begin_missing_arg(self, ledger_path, monkeypatch):
        monkeypatch.setenv("VOICE_LOOP_INSTALL_LEDGER", ledger_path)
        rc = install_ledger.main(["install_ledger.py", "step-begin"])
        assert rc == 2

    def test_step_done_missing_arg(self, ledger_path, monkeypatch):
        monkeypatch.setenv("VOICE_LOOP_INSTALL_LEDGER", ledger_path)
        rc = install_ledger.main(["install_ledger.py", "step-done"])
        assert rc == 2

    def test_steps_command_lists_all(self, ledger_path, monkeypatch, capsys):
        monkeypatch.setenv("VOICE_LOOP_INSTALL_LEDGER", ledger_path)
        rc = install_ledger.main(["install_ledger.py", "steps"])
        assert rc == 0
        out = capsys.readouterr().out
        assert json.loads(out) == list(install_ledger.INSTALL_STEPS)

    def test_no_args_exit_2(self):
        rc = install_ledger.main(["install_ledger.py"])
        assert rc == 2

    def test_step_begin_cli_success(self, ledger_path, monkeypatch, capsys):
        monkeypatch.setenv("VOICE_LOOP_INSTALL_LEDGER", ledger_path)
        install_ledger.main(["install_ledger.py", "start"])
        capsys.readouterr()
        rc = install_ledger.main(["install_ledger.py", "step-begin", "step-0-probe"])
        assert rc == 0
        assert json.loads(capsys.readouterr().out)["current_step"] == "step-0-probe"

    def test_finish_cli_incomplete_exits_2(self, ledger_path, monkeypatch, capsys):
        monkeypatch.setenv("VOICE_LOOP_INSTALL_LEDGER", ledger_path)
        install_ledger.main(["install_ledger.py", "start"])
        capsys.readouterr()
        rc = install_ledger.main(["install_ledger.py", "finish"])
        assert rc == 2
        assert json.loads(capsys.readouterr().out)["success"] is False

    def test_cancel_cli_success(self, ledger_path, monkeypatch, capsys):
        monkeypatch.setenv("VOICE_LOOP_INSTALL_LEDGER", ledger_path)
        install_ledger.main(["install_ledger.py", "start"])
        capsys.readouterr()
        rc = install_ledger.main(["install_ledger.py", "cancel"])
        assert rc == 0
        assert json.loads(capsys.readouterr().out)["state"] == "cancelled"

    def test_main_module_execution_exits(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["install_ledger.py"])
        with pytest.raises(SystemExit, match="2"):
            runpy.run_module("install_ledger", run_name="__main__")
        assert "usage:" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# The interrupted-then-resume scenario (acceptance test from the brief)
# ---------------------------------------------------------------------------


class TestAcceptanceInterruptedThenResume:
    """Verify check_state returns correct state for interrupted installs (re-entry behavior)"""

    def test_interrupted_at_every_boundary_then_resume(self, ledger_path, clock):
        # Simulate: run → interrupt at step boundary → re-run → resume → interrupt → ...
        install_ledger.start_install(ledger_path, clock=clock)
        for sid in install_ledger.INSTALL_STEPS:
            # Complete the step
            install_ledger.step_done(sid, ledger_path, clock=clock)
            # Check: state is in_progress with this step done
            state = install_ledger.check_state(ledger_path)
            assert state["state"] == "in_progress"
            assert sid in state["completed_steps"]

        # All steps done
        install_ledger.finish_install(ledger_path, clock=clock)
        assert install_ledger.check_state(ledger_path)["state"] == "complete"

    def test_interrupted_mid_step_then_resume(self, ledger_path, clock):
        # Step 3 (install deps) is the heaviest — interrupt mid-step
        install_ledger.start_install(ledger_path, clock=clock)
        install_ledger.step_done("step-0-probe", ledger_path, clock=clock)
        install_ledger.step_done("step-1-language", ledger_path, clock=clock)
        install_ledger.step_done("step-2-backends", ledger_path, clock=clock)
        install_ledger.step_begin("step-3-install-deps", ledger_path, clock=clock)

        # — INTERRUPTED mid-step-3 (e.g. Ctrl-C during pip install) —

        # Re-entry: check state
        state = install_ledger.check_state(ledger_path)
        assert state["state"] == "in_progress"
        assert state["completed_steps"] == ["step-0-probe", "step-1-language", "step-2-backends"]
        assert state["current_step"] == "step-3-install-deps"
        assert state["next_step"] == "step-3-install-deps"  # was in progress, not done

        # User chooses RESUME — the skill re-runs step-3 from scratch
        # (the ledger's in_progress marker tells it the step might be partial)
        # Re-run step-3 succeeds:
        install_ledger.step_done("step-3-install-deps", ledger_path, clock=clock)

        # Continue through remaining steps
        for sid in ("step-4-write-config", "step-5-paste-behaviour",
                    "step-6-hotkey", "step-7-speak-convention", "step-8-verify"):
            install_ledger.step_done(sid, ledger_path, clock=clock)

        install_ledger.finish_install(ledger_path, clock=clock)
        assert install_ledger.check_state(ledger_path)["state"] == "complete"


# ---------------------------------------------------------------------------
# The interrupted-then-restart scenario (acceptance test from the brief)
# ---------------------------------------------------------------------------


class TestAcceptanceInterruptedThenRestart:
    """kill the installer → re-run offers 3 choices → user picks restart"""

    def test_interrupted_then_restart_full_cycle(self, ledger_path, clock):
        # First run: get through probe and language, then interrupt
        install_ledger.start_install(ledger_path, clock=clock)
        install_ledger.step_done("step-0-probe", ledger_path, clock=clock)
        install_ledger.step_done("step-1-language", ledger_path, clock=clock)

        # — INTERRUPTED —

        # Re-entry: check state
        state = install_ledger.check_state(ledger_path)
        assert state["state"] == "in_progress"
        assert state["completed_steps"] == ["step-0-probe", "step-1-language"]

        # User chooses RESTART:
        # 1. Read what was done (for undo)
        completed_before = install_ledger.completed_steps(ledger_path)
        assert completed_before == ["step-0-probe", "step-1-language"]

        # 2. Undo partial state (skill's responsibility, guided by completed_before)
        #    e.g. remove config dir if config was written, disable service if started

        # 3. Reset the ledger
        install_ledger.reset_install(ledger_path)
        assert install_ledger.check_state(ledger_path)["state"] == "none"

        # 4. Fresh start
        clock.advance(120)
        install_ledger.start_install(ledger_path, clock=clock)
        assert install_ledger.completed_steps(ledger_path) == []
        assert install_ledger.check_state(ledger_path)["started_at"] == "2026-08-05T12:02:00+00:00"

        # Complete the fresh run
        for sid in install_ledger.INSTALL_STEPS:
            install_ledger.step_done(sid, ledger_path, clock=clock)
        install_ledger.finish_install(ledger_path, clock=clock)
        assert install_ledger.check_state(ledger_path)["state"] == "complete"


# ---------------------------------------------------------------------------
# Robustness — unreadable ledger, non-JSON ledger
# ---------------------------------------------------------------------------


class TestRobustness:
    def test_unreadable_ledger_reports_os_error(self, ledger_path, monkeypatch):
        real_open = builtins.open

        def blocked(path, *args, **kwargs):
            if path == ledger_path:
                raise PermissionError("denied")
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", blocked)
        result = install_ledger.read_ledger(ledger_path)
        assert result.status == "unreadable"
        assert result.detail.startswith("PermissionError:")

    def test_non_object_json_is_malformed(self, ledger_path):
        os.makedirs(os.path.dirname(ledger_path), exist_ok=True)
        with open(ledger_path, "w") as fh:
            json.dump([], fh)
        result = install_ledger.read_ledger(ledger_path)
        assert result.status == "malformed"
        assert result.detail == "ledger must be a JSON object"

    def test_check_state_preserves_ledger_without_optional_timestamps(self, ledger_path):
        os.makedirs(os.path.dirname(ledger_path), exist_ok=True)
        with open(ledger_path, "w") as fh:
            json.dump({"state": "complete", "steps": {}}, fh)
        assert install_ledger.check_state(ledger_path) == {
            "state": "complete",
            "completed_steps": [],
        }

    def test_invalid_state_is_malformed(self, ledger_path):
        os.makedirs(os.path.dirname(ledger_path), exist_ok=True)
        with open(ledger_path, "w") as fh:
            json.dump({"state": "broken", "steps": {}}, fh)
        result = install_ledger.read_ledger(ledger_path)
        assert result.status == "malformed"
        assert "invalid ledger state" in result.detail

    def test_steps_with_non_object_entry_are_malformed(self, ledger_path):
        os.makedirs(os.path.dirname(ledger_path), exist_ok=True)
        with open(ledger_path, "w") as fh:
            json.dump({"state": "none", "steps": {"step-0-probe": []}}, fh)
        result = install_ledger.read_ledger(ledger_path)
        assert result.status == "malformed"
        assert result.detail == "ledger steps must be an object of objects"

    def test_corrupt_ledger_treated_as_none(self, ledger_path):
        ledger_path.encode()  # ensure it's a string
        os.makedirs(os.path.dirname(ledger_path), exist_ok=True)
        with open(ledger_path, "w") as fh:
            fh.write("this is not json{{{")
        result = install_ledger.check_state(ledger_path)
        assert result["state"] == "none"
        assert result["read_status"] == "malformed"
        assert "JSONDecodeError" in result["read_detail"]

    def test_empty_ledger_file_treated_as_malformed(self, ledger_path):
        os.makedirs(os.path.dirname(ledger_path), exist_ok=True)
        with open(ledger_path, "w") as fh:
            fh.write("")
        result = install_ledger.check_state(ledger_path)
        assert result["state"] == "none"
        assert result["read_status"] == "malformed"
        assert "JSONDecodeError" in result["read_detail"]

    @pytest.mark.parametrize("state", [[], {}])
    def test_non_string_state_is_classified_as_malformed(self, ledger_path, state):
        os.makedirs(os.path.dirname(ledger_path), exist_ok=True)
        with open(ledger_path, "w") as fh:
            json.dump({"state": state, "steps": {}}, fh)
        result = install_ledger.check_state(ledger_path)
        assert result["state"] == "none"
        assert result["read_status"] == "malformed"
        assert "invalid ledger state" in result["read_detail"]


# ---------------------------------------------------------------------------
# Size bound — a ledger past 1 MiB is classified, never parsed
# ---------------------------------------------------------------------------


class TestLedgerSizeBound:
    """The read is bounded DURING reading: at most LEDGER_MAX_BYTES + 1 bytes are
    ever pulled off disk, and a file past the bound gets a named status instead of
    being handed to json.loads.
    """

    @staticmethod
    def _write(path: str, payload: bytes) -> None:
        with open(path, "wb") as fh:
            fh.write(payload)

    def test_oversized_ledger_is_classified_not_parsed(self, ledger_path):
        # Valid JSON padded past the bound with trailing spaces (legal JSON
        # whitespace).  Mutation gap: without the size check this file parses as a
        # perfectly ok ledger, so `start` would overwrite it — the padding is what
        # makes the bound observable.
        payload = b'{"state": "in_progress", "steps": {}}'
        payload += b" " * (install_ledger.LEDGER_MAX_BYTES + 1)
        self._write(ledger_path, payload)
        result = install_ledger.read_ledger(ledger_path)
        assert result is not None
        assert result.status == "oversized"
        assert str(install_ledger.LEDGER_MAX_BYTES) in result.detail

    def test_ledger_exactly_at_the_bound_still_reads(self, ledger_path):
        # The bound is exclusive: a ledger of exactly LEDGER_MAX_BYTES bytes is a
        # ledger.  Mutation gap: an off-by-one (`>=` instead of `>`) would classify
        # this healthy file as oversized.
        payload = b'{"state": "in_progress", "steps": {}}'
        payload += b" " * (install_ledger.LEDGER_MAX_BYTES - len(payload))
        assert len(payload) == install_ledger.LEDGER_MAX_BYTES
        self._write(ledger_path, payload)
        result = install_ledger.read_ledger(ledger_path)
        assert result is not None
        assert result.status == "ok"

    def test_check_reports_oversized_as_none_with_read_status(self, ledger_path):
        # Composition with the skill's entry flow: an oversized ledger keeps the
        # closed CLI contract (state "none" + read_status), so `check` still exits 0
        # and SKILL.md's guard branch keeps parsing it.
        self._write(ledger_path, b"{\"state\": \"in_progress\", \"steps\": {}} " * 40_000)
        result = install_ledger.check_state(ledger_path)
        assert result["state"] == "none"
        assert result["read_status"] == "oversized"
        assert "read_detail" in result


# ---------------------------------------------------------------------------
# A damaged ledger is evidence — `start` must not overwrite it
# ---------------------------------------------------------------------------


class TestStartRefusesDamagedLedger:
    """`start` is the one writer that used to fall through on any non-ok read status
    and recreate the ledger, destroying the only record of what the interrupted
    install did.  These tests pin the refusal (the CLI maps the RuntimeError to the
    module's error shape and exit 2), the untouched file, and `reset` as the sole
    way onward.
    """

    @staticmethod
    def _damaged_payload(kind: str) -> bytes:
        if kind == "oversized":
            return b'{"state": "in_progress", "steps": {}}' + b" " * (
                install_ledger.LEDGER_MAX_BYTES + 1
            )
        return b"{broken"

    @pytest.mark.parametrize("kind", ["malformed", "oversized"])
    def test_start_refuses_and_leaves_file_byte_identical(self, ledger_path, clock, kind):
        payload = self._damaged_payload(kind)
        with open(ledger_path, "wb") as fh:
            fh.write(payload)
        with pytest.raises(RuntimeError, match=f"read_status={kind}") as exc_info:
            install_ledger.start_install(ledger_path, clock=clock)
        assert "read_detail=" in str(exc_info.value)
        assert "reset" in str(exc_info.value)
        with open(ledger_path, "rb") as fh:
            assert fh.read() == payload  # the evidence survived, byte for byte

    def test_require_ledger_refuses_damaged_ledger(self, ledger_path):
        os.makedirs(os.path.dirname(ledger_path), exist_ok=True)
        with open(ledger_path, "w") as fh:
            fh.write("{broken")
        with pytest.raises(RuntimeError, match="is malformed"):
            install_ledger._require_ledger(ledger_path)

    def test_start_refuses_unreadable_ledger(self, ledger_path):
        # A directory where the ledger should be forces the unreadable read status.
        # Mutation gap: without the refusal the call raises a bare OSError from
        # os.replace — the read_status=unreadable message is what distinguishes a
        # deliberate refusal from an incidental write failure.
        os.mkdir(ledger_path)
        with pytest.raises(RuntimeError, match="read_status=unreadable"):
            install_ledger.start_install(ledger_path)
        assert os.path.isdir(ledger_path)  # nothing replaced it with a file

    def test_recovery_is_explicit_reset_then_start(self, ledger_path, clock):
        with open(ledger_path, "w") as fh:
            fh.write("{broken")
        with pytest.raises(RuntimeError):
            install_ledger.start_install(ledger_path, clock=clock)
        install_ledger.reset_install(ledger_path)
        result = install_ledger.start_install(ledger_path, clock=clock)
        assert result["state"] == "in_progress"

    def test_start_auto_restarts_cancelled_ledger(self, ledger_path, clock):
        install_ledger.start_install(ledger_path, clock=clock)
        install_ledger.cancel_install(ledger_path, clock=clock)
        clock.advance(10)
        result = install_ledger.start_install(ledger_path, clock=clock)
        assert result["state"] == "in_progress"
        assert result["started_at"] == "2026-08-05T12:00:10+00:00"

    def test_start_uses_default_path_and_clock_when_not_injected(self, monkeypatch, tmp_path):
        fixed = datetime(2026, 8, 5, 12, tzinfo=timezone.utc)
        path = str(tmp_path / "ledger")
        monkeypatch.setattr(install_ledger, "_default_ledger_path", lambda: path)
        monkeypatch.setattr(install_ledger, "_default_clock", lambda: fixed)
        result = install_ledger.start_install()
        assert result["started_at"] == fixed.isoformat()

    def test_write_ledger_uses_default_clock(self, ledger_path, monkeypatch):
        fixed = datetime(2026, 8, 5, 12, tzinfo=timezone.utc)
        monkeypatch.setattr(install_ledger, "_default_clock", lambda: fixed)
        os.makedirs(os.path.dirname(ledger_path), exist_ok=True)
        install_ledger.write_ledger(ledger_path, {"state": "none", "steps": {}})
        assert install_ledger.read_ledger(ledger_path)["updated_at"] == fixed.isoformat()

    def test_cli_start_exits_2_with_error_shape(self, ledger_path, monkeypatch, capsys):
        # Composition: the refusal reaches the operator through the module's existing
        # error shape — one "error: ..." line on stderr naming the read verdict.
        monkeypatch.setenv("VOICE_LOOP_INSTALL_LEDGER", ledger_path)
        with open(ledger_path, "w") as fh:
            fh.write("{broken")
        rc = install_ledger.main(["install_ledger.py", "start"])
        assert rc == 2
        err = capsys.readouterr().err
        assert err.startswith("error:")
        assert "read_status=malformed" in err
        assert "read_detail=" in err
