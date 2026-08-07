"""Tests for the install step ledger — deterministic lifecycle, resume/restart/cancel.

No network, no models, no audio hardware — ever.  The ledger is a JSON file on disk;
these tests point it at ``tmp_path`` and inject a frozen clock so every timestamp is
predictable.
"""

from __future__ import annotations

import json
import os
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

    def test_reset_is_idempotent_when_no_ledger(self, ledger_path):
        install_ledger.reset_install(ledger_path)  # must not raise
        assert install_ledger.check_state(ledger_path)["state"] == "none"


# ---------------------------------------------------------------------------
# Start install
# ---------------------------------------------------------------------------


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

    def test_step_begin_without_start_raises(self, ledger_path, clock):
        with pytest.raises(RuntimeError, match="no install ledger"):
            install_ledger.step_begin("step-0-probe", ledger_path, clock=clock)

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

    def test_check_on_complete_returns_complete(self, ledger_path, clock):
        install_ledger.start_install(ledger_path, clock=clock)
        for sid in install_ledger.INSTALL_STEPS:
            install_ledger.step_done(sid, ledger_path, clock=clock)
        install_ledger.finish_install(ledger_path, clock=clock)
        assert install_ledger.check_state(ledger_path)["state"] == "complete"

    def test_full_lifecycle_timestamps(self, ledger_path, clock):
        install_ledger.start_install(ledger_path, clock=clock)
        assert install_ledger.check_state(ledger_path)["started_at"] == "2026-08-05T12:00:00+00:00"

        clock.advance(30)
        install_ledger.finish_install(ledger_path, clock=clock)
        assert install_ledger.check_state(ledger_path)["completed_at"] == "2026-08-05T12:00:30+00:00"


# ---------------------------------------------------------------------------
# Atomic write — a crash mid-write leaves the old content intact
# ---------------------------------------------------------------------------


class TestAtomicWrite:
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
    def test_corrupt_ledger_treated_as_none(self, ledger_path):
        ledger_path.encode()  # ensure it's a string
        os.makedirs(os.path.dirname(ledger_path), exist_ok=True)
        with open(ledger_path, "w") as fh:
            fh.write("this is not json{{{")
        assert install_ledger.check_state(ledger_path)["state"] == "none"

    def test_empty_ledger_file_treated_as_none(self, ledger_path):
        os.makedirs(os.path.dirname(ledger_path), exist_ok=True)
        with open(ledger_path, "w") as fh:
            fh.write("")
        assert install_ledger.check_state(ledger_path)["state"] == "none"
