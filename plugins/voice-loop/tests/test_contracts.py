"""Coverage pin for ``scripts/contracts.py`` (#156 C).

The shared bounded readers in ``contracts.py`` carry several decision branches the original
suite did not exercise — the ratcheting `scripts/*` floor at 100% (PR body for #233 / chore #156 C)
requires each reachable branch to be pinned. Three surfaces are tested here:

* ``resolve_number``'s four reject paths (non-finite float, below the minimum, above the maximum,
  and the value-error fallthrough that logs once and returns the safe default) — the function
  exists to coerce a setting's value into a typed number, and a regression that accepted an out-of-
  band value silently would pass the happy-path tests but is the failure mode the gate was added
  to catch.
* ``read_playing_pid``'s malformed-input branches: a record too long for the bounded read, a non-
  ASCII token stream, a bare ``pg`` token with nothing after it, and a ``pg`` token followed by a
  non-positive integer. The grammar the poller writes must survive anything a parallel writer
  produced; a regression that crashed on bad input would wedge the speaking lock on the next read.
* ``pid_looks_like_speak``'s unknown-platform fallthrough — the function is platform-aware, and a
  ``platform_id`` that names nothing in its dispatch table must return ``False`` rather than
  raising or reaching the cmdline substring check with a ``cmdline`` that was never assigned.

The ``_windows_process_is_live`` body is excluded by ``pragma: windows-only`` on its introducing
line and is not exercised here — the disclosure lives in ``plugins/voice-loop/TESTING.md``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# `scripts/` is on pythonpath via pytest.ini, and contracts is the one file in there that imports
# cleanly under that name. The four voice-loop scripts that depend on it (`dictate.py`,
# `contour_poll.py`, `tls-probe.py`, `doctor.py`) all use `import contracts` at runtime, so this
# test exercises the same import path.
import contracts


# --- resolve_number: the four reject paths -----------------------------------------------------


class TestResolveNumberRejectPaths:
    """The four branches that prove ``resolve_number`` REFUSES out-of-band inputs.

    Each branch is a separate gap that a regression would close silently: the happy path's existing
    tests cover only the value-accepted arm, so without these a refactor that removed the bound
    checks would still pass.
    """

    def test_a_non_finite_float_is_rejected_and_logged(self):
        """GAP (line 38): ``math.isfinite`` is the rejection gate for ``inf`` / ``nan``.

        A regression that accepted a NaN would silently let a misconfigured timeout pass through
        ``resolve_number`` and reach a comparison that is itself always False — every other gate
        would block but the timeout would never fire.
        """
        logged: list[str] = []

        def sink(message: str) -> None:
            logged.append(message)

        result = contracts.resolve_number(
            "inf", default=1.0, setting="debounce_ms", log=sink
        )

        assert result == 1.0
        assert logged == ["debounce_ms rejected 'inf' — using default 1.0"]

    def test_a_value_below_the_minimum_is_rejected_and_logged(self):
        """GAP (line 40): the minimum bound is enforced; ``valid = False`` then ``raise ValueError``."""

        logged: list[str] = []

        def sink(message: str) -> None:
            logged.append(message)

        result = contracts.resolve_number(
            "-3", default=0, setting="retries", log=sink, minimum=0, integer=True
        )

        assert result == 0
        assert logged == ["retries rejected '-3' — using default 0"]

    def test_a_value_above_the_maximum_is_rejected_and_logged(self):
        """GAP (line 42): the maximum bound is enforced; symmetric to the minimum branch above."""

        logged: list[str] = []

        def sink(message: str) -> None:
            logged.append(message)

        result = contracts.resolve_number(
            "10000", default=180, setting="timeout_s", log=sink, maximum=600
        )

        assert result == 180
        assert logged == ["timeout_s rejected '10000' — using default 180"]

    def test_a_type_error_is_caught_and_falls_through_to_the_default(self):
        """GAP (lines 46-48): the ``except (TypeError, ValueError, OverflowError)`` clause, the
        diagnostic log line, and the default-return line.

        A regression that REMOVED the ``try/except`` would let a malformed value propagate out of
        the function and crash the caller — the whole point of the helper is that the caller never
        sees a TypeError.
        """
        logged: list[str] = []

        def sink(message: str) -> None:
            logged.append(message)

        # ``int(value) if integer else float(value)`` raises TypeError on a list.
        result = contracts.resolve_number(
            [1, 2], default=42, setting="port", log=sink, integer=True
        )

        assert result == 42
        assert logged == ["port rejected [1, 2] — using default 42"]

    def test_resolve_number_accepts_an_in_range_value(self):
        """L3 two-way falsification: a regression that always-rejected would pass the tests above
        by itself; this one pins that in-range inputs are NOT rejected."""

        logged: list[str] = []

        def sink(message: str) -> None:
            logged.append(message)

        result = contracts.resolve_number(
            "200", default=180, setting="timeout_s", log=sink, minimum=0, maximum=600
        )

        assert result == 200.0
        assert logged == []


# --- read_playing_pid: malformed input branches ------------------------------------------------


class TestReadPlayingPidMalformedInput:
    """The bounded reader's malformed-input fallthroughs.

    The grammar is fixed by ``playing.pid``'s contract; the parser must be DEFENSIVE rather than
    crash on bad input, because the file is written by a process under stress.
    """

    def test_a_record_over_the_bounded_read_is_refused(self, tmp_path):
        """GAP (line 96): a record longer than ``MAX_STATE_BYTES`` returns ``None`` rather than
        raising or partially parsing.

        The bounded read is a sentinel-byte pattern: ``MAX_STATE_BYTES + 1`` bytes is read, and a
        length over the cap means the file is bigger than the contract allows.
        """
        path = tmp_path / "playing.pid"
        path.write_bytes(b"1234" + b" " * (contracts.MAX_STATE_BYTES + 4))

        assert contracts.read_playing_pid(str(path)) is None

    def test_a_non_ascii_token_stream_is_refused(self, tmp_path):
        """GAP (lines 99-100): a non-ASCII byte in the token stream raises ``UnicodeDecodeError``
        inside the decode call, and the ``except`` returns ``None`` rather than propagating."""

        path = tmp_path / "playing.pid"
        # The utf-8 byte 0xc3 is the leading byte of a multi-byte sequence; on its own it is
        # invalid utf-8 and triggers the UnicodeDecodeError.
        path.write_bytes(b"12\xc3\x00")

        assert contracts.read_playing_pid(str(path)) is None

    def test_a_bare_pg_token_with_nothing_after_it_is_skipped(self, tmp_path):
        """GAP (lines 108-109): ``pg`` at the end of the record advances past itself and continues
        the loop rather than raising ``IndexError`` on the next ``tokens[index + 1]`` read."""

        path = tmp_path / "playing.pid"
        # A pid (parsed cleanly) followed by a bare ``pg`` with no value after it.
        path.write_bytes(b"1234 pg")

        result = contracts.read_playing_pid(str(path))

        assert result == contracts.PlayingPid(pids=(1234,), pgids=())

    def test_a_pg_token_with_a_non_positive_value_is_skipped(self, tmp_path):
        """GAP (line 113): ``pg 0`` and ``pg -3`` are not real process-group ids and must be skipped
        rather than recorded in ``pgids``."""

        path = tmp_path / "playing.pid"
        path.write_bytes(b"pg 0 pg -3 pg 7")

        result = contracts.read_playing_pid(str(path))

        assert result == contracts.PlayingPid(pids=(), pgids=(7,))

    def test_read_playing_pid_returns_a_well_formed_record(self, tmp_path):
        """L3 two-way falsification: a regression that always-returned ``None`` would pass the
        refuse-bad-input tests above; this one pins the happy path."""

        path = tmp_path / "playing.pid"
        path.write_bytes(b"1234 pg 5678")

        result = contracts.read_playing_pid(str(path))

        assert result == contracts.PlayingPid(pids=(1234,), pgids=(5678,))

    def test_read_playing_pid_returns_none_for_an_absent_file(self, tmp_path):
        """L3 two-way falsification: the ``OSError`` branch (line 93) returns ``None`` for an
        unreadable / missing file."""

        path = tmp_path / "does-not-exist"

        assert contracts.read_playing_pid(str(path)) is None


# --- pid_looks_like_speak: unknown platform fallthrough ----------------------------------------


class TestPidLooksLikeSpeakUnknownPlatform:
    """The ``else: return False`` branch (line 181) is reached when ``platform_id`` is neither
    ``win32``, ``linux``, nor ``darwin``."""

    def test_unknown_platform_returns_false(self):
        """GAP (line 181): an unknown ``platform_id`` MUST return ``False`` rather than raise or
        reach the cmdline-substring check with an unbound ``cmdline``."""

        result = contracts.pid_looks_like_speak(
            pid=1234, platform_id="freebsd", read_cmdline=lambda _pid: "voice-loop-speak"
        )

        assert result is False

    def test_unknown_platform_does_not_call_read_cmdline(self):
        """L3 two-way falsification: a regression that called ``read_cmdline`` even on an unknown
        platform would silently invoke an arbitrary callable for an unhandled platform id."""

        called: list[int] = []

        def trap(pid: int) -> str | None:
            called.append(pid)
            return "voice-loop-speak"

        contracts.pid_looks_like_speak(
            pid=1234, platform_id="netbsd", read_cmdline=trap
        )

        assert called == []
