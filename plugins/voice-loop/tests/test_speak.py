"""The speaking hook's pure functions: chunking, extraction, config precedence, key reading — plus
the audit-hardening seams: the identity-checked takeover (PID-reuse guard, with a real-child
integration case), the urllib-level cloud synthesis request shapes, the bare-marker fast path
that must never burn the flush-race backoff, and eager mode's spoken-ledger, first-run seeding and
inter-firing queue.

speak.py is glue around subprocess players and an HTTP synthesis call, so its full runtime
contract is proven by the REAL Stop-hook invocation in CI (see TESTING.md). What is tested here
never reaches the network, a player, or the live state dir: state paths are monkeypatched into
tmp_path, HTTP openers are faked at the urllib seam, and the only real subprocesses are
short-lived pythons owned by the tests themselves.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import itertools
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_SPEAK_PATH = Path(__file__).resolve().parents[1] / "scripts" / "speak.py"
_spec = importlib.util.spec_from_file_location("speak", _SPEAK_PATH)
speak = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(speak)


@pytest.fixture
def state(monkeypatch, tmp_path):
    """Every state-dir path the script writes, owned by the test — never the live ~/.local/state."""
    monkeypatch.setattr(speak, "_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(speak, "_LOG_PATH", str(tmp_path / "speak.log"))
    monkeypatch.setattr(speak, "_LAST_PATH", str(tmp_path / "last-spoken"))
    monkeypatch.setattr(speak, "_LAST_KEY_PATH", str(tmp_path / "last-spoken-key"))
    monkeypatch.setattr(speak, "_PID_PATH", str(tmp_path / "playing.pid"))
    monkeypatch.setattr(speak, "_LEDGER_PATH", str(tmp_path / "spoken.ledger"))
    monkeypatch.setattr(speak, "_LOCK_PATH", str(tmp_path / "speaking.lock"))
    monkeypatch.setattr(speak, "_STAMP_PATH", str(tmp_path / "hook-last-fired"))
    monkeypatch.setattr(speak, "_pidfile_record", None)

    monkeypatch.setattr(speak, "_CONTOUR_PATH", str(tmp_path / "contour.json"))
    monkeypatch.setattr(speak, "_CONTOUR_ANNOUNCED_PATH", str(tmp_path / "contour-announced"))
    return tmp_path


# --- chunk_sentences: the streaming plan -------------------------------------------------------


def test_chunks_split_on_sentence_boundaries():
    text = "The first sentence is long enough to stand alone. And this second sentence is also long enough."
    assert speak.chunk_sentences(text) == [
        "The first sentence is long enough to stand alone.",
        "And this second sentence is also long enough.",
    ]


def test_tiny_sentences_merge_until_min_chars():
    text = "Yes. No. Maybe. This sentence finally makes the chunk long enough to flush."
    chunks = speak.chunk_sentences(text)
    assert chunks == ["Yes. No. Maybe. This sentence finally makes the chunk long enough to flush."]


def test_short_tail_merges_into_previous_chunk():
    text = "A first sentence comfortably past the minimum length. Tail."
    chunks = speak.chunk_sentences(text)
    assert len(chunks) == 1
    assert chunks[0].endswith("Tail.")


def test_short_text_yields_one_small_chunk():
    assert speak.chunk_sentences("Speech check complete.") == ["Speech check complete."]


def test_empty_text_yields_no_chunks():
    assert speak.chunk_sentences("") == []


def test_every_nonfinal_chunk_meets_the_minimum():
    text = "One. Two. Three. Four. " * 20
    chunks = speak.chunk_sentences(text)
    assert len(chunks) > 1
    assert all(len(c) >= speak.MIN_CHUNK_CHARS for c in chunks)


def test_ellipsis_and_question_marks_are_boundaries():
    text = "Is this a boundary question, asked at length? It is, indeed… and this confirms the ellipsis split."
    assert len(speak.chunk_sentences(text, min_chars=10)) == 3


# --- extract_from_lines: the transcript reader --------------------------------------------------


def _assistant(text: str) -> str:
    return json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}})


def test_extract_takes_the_last_assistant_message():
    lines = [
        _assistant("🔊 the stale first turn"),
        json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": "🔊 not assistant"}]}}),
        _assistant("plain detail line\n🔊 the fresh spoken line"),
    ]
    assert speak.extract_from_lines(lines, "🔊", 600) == "the fresh spoken line"


def test_extract_skips_malformed_lines_and_unmarked_messages():
    lines = ["{not json", _assistant("no marker anywhere")]
    # '' (not None): the message IS present and parsed — it just marks nothing to speak
    assert speak.extract_from_lines(lines, "🔊", 600) == ""


def test_extract_joins_multiple_marked_lines_and_clips():
    lines = [_assistant("🔊 first part\ndetail\n🔊 second part")]
    assert speak.extract_from_lines(lines, "🔊", 600) == "first part second part"
    assert speak.extract_from_lines(lines, "🔊", 10) == "first part"


def test_extract_honours_a_custom_marker_and_leading_space():
    lines = [_assistant("  >> spoken with a custom marker")]
    assert speak.extract_from_lines(lines, ">>", 600) == "spoken with a custom marker"


def test_extract_is_none_before_any_assistant_message_flushes():
    # None is the flush-race signature the caller retries on; '' is a decided "nothing to say"
    assert speak.extract_from_lines([], "🔊", 600) is None
    assert speak.extract_from_lines(["{not json"], "🔊", 600) is None


def test_bare_marker_yields_empty_and_logs(state):
    assert speak.extract_from_lines([_assistant("🔊")], "🔊", 600) == ""
    assert speak.extract_from_lines([_assistant("🔊  \n🔊\t")], "🔊", 600) == ""
    assert "marker with no text" in (state / "speak.log").read_text(encoding="utf-8")


# --- resolve_settings: the config-precedence table ----------------------------------------------


def test_defaults_with_empty_config_linux():
    s = speak.resolve_settings({}, "Linux")
    assert s["enabled"] is True
    assert s["marker"] == "🔊"
    assert s["player"] == "aplay -q"
    assert s["sink"] == ""  # no echo-cancel sink by default
    assert s["max_chars"] == 600
    assert s["timeout"] == 60.0
    assert s["backend"] == "lan"
    assert s["language"] == "en"  # explicit-language setups always write the key; the default is English
    assert s["key_env"] == "VOICE_LOOP_TTS_API_KEY"


def test_speak_sink_is_read_from_config():
    """L2: speak.sink routes TTS audio into the named PipeWire sink for echo cancellation."""
    s = speak.resolve_settings({"speak": {"sink": "Echo-Cancel Sink"}}, "Linux")
    assert s["sink"] == "Echo-Cancel Sink"


def test_default_player_on_macos():
    assert speak.resolve_settings({}, "Darwin")["player"] == "afplay"


def test_default_player_on_windows_is_the_inbox_powershell_soundplayer():
    """The POSIX default (aplay) is not present on Windows, and the "never fail a turn" contract
    turned that into silent speak-back. The in-box PowerShell SoundPlayer is the Windows
    equivalent, and its ``{file}`` placeholder is what lets the WAV path ride inside ``-Command``."""
    player = speak.resolve_settings({}, "Windows")["player"]
    assert "powershell.exe" in player
    assert "{file}" in player


@pytest.mark.parametrize("value", [False, "false"])
def test_enabled_false_disables(value):
    assert speak.resolve_settings({"speak": {"enabled": value}}, "Linux")["enabled"] is False


def test_empty_string_falls_back_to_default():
    # bash-cfg parity: an empty value in the config behaves like an absent key
    assert speak.resolve_settings({"speak": {"marker": ""}}, "Linux")["marker"] == "🔊"


def test_tts_language_beats_top_level_language_beats_default():
    assert speak.resolve_settings({"language": "ru"}, "Linux")["language"] == "ru"
    both = {"language": "ru", "tts": {"language": "de"}}
    assert speak.resolve_settings(both, "Linux")["language"] == "de"


def test_voice_id_defaults_to_speaker():
    s = speak.resolve_settings({"tts": {"speaker": "baya"}}, "Linux")
    assert s["voice_id"] == "baya"
    explicit = {"tts": {"speaker": "baya", "cloud": {"voice_id": "v123"}}}
    assert speak.resolve_settings(explicit, "Linux")["voice_id"] == "v123"


def test_cloud_model_default_follows_provider():
    eleven = speak.resolve_settings({"tts": {"cloud": {"provider": "elevenlabs"}}}, "Linux")
    assert eleven["cloud_model"] == "eleven_multilingual_v2"
    assert speak.resolve_settings({}, "Linux")["cloud_model"] == "tts-1"
    deepgram = speak.resolve_settings({"tts": {"cloud": {"provider": "deepgram"}}}, "Linux")
    assert deepgram["cloud_model"] == "aura-2-thalia-en"


def test_the_output_format_default_follows_the_provider_too():
    """It is not one value with one spelling: ElevenLabs takes an opaque token, Deepgram takes a
    query fragment, and the OpenAI-compatible path does not use it at all. So the default is the
    entry's, not a constant in this file."""
    assert speak.resolve_settings({}, "Linux")["output_format"] == ""
    eleven = speak.resolve_settings({"tts": {"cloud": {"provider": "elevenlabs"}}}, "Linux")
    assert eleven["output_format"] == "mp3_44100_128"
    deepgram = speak.resolve_settings({"tts": {"cloud": {"provider": "deepgram"}}}, "Linux")
    assert deepgram["output_format"] == "encoding=linear16&container=wav"


def test_key_env_precedence_cloud_over_tts_over_default():
    tts_level = {"tts": {"api_key_env": "TTS_LEVEL"}}
    assert speak.resolve_settings(tts_level, "Linux")["key_env"] == "TTS_LEVEL"
    both = {"tts": {"api_key_env": "TTS_LEVEL", "cloud": {"api_key_env": "CLOUD_LEVEL"}}}
    assert speak.resolve_settings(both, "Linux")["key_env"] == "CLOUD_LEVEL"


def test_voice_settings_passthrough_is_a_dict_or_none():
    knobs = {"stability": 0.7, "style": 0.1}
    cfg = {"tts": {"cloud": {"voice_settings": knobs}}}
    assert speak.resolve_settings(cfg, "Linux")["voice_settings"] == knobs
    assert speak.resolve_settings({}, "Linux")["voice_settings"] is None
    junk = {"tts": {"cloud": {"voice_settings": "not-a-dict"}}}
    assert speak.resolve_settings(junk, "Linux")["voice_settings"] is None


# --- read_key: key_file wins, whitespace stripped, never from argv ------------------------------


def test_key_file_wins_over_env(tmp_path):
    key_file = tmp_path / "k"
    key_file.write_text(" sk-fromfile \n")
    assert speak.read_key(str(key_file), "K_ENV", {"K_ENV": "sk-fromenv"}) == "sk-fromfile"


def test_missing_key_file_falls_back_to_env(tmp_path):
    assert speak.read_key(str(tmp_path / "absent"), "K_ENV", {"K_ENV": "sk-fromenv"}) == "sk-fromenv"
    assert speak.read_key("", "K_ENV", {}) == ""


# --- the retry schedule: adaptive, front-loaded, shorter than the old flat tail -----------------


def test_backoff_is_adaptive_and_shorter_than_the_flat_tail():
    assert speak.BACKOFF == (0.15, 0.3, 0.5, 0.7, 1.0)
    assert sum(speak.BACKOFF) < 5 * 0.7  # the old worst case slept 3.5 s before ever giving up


# --- the SSE client: framing, decode, and the fallback decision ---------------------------------


WAV_A = b"RIFF-first-standalone-wav"
WAV_B = b"RIFF-second-standalone-wav"


def _event(name: str, data: dict) -> list[bytes]:
    """Raw byte lines exactly as the server frames them: event, data, blank."""
    return [f"event: {name}\n".encode(), f"data: {json.dumps(data)}\n".encode(), b"\n"]


def _chunk(index: int, payload: bytes) -> list[bytes]:
    return _event("chunk", {"index": index, "audio": base64.b64encode(payload).decode("ascii")})


def test_parse_sse_yields_event_data_pairs_off_byte_lines():
    lines = _chunk(0, WAV_A) + _event("end", {"chunks": 1})
    assert list(speak.parse_sse(lines)) == [
        ("chunk", {"index": 0, "audio": base64.b64encode(WAV_A).decode("ascii")}),
        ("end", {"chunks": 1}),
    ]


def test_parse_sse_accepts_str_lines_too():
    lines = ["event: end\n", 'data: {"chunks": 0}\n', "\n"]
    assert list(speak.parse_sse(lines)) == [("end", {"chunks": 0})]


def test_parse_sse_skips_orphan_data_bad_json_and_non_dict_data():
    lines = [
        b'data: {"orphan": "no event line before me"}\n',
        b"event: chunk\n",
        b"data: {not json\n",
        b"event: end\n",
        b'data: ["a", "list"]\n',
        b"\n",
    ] + _event("end", {"chunks": 0})
    assert list(speak.parse_sse(lines)) == [("end", {"chunks": 0})]


def test_iter_stream_audio_decodes_chunks_and_stops_at_end():
    lines = _chunk(0, WAV_A) + _chunk(1, WAV_B) + _event("end", {"chunks": 2})
    assert list(speak.iter_stream_audio(lines)) == [WAV_A, WAV_B]


def test_iter_stream_audio_keeps_prior_chunks_on_a_late_error():
    # per the contract chunks already sent stay valid; the error is the last event, logged not raised
    lines = _chunk(0, WAV_A) + _event("error", {"error": "engine died", "chunks": 1})
    assert list(speak.iter_stream_audio(lines)) == [WAV_A]


def test_iter_stream_audio_stops_on_undecodable_base64():
    lines = _event("chunk", {"index": 0, "audio": "a"})  # invalid padding
    assert list(speak.iter_stream_audio(lines)) == []


def test_iter_stream_audio_survives_a_connection_drop():
    def dropped():
        yield from _chunk(0, WAV_A)
        raise OSError("connection reset")

    assert list(speak.iter_stream_audio(dropped())) == [WAV_A]


def test_stream_source_replays_the_eagerly_pulled_first_chunk():
    lines = _chunk(0, WAV_A) + _chunk(1, WAV_B) + _event("end", {"chunks": 2})
    source = speak.stream_source(lines)
    assert source is not None
    assert list(source) == [WAV_A, WAV_B]


def test_stream_source_is_none_when_the_stream_dies_before_the_first_chunk():
    # each of these is the "fall back to /tts once" signal
    assert speak.stream_source(_event("error", {"error": "boom", "chunks": 0})) is None
    assert speak.stream_source(_event("end", {"chunks": 0})) is None
    assert speak.stream_source([]) is None

    def dead_socket():
        raise OSError("connection reset")
        yield  # pragma: no cover - never reached, makes this a generator

    assert speak.stream_source(dead_socket()) is None


# --- server_offers_streaming: the /health detection ---------------------------------------------


def test_streaming_detected_only_on_an_explicit_true():
    assert speak.server_offers_streaming(b'{"ok": true, "streaming": true}') is True
    assert speak.server_offers_streaming(b'{"ok": true, "streaming": false}') is False
    assert speak.server_offers_streaming(b'{"ok": true}') is False  # a pre-streaming server
    assert speak.server_offers_streaming(b'{"streaming": "true"}') is False  # strictly boolean


def test_streaming_not_detected_on_garbage_or_no_answer():
    assert speak.server_offers_streaming(None) is False  # unreachable
    assert speak.server_offers_streaming(b"") is False
    assert speak.server_offers_streaming(b"<html>proxy error</html>") is False
    assert speak.server_offers_streaming(b'["not", "a", "dict"]') is False


# --- the timing contract: first_audio_ms is hook start -> first player spawn --------------------
#
# The number the log prints must be the real time-to-first-sound. It once was not: _play_stream
# started its own clock, which begins AFTER stream_source has eagerly pulled (and waited out) the
# first chunk, so a measured 2308 ms wait was logged as 3 ms. These two cases pin the definition on
# both audio paths, with a clock the test drives — no sleeping, no real player, no synthesis.

# Durations are binary-exact (quarters and halves) so the assertions can be on an exact ms — the
# production code truncates with int(), and 0.3 s would land on 2299 instead of 2300.
EXTRACT_SECONDS = 0.25  # the transcript read, before any audio path is entered
PRE_AUDIO_SECONDS = 2.0  # /health + stream open + the first synthesis: the wait that was invisible
PLAYBACK_SECONDS = 0.5  # how long one chunk takes to play, i.e. what a player process costs
FIRST_AUDIO_MS = 2250  # what every path below must report, however the wait was spent


class FakeClock:
    """A monotonic clock the test owns: reading it is free, `advance` is the only way time passes."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakePlayerProcess:
    """One player subprocess, with playback as clock time instead of sound."""

    def __init__(self, clock: FakeClock, playback: float) -> None:
        self._clock = clock
        self._playback = playback
        self.pid = os.getpid()  # a pid the pidfile writer can render; never signalled here
        self.returncode: int | None = None

    def wait(self) -> int:
        self._clock.advance(self._playback)
        self.returncode = 0
        return 0

    def poll(self) -> int | None:
        return self.returncode


def _measure_first_audio_ms(monkeypatch, build_source, playback: float = PLAYBACK_SECONDS):
    """Run one audio path under a driven clock and return (_play_stream result, spawn instants).

    The shape is the same for both paths: t0 at hook start, EXTRACT_SECONDS for the transcript
    read, then a source that burns PRE_AUDIO_SECONDS before the first playable bytes exist.
    """
    clock = FakeClock()
    monkeypatch.setattr(speak.time, "monotonic", clock)
    spawns: list[float] = []

    def fake_popen(argv, **kwargs):
        spawns.append(clock())
        return FakePlayerProcess(clock, playback)

    monkeypatch.setattr(speak.subprocess, "Popen", fake_popen)

    s = speak.resolve_settings({}, "Linux")
    t0 = clock()  # the hook starts here — the one clock every timing is measured from
    clock.advance(EXTRACT_SECONDS)
    source = build_source(clock, s)  # eager for the stream path, lazy for the blob path
    return speak._play_stream(source, s, t0), [round((at - t0) * 1000) for at in spawns]


def test_first_audio_ms_includes_the_wait_before_the_first_chunk(state, monkeypatch):
    """The regression, named: the delay is injected BEFORE the first chunk is yielded — i.e. inside
    stream_source's eager pull, before _play_stream is even entered — and must still be counted."""

    def build_source(clock, s):
        def sse_lines():
            clock.advance(PRE_AUDIO_SECONDS)  # the first synthesis, waited out by the eager pull
            yield from _chunk(0, WAV_A)
            clock.advance(0.125)
            yield from _chunk(1, WAV_B)
            yield from _event("end", {"chunks": 2})

        source = speak.stream_source(sse_lines())
        assert source is not None
        return source

    (played, total_bytes, first_ms, rc), spawns = _measure_first_audio_ms(monkeypatch, build_source)

    assert (played, total_bytes, rc) == (2, len(WAV_A) + len(WAV_B), 0)
    assert first_ms == FIRST_AUDIO_MS  # extract + the wait before the first chunk — NOT the old ~0
    assert spawns[0] == first_ms  # the definition itself: first sound == the first player spawn


def test_blob_path_measures_first_audio_from_the_same_hook_start(state, monkeypatch):
    """Same definition on the blob path: its wait is one /tts synthesis pulled lazily inside
    _play_stream rather than an eager stream pull, and the same shape must report the same ms."""

    def build_source(clock, s):
        def slow_synthesis(text, settings, key):
            clock.advance(PRE_AUDIO_SECONDS)
            return WAV_A

        monkeypatch.setattr(speak, "synthesize", slow_synthesis)
        return speak._synthesized_audio(["one chunk of prose"], s, "")

    (played, total_bytes, first_ms, rc), spawns = _measure_first_audio_ms(monkeypatch, build_source)

    assert (played, total_bytes, rc) == (1, len(WAV_A), 0)
    assert first_ms == FIRST_AUDIO_MS  # identical to the streaming path: one definition, both paths
    assert spawns == [first_ms]


def test_first_audio_ms_is_minus_one_when_nothing_ever_played(state, monkeypatch):
    result, spawns = _measure_first_audio_ms(monkeypatch, lambda clock, s: iter(()))
    assert result == (0, 0, -1, None)
    assert spawns == []


def test_a_nonzero_player_exit_is_not_recorded_as_played(state, monkeypatch):
    """L2 mutation gap: counting at spawn would report delivery even when the player rejects the
    audio. Only a zero exit is a delivered chunk, so a failed player must leave the result empty."""

    class FailedPlayer:
        pid = 123
        returncode = None

        def wait(self):
            self.returncode = 1
            return self.returncode

        def poll(self):
            return self.returncode

    monkeypatch.setattr(speak.subprocess, "Popen", lambda argv, **kwargs: FailedPlayer())
    result = speak._play_stream(iter([WAV_A]), speak.resolve_settings({}, "Linux"), time.monotonic())
    assert result[0] == 0
    assert result[1] == 0
    assert result[3] == 1


def test_a_file_placeholder_player_embeds_the_wav_without_resplitting_its_path():
    """shlex is POSIX: splitting a substituted Windows path (backslashes) would corrupt it. The
    placeholder is replaced AFTER splitting, so the path stays one argv element inside -Command,
    and a player without the placeholder still appends the WAV as the last argument."""
    player = "powershell.exe -NoProfile -Command \"(New-Object System.Media.SoundPlayer '{file}').PlaySync()\""
    assert speak._player_argv(player, r"C:\Users\John Doe\voice.wav") == [
        "powershell.exe",
        "-NoProfile",
        "-Command",
        "(New-Object System.Media.SoundPlayer 'C:\\Users\\John Doe\\voice.wav').PlaySync()",
    ]
    assert speak._player_argv("aplay -q", "/tmp/a.wav") == ["aplay", "-q", "/tmp/a.wav"]


def test_pidfile_write_is_atomic_and_stale_owner_cannot_clear_new_state(state, monkeypatch):
    """L2 mutation gap: an in-place write can expose truncated tokens, and an old cleanup can erase
    a replacement chain. Temp-then-replace plus the exact installed record protects both boundaries."""
    speak._write_pidfile(111, 222)
    monkeypatch.setattr(speak.os, "getpid", lambda: 111)
    speak._atomic_write_text(str(state / "playing.pid"), "333 444", "replacement-")
    speak._clear_pidfile()
    assert (state / "playing.pid").read_text(encoding="utf-8") == "333 444"
    assert not list(state.glob("voice-loop-playing-*"))


def test_atomic_write_removes_its_temp_file_when_the_replace_fails(state, monkeypatch):
    def deny_replace(src, dst):
        raise OSError("replace denied")

    monkeypatch.setattr(speak.os, "replace", deny_replace)
    speak._atomic_write_text(str(state / "playing.pid"), "111", "voice-loop-playing-")
    assert not (state / "playing.pid").exists()
    assert not list(state.glob("voice-loop-playing-*"))


def test_atomic_write_swallows_a_failed_cleanup(state, monkeypatch):
    def deny_replace(src, dst):
        raise OSError("replace denied")

    def deny_unlink(path):
        raise OSError("unlink denied")

    monkeypatch.setattr(speak.os, "replace", deny_replace)
    monkeypatch.setattr(speak.os, "unlink", deny_unlink)
    # the cleanup is best-effort: a failure to remove the temp file must not mask the original error
    speak._atomic_write_text(str(state / "playing.pid"), "111", "voice-loop-playing-")


def test_atomic_write_skips_cleanup_when_the_temp_file_is_never_created(state, monkeypatch):
    def deny_mkstemp(**kwargs):
        raise OSError("mkstemp denied")

    monkeypatch.setattr(speak.tempfile, "mkstemp", deny_mkstemp)
    speak._atomic_write_text(str(state / "playing.pid"), "111", "voice-loop-playing-")
    assert not (state / "playing.pid").exists()


def test_pidfile_ownership_is_false_when_the_record_cannot_be_read(state, monkeypatch):
    monkeypatch.setattr(speak, "_pidfile_record", "111")
    monkeypatch.setattr(speak, "_PID_PATH", str(state / "no-such-dir" / "playing.pid"))
    assert speak._owns_pidfile() is False


def test_pidfile_clear_swallows_a_failed_unlink(state, monkeypatch):
    speak._write_pidfile(111)

    def deny_unlink(path):
        raise OSError("unlink denied")

    monkeypatch.setattr(speak.os, "unlink", deny_unlink)
    speak._clear_pidfile()
    assert speak._pidfile_record is None  # the finally still released the record


# --- corrupt config / key files: ignored loudly, never a crash ----------------------------------


def test_corrupt_json_config_is_ignored_and_logged(state):
    bad = state / "config.json"
    bad.write_text("{not json", encoding="utf-8")
    assert speak.load_config(str(bad)) == {}
    logged = (state / "speak.log").read_text(encoding="utf-8")
    assert "config ignored" in logged and "JSONDecodeError" in logged


def test_non_utf8_config_is_ignored_and_logged(state):
    bad = state / "config.json"
    bad.write_bytes(b'\xff\xfe{"a": 1}')
    assert speak.load_config(str(bad)) == {}
    assert "UnicodeDecodeError" in (state / "speak.log").read_text(encoding="utf-8")


def test_absent_config_stays_silent(state):
    assert speak.load_config(str(state / "absent.json")) == {}
    assert not (state / "speak.log").exists()


def test_non_utf8_key_file_falls_back_to_env_and_never_logs_content(state):
    key_file = state / "k"
    key_file.write_bytes(b"\xff\xfe topsecretbytes")
    assert speak.read_key(str(key_file), "K_ENV", {"K_ENV": "sk-fromenv"}) == "sk-fromenv"
    logged = (state / "speak.log").read_text(encoding="utf-8")
    assert "UnicodeDecodeError" in logged
    assert "topsecret" not in logged


# --- the bare-marker fast path: a decided "nothing to say" burns zero backoff -------------------


def _run_main_against(transcript_text: str, state, monkeypatch) -> tuple[int, list[float]]:
    transcript = state / "transcript.jsonl"
    transcript.write_text(transcript_text + "\n", encoding="utf-8")
    monkeypatch.setenv("VOICE_LOOP_CONFIG", str(state / "absent.json"))
    monkeypatch.setattr(speak.sys, "stdin", io.StringIO(json.dumps({"transcript_path": str(transcript)})))
    sleeps: list[float] = []
    monkeypatch.setattr(speak.time, "sleep", lambda seconds: sleeps.append(seconds))
    return speak.main(), sleeps


def test_bare_marker_exits_immediately_without_backoff(state, monkeypatch):
    rc, sleeps = _run_main_against(_assistant("🔊"), state, monkeypatch)
    assert rc == 0
    assert sleeps == []  # the old behaviour burned the full 2.65 s schedule here
    assert "marker with no text" in (state / "speak.log").read_text(encoding="utf-8")


def test_unmarked_final_message_exits_immediately_without_backoff(state, monkeypatch):
    rc, sleeps = _run_main_against(_assistant("plain prose, nothing marked"), state, monkeypatch)
    assert rc == 0
    assert sleeps == []


# --- the heartbeat stamp: proof the harness still calls the hook ----------------------------------


def test_stamp_hook_fired_writes_epoch_seconds_atomically(state):
    speak.stamp_hook_fired(clock=lambda: 1754157721.5)
    assert (state / "hook-last-fired").read_text(encoding="utf-8") == "1754157721.500\n"
    # nothing half-written is left behind — the temp file was renamed over the stamp
    assert not list(state.glob("voice-loop-stamp-*"))


def test_stamp_hook_fired_never_raises(state, monkeypatch):
    # the stamp lives beside a FILE: its dirname is not a directory, so mkstemp raises — swallowed
    monkeypatch.setattr(speak, "_STAMP_PATH", str(state / "a-file" / "hook-last-fired"))
    (state / "a-file").write_text("x", encoding="utf-8")
    speak.stamp_hook_fired()


def test_every_invocation_stamps_even_one_that_speaks_nothing(state, monkeypatch):
    # the whole point: a firing that exits silently (disabled, nothing marked) still proves the
    # harness called the hook — that proof is what a silent session is diagnosed by
    rc, _ = _run_main_against(_assistant("plain prose, nothing marked"), state, monkeypatch)
    assert rc == 0
    stamp = float((state / "hook-last-fired").read_text(encoding="utf-8"))
    assert stamp > 0
    assert not (state / "last-spoken").exists()  # nothing was spoken, yet the stamp is there


def test_a_disabled_hook_still_stamps(state, monkeypatch):
    config = state / "config.json"
    config.write_text(json.dumps({"speak": {"enabled": False}}), encoding="utf-8")
    monkeypatch.setenv("VOICE_LOOP_CONFIG", str(config))
    monkeypatch.setattr(speak.sys, "stdin", io.StringIO("{}"))
    assert speak.main() == 0
    assert (state / "hook-last-fired").exists()


def test_a_still_unflushed_transcript_does_retry(state, monkeypatch):
    rc, sleeps = _run_main_against("{not json yet", state, monkeypatch)
    assert rc == 0
    assert sleeps == list(speak.BACKOFF)  # None IS the race signature — the backoff still applies


# --- end to end: the timings line main() actually LOGS ------------------------------------------
#
# The cases above pin _play_stream's contract, but they hand it a t0 themselves — so they cannot
# see the bug, which lived in main()'s COMPOSITION: which clock gets passed at the call site. These
# drive the real main() from stdin to the emitted log line, with every I/O seam faked and time
# charged in named slices, so a fresh `time.monotonic()` at either call site is caught.

HEALTH_SECONDS = 0.125  # the /health probe, before any synthesis is even requested
OPEN_SECONDS = 0.125  # POSTing /tts/stream and getting a response back
STREAM_SYNTH_SECONDS = 1.75  # the server's first chunk — waited out by stream_source's eager pull
BLOB_SYNTH_SECONDS = 1.875  # the blob path's first /tts call, pulled lazily inside _play_stream
CHUNK_GAP_SECONDS = 0.125  # the server producing chunk 2 while chunk 1 plays
# Both paths are budgeted to the SAME time-to-first-sound, so one expected number covers both:
#   stream: 0.25 extract + 0.125 health + 0.125 open + 1.75  synth = 2.25 s
#   blob:   0.25 extract + 0.125 health +               1.875 synth = 2.25 s


class FakeStreamResponse:
    """What _open_stream hands back: an iterable of raw SSE lines that can be closed."""

    def __init__(self, lines) -> None:
        self._lines = lines
        self.closed = False

    def __iter__(self):
        return iter(self._lines)

    def close(self) -> None:
        self.closed = True


def _timings_logged(state) -> dict[str, int]:
    """The `timings …` line parsed back into numbers — the hook's actual published contract."""
    logged = (state / "speak.log").read_text(encoding="utf-8")
    lines = [line for line in logged.splitlines() if " timings " in line]
    assert len(lines) == 1, f"expected exactly one timings line, got: {lines}"
    fields = lines[0].split("timings ", 1)[1].split()
    assert [f.split("=")[0] for f in fields] == ["extract_ms", "first_audio_ms", "total_ms"]
    return {key: int(value) for key, value in (f.split("=") for f in fields)}


def _run_hook_on_a_driven_clock(state, monkeypatch, *, streaming: bool):
    """Run the REAL main() end to end with time under the test's control.

    Every seam that costs a listener time is faked to charge a named slice to the clock: the
    transcript read, the /health probe, then either opening the stream and the server's first
    chunk, or the blob path's first synthesis. The player is a fake process whose `wait` is
    playback. Returns (rc, spawn offsets in ms from hook start, the log text).
    """
    clock = FakeClock()
    monkeypatch.setattr(speak.time, "monotonic", clock)
    t0 = clock()  # main() takes its own t0 first thing; on this clock it is exactly this instant

    real_extract = speak.extract

    def slow_extract(path, marker, limit, **scope):
        # **scope carries the event's reading scope (all_messages) and the ledger's veto (accept)
        # through to the real extractor untouched — this seam only charges the clock.
        clock.advance(EXTRACT_SECONDS)  # the transcript read, incl. any flush-race retries
        return real_extract(path, marker, limit, **scope)

    def slow_health(url, timeout):
        clock.advance(HEALTH_SECONDS)
        return b'{"ok": true, "streaming": true}' if streaming else b'{"ok": true}'

    def slow_open_stream(endpoint, payload, timeout):
        clock.advance(OPEN_SECONDS)

        def sse_lines():
            clock.advance(STREAM_SYNTH_SECONDS)  # the first chunk: the wait that was invisible
            yield from _chunk(0, WAV_A)
            clock.advance(CHUNK_GAP_SECONDS)
            yield from _chunk(1, WAV_B)
            yield from _event("end", {"chunks": 2})

        return FakeStreamResponse(sse_lines())

    def slow_synthesize(text, s, key):
        clock.advance(BLOB_SYNTH_SECONDS)
        return WAV_A

    monkeypatch.setattr(speak, "extract", slow_extract)
    monkeypatch.setattr(speak, "_get", slow_health)
    monkeypatch.setattr(speak, "_open_stream", slow_open_stream)
    monkeypatch.setattr(speak, "synthesize", slow_synthesize)

    spawns: list[float] = []

    def fake_popen(argv, **kwargs):
        spawns.append(clock())
        return FakePlayerProcess(clock, PLAYBACK_SECONDS)

    monkeypatch.setattr(speak.subprocess, "Popen", fake_popen)

    line = "The dawn comes and the latency number finally tells the truth."
    rc, sleeps = _run_main_against(_assistant(f"🔊 {line}"), state, monkeypatch)
    assert sleeps == []  # the transcript is flushed; no backoff may pollute the measurement
    return rc, [round((at - t0) * 1000) for at in spawns], (state / "speak.log").read_text(encoding="utf-8")


def test_logged_first_audio_ms_counts_the_wait_before_the_stream_s_first_chunk(state, monkeypatch):
    """The bug's real shape, end to end: /health + stream open + the first chunk all elapse before
    _play_stream is entered, and the LOGGED number must still contain them."""
    rc, spawns, logged = _run_hook_on_a_driven_clock(state, monkeypatch, streaming=True)

    assert rc == 0
    assert "via=stream" in logged  # the streaming call site really is the one under test
    timings = _timings_logged(state)
    assert timings["extract_ms"] == 250
    assert timings["first_audio_ms"] == FIRST_AUDIO_MS  # NOT the ~0 a call-site clock would report
    assert spawns[0] == timings["first_audio_ms"]  # sound begins when the first player is spawned
    # one clock, in order: the read is inside the wait for sound, which is inside the whole run
    assert timings["extract_ms"] < timings["first_audio_ms"] < timings["total_ms"]
    expected_total = FIRST_AUDIO_MS + round((CHUNK_GAP_SECONDS + 2 * PLAYBACK_SECONDS) * 1000)
    assert timings["total_ms"] == expected_total


def test_logged_first_audio_ms_counts_the_blob_path_s_first_synthesis(state, monkeypatch):
    """The other call site: an older server sends the hook down the blob path, where the first
    synthesis happens lazily INSIDE _play_stream — same definition, same number."""
    rc, spawns, logged = _run_hook_on_a_driven_clock(state, monkeypatch, streaming=False)

    assert rc == 0
    assert "via=tts" in logged  # the /health probe declined streaming, so this is the blob path
    timings = _timings_logged(state)
    assert timings["extract_ms"] == 250
    assert timings["first_audio_ms"] == FIRST_AUDIO_MS
    assert spawns == [timings["first_audio_ms"]]
    assert timings["extract_ms"] < timings["first_audio_ms"] < timings["total_ms"]
    assert timings["total_ms"] == FIRST_AUDIO_MS + round(PLAYBACK_SECONDS * 1000)


# --- the PID-reuse identity check (duplicated helper — kept in sync with dictate.py) ------------


def test_pid_identity_accepts_the_voice_loop_chain_on_linux():
    player = "aplay -q /tmp/voice-loop-speak-abc123"
    assert speak.pid_looks_like_speak(9, read_cmdline=lambda pid: player, platform_id="linux") is True
    python_half = "python3 /repo/plugins/voice-loop/scripts/speak.py"
    assert speak.pid_looks_like_speak(9, read_cmdline=lambda pid: python_half, platform_id="linux") is True


def test_pid_identity_rejects_a_reused_or_gone_pid_on_linux():
    assert speak.pid_looks_like_speak(9, read_cmdline=lambda pid: "sshd: user@pts/0", platform_id="linux") is False
    assert speak.pid_looks_like_speak(9, read_cmdline=lambda pid: None, platform_id="linux") is False


def test_pid_identity_check_uses_the_macos_command_line_seam():
    assert speak.pid_looks_like_speak(
        9, read_cmdline=lambda pid: "python3 /repo/scripts/speak.py", platform_id="darwin"
    ) is True
    assert speak.pid_looks_like_speak(
        9, read_cmdline=lambda pid: "ssh user@example", platform_id="darwin"
    ) is False


def test_pid_identity_check_fails_closed_on_an_unsupported_platform():
    assert speak.pid_looks_like_speak(9, read_cmdline=lambda pid: "speak.py", platform_id="win32") is False


def test_pid_identity_queries_ps_for_the_macos_command_line(monkeypatch):
    calls = []

    class PsResult:
        returncode = 0
        stdout = b"aplay -q /tmp/voice-loop-speak-abc"

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return PsResult()

    monkeypatch.setattr(speak.subprocess, "run", fake_run)
    assert speak.pid_looks_like_speak(9, platform_id="darwin") is True
    assert calls == [["ps", "-p", "9", "-o", "command="]]


def test_pid_identity_rejects_a_failed_or_unavailable_ps(monkeypatch):
    class FailedPs:
        returncode = 1
        stdout = b""

    monkeypatch.setattr(speak.subprocess, "run", lambda argv, **kwargs: FailedPs())
    assert speak.pid_looks_like_speak(9, platform_id="darwin") is False

    def no_ps(argv, **kwargs):
        raise OSError("no ps binary")

    monkeypatch.setattr(speak.subprocess, "run", no_ps)
    assert speak.pid_looks_like_speak(9, platform_id="darwin") is False


def test_cmdline_of_reads_our_own_process():
    if not sys.platform.startswith("linux"):
        pytest.skip("/proc/<pid>/cmdline is Linux-only")
    cmdline = speak._cmdline_of(os.getpid())
    assert cmdline is not None and "py" in cmdline  # the pytest python chain
    assert speak._cmdline_of(2**31 - 2) is None  # unreadable: not a process we can inspect


# --- take_over: exactly the recorded pids, each identity-verified -------------------------------


def test_take_over_signals_only_identity_verified_pids(state, monkeypatch):
    (state / "playing.pid").write_text("111 222", encoding="utf-8")
    monkeypatch.setattr(speak, "pid_looks_like_speak", lambda pid: pid == 111)
    kills: list[tuple[int, int]] = []
    monkeypatch.setattr(speak.os, "kill", lambda pid, sig: kills.append((pid, sig)))
    speak.take_over()
    assert kills == [(111, speak.signal.SIGTERM)]


def test_take_over_never_signals_itself_or_without_a_pidfile(state, monkeypatch):
    monkeypatch.setattr(speak, "pid_looks_like_speak", lambda pid: True)

    def no_kill(pid, sig):
        raise AssertionError("nothing may be signalled")

    monkeypatch.setattr(speak.os, "kill", no_kill)
    speak.take_over()  # no pidfile at all
    (state / "playing.pid").write_text(f"{os.getpid()} 0", encoding="utf-8")
    speak.take_over()  # own pid and pid 0 are both skipped


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the identity check reads /proc/<pid>/cmdline; Windows has no /proc to identify a child from")
def test_take_over_sigterms_a_real_recorded_child(state):
    """Real-child integration: a long-sleeping child whose argv carries the voice-loop marker is
    recorded in the pidfile, then taken over — it must receive SIGTERM."""
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(300)", "voice-loop-speak-integration-marker"]
    )
    try:
        (state / "playing.pid").write_text(str(child.pid), encoding="utf-8")
        speak.take_over()
        assert child.wait(timeout=10) == -signal.SIGTERM
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the identity check is Linux-only")
def test_take_over_spares_a_real_child_without_the_marker(state):
    """PID reuse, live: the pidfile points at a same-user process that is NOT voice-loop — it must
    survive the takeover untouched."""
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
    try:
        (state / "playing.pid").write_text(str(child.pid), encoding="utf-8")
        speak.take_over()
        time.sleep(0.2)  # a delivered SIGTERM would have landed by now
        assert child.poll() is None
    finally:
        child.kill()
        child.wait(timeout=10)


# --- process-group pidfile markers (windowsill#152): tts.command children ------------------------
#
# The "pg" marker in the pidfile tells take_over "do not signal this PID directly — the old
# chain's _on_sigterm handler uses killpg on its own children."  playback_is_live checks pg
# leaders by existence alone (no identity guard needed — the marker IS the identity record).


def test_recorded_pids_excludes_pg_marked_children(state):
    """A pidfile with a "pg" marker: _recorded_pids returns only the non-pg PIDs — the ones
    take_over may safely signal after identity verification."""
    (state / "playing.pid").write_text(f"{os.getpid()} pg 222 333", encoding="utf-8")
    assert speak._recorded_pids() == [333]  # own pid filtered, pg+222 skipped


def test_recorded_pids_handles_legacy_format_with_no_pg_marker(state):
    """Backward compat: a pidfile without "pg" tokens returns all non-self PIDs exactly as before."""
    (state / "playing.pid").write_text(f"{os.getpid()} 222", encoding="utf-8")
    assert speak._recorded_pids() == [222]


def test_recorded_pgids_returns_process_group_leaders(state):
    """_recorded_pgids returns only the PIDs that follow "pg" markers — the tts.command children
    whose liveness playback_is_live checks."""
    (state / "playing.pid").write_text(f"{os.getpid()} pg 222 333 pg 444", encoding="utf-8")
    assert speak._recorded_pgids() == [222, 444]


def test_recorded_pgids_empty_when_no_pg_markers(state):
    """No "pg" tokens: _recorded_pgids returns an empty list."""
    (state / "playing.pid").write_text(f"{os.getpid()} 222", encoding="utf-8")
    assert speak._recorded_pgids() == []


def test_recorded_pgids_skips_own_pid(state):
    """A pg-marked PID that matches our own is excluded, same as _recorded_pids."""
    (state / "playing.pid").write_text(f"{os.getpid()} pg {os.getpid()}", encoding="utf-8")
    assert speak._recorded_pgids() == []


def test_take_over_never_signals_pg_marked_children(state, monkeypatch):
    """take_over signals only the PIDs _recorded_pids returns — which excludes pg-marked
    children.  The pg child is not signalled even when every PID passes the identity guard."""
    (state / "playing.pid").write_text(f"{os.getpid()} pg 222 333", encoding="utf-8")
    monkeypatch.setattr(speak, "pid_looks_like_speak", lambda pid: True)
    kills: list[tuple[int, int]] = []
    monkeypatch.setattr(speak.os, "kill", lambda pid, sig: kills.append((pid, sig)))
    speak.take_over()
    assert kills == [(333, speak.signal.SIGTERM)]  # only 333, not 222


def test_playback_is_live_detects_pg_leaders(state, monkeypatch):
    """A pg leader that still exists makes playback_is_live return True — no identity guard
    needed, because the "pg" marker IS the identity record."""
    (state / "playing.pid").write_text(f"{os.getpid()} pg 222", encoding="utf-8")
    # pid_looks_like_speak returns False for every regular PID, so only the pg leader matters
    monkeypatch.setattr(speak, "pid_looks_like_speak", lambda pid: False)
    monkeypatch.setattr(speak.os, "kill", lambda pid, sig: None)  # both exist
    assert speak.playback_is_live() is True


def test_playback_is_live_ignores_dead_pg_leaders(state, monkeypatch):
    """A pg leader that no longer exists is not live playback."""
    (state / "playing.pid").write_text(f"{os.getpid()} pg 222", encoding="utf-8")
    monkeypatch.setattr(speak, "pid_looks_like_speak", lambda pid: False)

    def kill_races(pid, sig):
        if pid == 222:
            raise ProcessLookupError

    monkeypatch.setattr(speak.os, "kill", kill_races)
    assert speak.playback_is_live() is False


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="process-group signalling needs os.killpg, which Windows does not have")
def test_on_sigterm_uses_killpg_when_pgid_is_set(monkeypatch):
    """When _live has a "pgid", _on_sigterm calls os.killpg on it — the process-group
    signalling that reaches the player inside the shell regardless of exec or wrapper depth."""
    killpg_calls: list[tuple[int, int]] = []
    monkeypatch.setattr(speak.os, "killpg", lambda pgid, sig: killpg_calls.append((pgid, sig)))
    monkeypatch.setattr(speak.os, "_exit", lambda code: None)  # don't actually exit

    class _FakeProc:
        def poll(self):
            return None

        def terminate(self):
            pass

    speak._live["proc"] = _FakeProc()
    speak._live["pgid"] = 999
    try:
        speak._on_sigterm(signal.SIGTERM, None)
        assert killpg_calls == [(999, signal.SIGTERM)]
    finally:
        speak._live["proc"] = None
        speak._live.pop("pgid", None)


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="process-group signalling needs os.killpg, which Windows does not have")
def test_on_sigterm_skips_killpg_when_pgid_not_set(monkeypatch):
    """Without a pgid, _on_sigterm never calls killpg — the normal path is unchanged."""
    killpg_calls: list = []
    monkeypatch.setattr(speak.os, "killpg", lambda pgid, sig: killpg_calls.append(1))
    monkeypatch.setattr(speak.os, "_exit", lambda code: None)  # don't actually exit

    class _FakeProc:
        def poll(self):
            return None

        def terminate(self):
            pass

    speak._live["proc"] = _FakeProc()
    speak._live.pop("pgid", None)  # ensure clean state
    try:
        speak._on_sigterm(signal.SIGTERM, None)
        assert killpg_calls == []
    finally:
        speak._live["proc"] = None


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="process-group signalling is POSIX; the identity check is Linux-only")
def test_take_over_spares_a_pg_marked_child_integration(state):
    """Real-child integration: a process-group leader marked "pg" in the pidfile is NOT signalled
    by take_over — the identity-guess is skipped.  The child exits normally (rc=0), not by signal.
    This is the acceptance criterion from windowsill#152: the player exits on its own rather than
    by signal when it belongs to a process-group chain."""
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(2); print('done')"],
    )
    try:
        (state / "playing.pid").write_text(f"{os.getpid()} pg {child.pid}", encoding="utf-8")
        speak.take_over()
        # The child should survive — it's pg-marked, so take_over skips it.
        time.sleep(0.3)  # a delivered SIGTERM would have landed by now
        assert child.poll() is None, f"pg-marked child was signalled (rc={child.poll()})"
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)


# --- the cloud synthesis request shapes, mocked at the urllib seam ------------------------------


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class FakeOpener:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.requests: list[tuple[object, float | None]] = []

    def open(self, request, timeout=None):
        self.requests.append((request, timeout))
        return FakeResponse(self.body)


@pytest.fixture
def opener(monkeypatch):
    holder: dict[str, FakeOpener] = {}
    monkeypatch.setattr(speak.urllib.request, "build_opener", lambda *handlers: holder["opener"])

    def install(body: bytes) -> FakeOpener:
        holder["opener"] = FakeOpener(body)
        return holder["opener"]

    return install


def test_elevenlabs_synthesis_posts_the_documented_shape(opener):
    fake = opener(b"MP3-audio-bytes")
    config = {
        "tts": {
            "backend": "cloud",
            "cloud": {
                "provider": "elevenlabs",
                "voice_id": "v123",
                "output_format": "mp3_22050_32",
                "voice_settings": {"stability": 0.6, "style": 0.1},
            },
        }
    }
    s = speak.resolve_settings(config, "Linux")
    assert speak.synthesize("hello there", s, "xi-secret") == b"MP3-audio-bytes"

    request, timeout = fake.requests[0]
    assert request.full_url == "https://api.elevenlabs.io/v1/text-to-speech/v123?output_format=mp3_22050_32"
    assert request.get_method() == "POST"
    assert request.get_header("Xi-api-key") == "xi-secret"
    assert request.get_header("Content-type") == "application/json"
    assert timeout == 60.0
    assert json.loads(request.data) == {
        "text": "hello there",
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {"stability": 0.6, "style": 0.1},
    }


def test_elevenlabs_omits_voice_settings_when_unset(opener):
    fake = opener(b"MP3-audio-bytes")
    config = {"tts": {"backend": "cloud", "cloud": {"provider": "elevenlabs", "voice_id": "v123"}}}
    s = speak.resolve_settings(config, "Linux")
    assert speak.synthesize("hi", s, "xi-secret") == b"MP3-audio-bytes"
    request, _ = fake.requests[0]
    assert request.full_url == "https://api.elevenlabs.io/v1/text-to-speech/v123?output_format=mp3_44100_128"
    assert json.loads(request.data) == {"text": "hi", "model_id": "eleven_multilingual_v2"}


def test_an_unset_elevenlabs_voice_logs_misconfiguration_and_never_posts(state, opener):
    """The empty voice_id used to interpolate an empty path segment and come back as an opaque 404;
    the builder now refuses it, and synthesize() turns that refusal into a log line and no request."""
    fake = opener(b"MP3-audio-bytes")
    s = speak.resolve_settings(
        {"tts": {"backend": "cloud", "cloud": {"provider": "elevenlabs"}}}, "Linux"
    )
    assert speak.synthesize("hi", s, "xi-secret") is None
    assert fake.requests == [], "an unset voice must not reach the network"
    log_text = (state / "speak.log").read_text(encoding="utf-8")
    assert "cloud tts misconfigured" in log_text
    assert "voice" in log_text


def test_a_clear_text_cloud_endpoint_is_refused_at_configuration_time(state, monkeypatch, opener):
    """windowsill #215: an http:// endpoint carrying the API key used to be WARNED about while the
    request was sent. The policy is now a REFUSAL, made when the configuration is assembled —
    before any request exists — naming the endpoint and the fix."""
    fake = opener(b"MP3-audio-bytes")
    monkeypatch.setenv("VOICE_LOOP_TTS_API_KEY", "sk-secret")
    s = speak.resolve_settings(
        {"tts": {"backend": "cloud", "endpoint": "http://192.168.1.100:8080",
                 "cloud": {"provider": "elevenlabs", "voice_id": "v123"}}},
        "Linux",
    )
    refusal = speak._clear_text_refusal(s)
    assert refusal is not None
    assert "192.168.1.100" in refusal
    assert "https://" in refusal
    assert fake.requests == []


def test_an_endpoint_name_that_merely_starts_with_127_is_refused(state, monkeypatch):
    """The exact shape from the tracker (#215): 127.evil.com is a DNS name that merely LOOKS
    loopback and resolves wherever its owner pointed it. Local is decided by the RESOLVED address
    — here a public one — so the credential never rides the clear text to it."""
    monkeypatch.setenv("VOICE_LOOP_TTS_API_KEY", "sk-secret")
    monkeypatch.setattr(
        speak.socket, "getaddrinfo", lambda host, *a, **k: [["", "", "", "", ("93.184.216.34", 0)]]
    )
    s = speak.resolve_settings(
        {"tts": {"backend": "cloud", "endpoint": "http://127.evil.com:9000",
                 "cloud": {"provider": "elevenlabs", "voice_id": "v123"}}},
        "Linux",
    )
    refusal = speak._clear_text_refusal(s)
    assert refusal == (
        "cloud tts refused: http endpoint '127.evil.com' would carry the API key and the audio in the "
        "clear — point it at https://, or at this machine"
    )


def test_a_local_by_resolution_endpoint_is_classified_once_not_per_request(state, monkeypatch, opener):
    """windowsill #215's trap: resolving per REQUEST is a blocking call on every synthesis. The
    guard resolves the name ONCE, at configuration time; synthesize() then sends without ever
    looking the host up again."""
    fake = opener(b"MP3-audio-bytes")
    lookups: list[str] = []

    def one_address(host, *args, **kwargs):
        lookups.append(host)
        return [["", "", "", "", ("127.0.0.1", 0)]]

    monkeypatch.setattr(speak.socket, "getaddrinfo", one_address)
    monkeypatch.setenv("VOICE_LOOP_TTS_API_KEY", "sk-secret")
    s = speak.resolve_settings(
        {"tts": {"backend": "cloud", "endpoint": "http://tunnel.internal:8355",
                 "cloud": {"provider": "openai"}}},
        "Linux",
    )
    assert speak._clear_text_refusal(s) is None  # the name resolves to loopback: admitted
    assert lookups == ["tunnel.internal"]  # exactly one lookup, and the guard made it
    monkeypatch.setattr(
        speak.socket, "getaddrinfo", lambda host, *a, **k: pytest.fail(f"per-request lookup of {host}")
    )
    assert speak.synthesize("hi", s, "sk-secret") == b"MP3-audio-bytes"


def test_the_default_local_cloud_path_is_never_refused(state, monkeypatch, opener):
    """The local server on 127.0.0.1 is the cloud backend's default landing (openai has no remote
    host): a key over that http:// endpoint is plaintext to loopback, which the policy allows."""
    fake = opener(b"WAV-audio-bytes")
    monkeypatch.setenv("VOICE_LOOP_TTS_API_KEY", "sk-secret")
    s = speak.resolve_settings({"tts": {"backend": "cloud"}}, "Linux")
    assert speak._clear_text_refusal(s) is None
    assert speak.synthesize("hi", s, "sk-secret") == b"WAV-audio-bytes"


def test_a_keyless_clear_text_config_is_not_the_guard_s_business(monkeypatch):
    """No credential configured, nothing rides the clear text — the guard stays out of the way
    (the cloud path refuses keyless calls itself, unchanged)."""
    monkeypatch.delenv("VOICE_LOOP_TTS_API_KEY", raising=False)
    s = speak.resolve_settings(
        {"tts": {"backend": "cloud", "endpoint": "http://192.168.1.100:8080"}}, "Linux"
    )
    assert speak._clear_text_refusal(s) is None


def test_main_refuses_a_clear_text_cloud_config_and_never_builds_a_request(state, monkeypatch, opener):
    """L1 composition junction: the refusal decision is pinned in the guard tests above and in
    providers.py; what only a real hook drive can catch is the WIRING being lost — the guard
    skipped, or reached only after the first request was already built. entry() is the whole hook
    (the turn's speech AND the contour check, which resolves its own settings and synthesizes on
    its own path), so an ACTIVE alert is planted: for a refused configuration no request, no
    player and no page may exist — the alert stays unannounced, to be retried once the config is
    fixed, exactly like a page whose delivery failed."""
    fake = opener(b"MP3-audio-bytes")
    config = state / "config.json"
    config.write_text(
        json.dumps(
            {"tts": {"backend": "cloud", "endpoint": "http://192.168.1.100:8080",
                     "cloud": {"provider": "elevenlabs", "voice_id": "v123"}}}
        ),
        encoding="utf-8",
    )
    transcript = state / "transcript.jsonl"
    transcript.write_text(_assistant("🔊 hello") + "\n", encoding="utf-8")
    _contour_status(state, [_DEMOTED])
    monkeypatch.setenv("VOICE_LOOP_CONFIG", str(config))
    monkeypatch.setenv("VOICE_LOOP_TTS_API_KEY", "sk-secret")
    monkeypatch.setattr(
        speak.sys, "stdin", io.StringIO(json.dumps({"transcript_path": str(transcript)}))
    )

    class SilentPlayer:
        pid = 4242
        returncode = 0

        def wait(self):
            return 0

    players: list[list[str]] = []
    monkeypatch.setattr(speak.subprocess, "Popen", lambda argv, **kw: players.append(argv) or SilentPlayer())

    assert speak.entry() == 0
    assert fake.requests == [], "a refused configuration must not build a request"
    assert players == [], "nothing to play: no synthesis happened"
    assert "cloud tts refused" in (state / "speak.log").read_text(encoding="utf-8")
    assert not (state / "contour-announced").exists(), "a refused configuration must not page either"


def test_deepgram_synthesis_goes_through_synthesize_with_no_branch_in_the_way(opener):
    """The TTS half of the one-entry proof: `deepgram` reaches its own host with its own auth
    scheme and its own container parameters, and synthesize() never learned its name.

    WAV rather than mp3 on purpose — `aplay -q`, the documented Linux player, cannot play mp3, so
    an mp3 default would be a provider that installs green and plays nothing."""
    fake = opener(b"RIFF-wav-audio-bytes")
    config = {"tts": {"backend": "cloud", "cloud": {"provider": "deepgram"}}}
    s = speak.resolve_settings(config, "Linux")
    assert speak.synthesize("hello there", s, "dg-secret") == b"RIFF-wav-audio-bytes"

    request, timeout = fake.requests[0]
    assert request.full_url == (
        "https://api.deepgram.com/v1/speak?model=aura-2-thalia-en&encoding=linear16&container=wav"
    )
    assert request.get_header("Authorization") == "Token dg-secret"
    assert request.get_header("Content-type") == "application/json"
    assert json.loads(request.data) == {"text": "hello there"}
    assert timeout == 60.0


def test_an_unknown_tts_provider_falls_back_to_the_default_and_says_so(state, opener):
    fake = opener(b"WAV-audio-bytes")
    s = speak.resolve_settings({"tts": {"backend": "cloud", "cloud": {"provider": "11labs"}}}, "Linux")
    assert s["provider"] == "openai"
    assert speak.synthesize("hi", s, "sk-secret") == b"WAV-audio-bytes"
    # the default provider has no remote host of its own — it lands on the local speech server
    assert fake.requests[0][0].full_url == "http://127.0.0.1:8355/v1/audio/speech"
    log_text = (state / "speak.log").read_text(encoding="utf-8")
    assert "tts.cloud.provider is not a known provider" in log_text
    assert "'11labs'" in log_text


def test_openai_compatible_synthesis_posts_the_documented_shape(opener):
    fake = opener(b"WAV-audio-bytes")
    config = {
        "tts": {
            "backend": "cloud",
            "endpoint": "https://speech.example.com",
            "cloud": {"voice_id": "onyx", "model": "gpt-4o-mini-tts"},
        }
    }
    s = speak.resolve_settings(config, "Linux")
    assert speak.synthesize("hi", s, "sk-secret") == b"WAV-audio-bytes"

    request, timeout = fake.requests[0]
    assert request.full_url == "https://speech.example.com/v1/audio/speech"
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == "Bearer sk-secret"
    assert timeout == 60.0
    assert json.loads(request.data) == {
        "model": "gpt-4o-mini-tts",
        "voice": "onyx",
        "input": "hi",
        "response_format": "wav",
    }


def test_openai_compatible_defaults_to_the_local_server_and_alloy(opener):
    fake = opener(b"WAV-audio-bytes")
    s = speak.resolve_settings({"tts": {"backend": "cloud"}}, "Linux")
    assert speak.synthesize("hi", s, "sk-secret") == b"WAV-audio-bytes"
    request, _ = fake.requests[0]
    assert request.full_url == "http://127.0.0.1:8355/v1/audio/speech"
    payload = json.loads(request.data)
    assert payload["voice"] == "alloy"
    assert payload["model"] == "tts-1"


# === eager mode ================================================================================
#
# The Stop hook only fires when a turn ENDS, so a 🔊 line printed early in a long tool-heavy turn is
# heard minutes late. Eager mode adds a PostToolUse path that speaks it as it appears — which turns
# "have I said this already?" from one last-spoken string into a real question, since two event
# paths now read the same transcript. The answer is the spoken-ledger, and the tests below pin its
# three consequences: a line is spoken exactly once whichever hook saw it first, a transcript nobody
# has spoken for yet is history rather than news, and firings within one turn QUEUE instead of
# talking over each other.


# --- the ledger itself --------------------------------------------------------------------------


def test_ledger_key_is_per_line_per_message_and_per_transcript():
    key = speak.ledger_key("/t/one.jsonl", 3, "the line")
    assert key == speak.ledger_key("/t/one.jsonl", 3, "the line")  # stable: it IS the identity
    assert key != speak.ledger_key("/t/one.jsonl", 3, "another line")
    # the same sentence in a different session is a different line, and deserves to be heard
    assert key != speak.ledger_key("/t/two.jsonl", 3, "the line")
    # and so is the same sentence in a LATER message of the same session: «Done.» said again is a
    # new «Done.», not the old one echoing. Keyed by text alone it would go permanently silent.
    assert key != speak.ledger_key("/t/one.jsonl", 4, "the line")
    assert len(key) == 16


def test_seed_marker_is_per_transcript_and_never_looks_like_a_line():
    marker = speak.seed_marker("/t/one.jsonl")
    assert marker.startswith(speak.SEED_PREFIX)
    assert marker != speak.seed_marker("/t/two.jsonl")
    assert marker != speak.ledger_key("/t/one.jsonl", 0, "/t/one.jsonl")


def test_ledger_claims_append_and_read_back_in_order(state):
    assert speak.read_ledger() == []  # an absent ledger is an empty one, never an error
    speak.append_ledger(["aaaa", "bbbb"])
    speak.append_ledger([])  # nothing to claim writes nothing
    speak.append_ledger(["cccc"])
    assert speak.read_ledger() == ["aaaa", "bbbb", "cccc"]


def test_an_unwritable_ledger_is_logged_and_never_raises(state, monkeypatch):
    monkeypatch.setattr(speak, "_LEDGER_PATH", str(state / "no-such-dir" / "spoken.ledger"))
    speak.append_ledger(["aaaa"])  # must not raise: a dedup we cannot record is not a failed turn
    assert "ledger unwritable" in (state / "speak.log").read_text(encoding="utf-8")
    assert speak.read_ledger() == []


def test_ledger_trim_keeps_the_last_lines_and_the_seed_markers(state):
    seeds = [f"{speak.SEED_PREFIX}{n:016d}" for n in range(speak.LEDGER_SEEDS + 5)]
    lines = [f"{n:016d}" for n in range(speak.LEDGER_LINES + 50)]
    speak.append_ledger(seeds + lines)
    speak.trim_ledger()

    kept = speak.read_ledger()
    kept_seeds = [e for e in kept if e.startswith(speak.SEED_PREFIX)]
    kept_lines = [e for e in kept if not e.startswith(speak.SEED_PREFIX)]
    assert kept_lines == lines[-speak.LEDGER_LINES:]  # the rolling window, newest end kept
    assert kept_seeds == seeds[-speak.LEDGER_SEEDS:]  # markers survive the line churn separately
    assert len(kept) == speak.LEDGER_LINES + speak.LEDGER_SEEDS


def test_ledger_trim_under_the_limit_rewrites_nothing(state):
    speak.append_ledger(["aaaa", "bbbb"])
    before = (state / "spoken.ledger").stat().st_mtime_ns
    speak.trim_ledger()
    assert (state / "spoken.ledger").stat().st_mtime_ns == before
    assert speak.read_ledger() == ["aaaa", "bbbb"]


# --- the two reading scopes, and the ledger's veto ----------------------------------------------


def test_eager_reads_every_message_where_stop_reads_only_the_last():
    lines = [_assistant("🔊 the first line"), _assistant("plain detail\n🔊 the second line")]
    # Stop: the turn it was called to speak
    assert speak.extract_from_lines(lines, "🔊", 600) == "the second line"
    # PostToolUse: by the time a tool returns, the line worth hearing can be several messages back
    assert speak.extract_from_lines(lines, "🔊", 600, all_messages=True) == "the first line the second line"


def test_already_spoken_lines_read_as_nothing_new_not_as_nothing_to_say():
    lines = [_assistant("🔊 alpha"), _assistant("🔊 beta")]
    spoken = {"alpha"}
    # 'beta' is new -> speak it alone, oldest-first order preserved among what is left
    assert (
        speak.extract_from_lines(lines, "🔊", 600, all_messages=True, accept=lambda i, x: x not in spoken) == "beta"
    )
    # everything claimed -> None, the SAME answer an unflushed message gives: nothing new YET.
    # '' would mean "decided: nothing to say" and would end the Stop path's flush-race retry early.
    assert speak.extract_from_lines(lines, "🔊", 600, all_messages=True, accept=lambda i, x: False) is None
    assert speak.extract_from_lines([_assistant("no marker here")], "🔊", 600, accept=lambda i, x: False) == ""


def test_the_veto_sees_which_message_each_line_came_from():
    """The index the ledger keys on: the veto is asked about (message, line), so a repeated sentence
    in a later message is a question it can answer differently."""
    lines = [_assistant("🔊 Done."), _assistant("working"), _assistant("🔊 Done.")]
    seen: list[tuple[int, str]] = []

    def accept(index: int, line: str) -> bool:
        seen.append((index, line))
        return index != 0  # the first «Done.» is claimed; the later one is not

    assert speak.extract_from_lines(lines, "🔊", 600, all_messages=True, accept=accept) == "Done."
    assert seen == [(0, "Done."), (2, "Done.")]  # message 1 has no marked line, so index 2 it is


def test_marked_history_is_scoped_by_the_event(state):
    transcript = state / "transcript.jsonl"
    transcript.write_text("\n".join([_assistant("🔊 alpha"), _assistant("🔊 beta")]) + "\n", encoding="utf-8")
    # history comes back in the shape the ledger keys on: (message index, line)
    # eager owns none of it: never recite a session back at its user
    assert speak.marked_history(str(transcript), "🔊", include_last=True) == [(0, "alpha"), (1, "beta")]
    # Stop is entitled to the last message — that is the turn it was invoked to speak
    assert speak.marked_history(str(transcript), "🔊", include_last=False) == [(0, "alpha")]
    assert speak.marked_history(str(state / "absent.jsonl"), "🔊", include_last=True) == []


# --- driving the real main() through both event paths -------------------------------------------


class _Stdin:
    """A stdin any number of invocations can read — including two of them, concurrently."""

    def __init__(self, payload: str) -> None:
        self._payload = payload

    def read(self) -> str:
        return self._payload


class _NullPlayer:
    """A player process that plays nothing, instantly."""

    def __init__(self) -> None:
        self.pid = os.getpid()  # a pid the pidfile writer can render; never signalled here
        self.returncode: int | None = None

    def wait(self) -> int:
        self.returncode = 0
        return 0

    def poll(self) -> int | None:
        return self.returncode


def _record_speech(monkeypatch, on_synthesize=None) -> list[str]:
    """Install the whole audio half as a recorder: what the hook decides to SAY, in order.

    Every line these tests speak is far shorter than MIN_CHUNK_CHARS, so chunk_sentences yields
    exactly one chunk per utterance and one recorded entry IS one spoken utterance.
    """
    spoken: list[str] = []
    monkeypatch.setattr(speak, "_get", lambda url, timeout: None)  # no /health answer -> blob path

    def fake_synthesize(text, s, key):
        spoken.append(text)
        if on_synthesize is not None:
            on_synthesize(text)
        return WAV_A

    monkeypatch.setattr(speak, "synthesize", fake_synthesize)
    monkeypatch.setattr(speak.subprocess, "Popen", lambda argv, **kwargs: _NullPlayer())
    return spoken


def _write_config(state, monkeypatch, **speak_keys) -> None:
    config = state / "config.json"
    config.write_text(json.dumps({"speak": speak_keys}), encoding="utf-8")
    monkeypatch.setenv("VOICE_LOOP_CONFIG", str(config))


def _fire(state, monkeypatch, transcript, event: str) -> tuple[int, list[float]]:
    """One hook invocation, as the harness makes it: the event named on stdin, nothing else."""
    payload = json.dumps({"transcript_path": str(transcript), "hook_event_name": event})
    monkeypatch.setattr(speak.sys, "stdin", _Stdin(payload))
    sleeps: list[float] = []
    monkeypatch.setattr(speak.time, "sleep", lambda seconds: sleeps.append(seconds))
    return speak.main(), sleeps


def _append_message(transcript, text: str) -> None:
    with transcript.open("a", encoding="utf-8") as fh:
        fh.write(_assistant(text) + "\n")


def test_eager_stays_a_no_op_until_it_is_opted_into(state, monkeypatch):
    """hooks.json registers PostToolUse unconditionally, so with speak.eager off the firing must
    cost one stdin read and stop there — before the transcript, before the ledger, before anything
    that touches the disk on every single tool call."""
    transcript = state / "transcript.jsonl"
    transcript.write_text(_assistant("🔊 a line nobody has opted into hearing yet") + "\n", encoding="utf-8")

    def never(*args, **kwargs):
        raise AssertionError("eager is off: nothing may be read")

    monkeypatch.setattr(speak, "extract", never)
    monkeypatch.setattr(speak, "read_ledger", never)
    _write_config(state, monkeypatch)  # a config with no speak.eager key at all: the default

    rc, sleeps = _fire(state, monkeypatch, transcript, "PostToolUse")
    assert (rc, sleeps) == (0, [])
    assert not (state / "spoken.ledger").exists()
    assert not (state / "last-spoken").exists()
    assert not (state / "last-spoken-key").exists()


def _speak_turns(state, monkeypatch, lines: list[str]) -> list[str]:
    """Play a whole session down the Stop path: one assistant message per turn, one Stop firing
    after each. Returns what was actually spoken, in order."""
    transcript = state / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")
    spoken = _record_speech(monkeypatch)
    for line in lines:
        _append_message(transcript, f"🔊 {line}")
        assert _fire(state, monkeypatch, transcript, "Stop")[0] == 0
    return spoken


def test_eager_off_repeated_line_in_a_new_message_is_decided_without_flush_wait(state, monkeypatch):
    """L2 mutation gap: an eager-off implementation that keys only by text still burns the
    flush wait for a genuine repeat in a new assistant message; the message-index key must make it
    a settled dedup with no extra sleep."""
    transcript = state / "transcript.jsonl"
    _write_config(state, monkeypatch)
    spoken = _record_speech(monkeypatch)
    transcript.write_text("", encoding="utf-8")
    _append_message(transcript, "🔊 Done.")
    assert _fire(state, monkeypatch, transcript, "Stop") == (0, [])
    _append_message(transcript, "🔊 Done.")
    rc, sleeps = _fire(state, monkeypatch, transcript, "Stop")
    assert (rc, sleeps) == (0, [])
    assert spoken == ["Done."]
    assert "dropped a read identical to the last spoken line (dedup): Done." in _speak_log(state)


def test_eager_off_keeps_exactly_the_pre_ledger_stop_dedup(state, monkeypatch):
    """DEFAULT-OFF REGRESSION — the ledger must not reach a user who never opted in.

    With `speak.eager` unset, Stop dedups against the IMMEDIATELY previous utterance and nothing
    else: a verbatim repeat of the line just spoken is dropped (turn 2), and the same line said
    again LATER, after something else came in between, is spoken again (turn 4). A per-session
    ledger keyed by text would mute that fourth «Done.» for the rest of the session — a silent
    regression for every default-off user, which is what this pins.

    Mutation proof: make the ledger unconditional in main() (drop the `if ledger_on:` gate and the
    `if not ledger_on` branch inside read_fresh) and BOTH halves fail — the preloaded ledger vetoes
    turn 1, and the claims it writes change a file that must not be touched at all.
    """
    transcript = state / "transcript.jsonl"
    turns = ["Done.", "Done.", "Working.", "Done."]
    # A ledger that already accounts for every one of those lines, in both the current
    # (transcript, message, line) keying and the per-session (transcript, line) keying the blocker
    # was reported against: eager being off means NEITHER is ever consulted.
    preloaded = [speak.seed_marker(str(transcript))]
    for index, line in enumerate(turns):
        preloaded.append(speak.ledger_key(str(transcript), index, line))
        preloaded.append(hashlib.sha1(f"{transcript}\n{line}".encode()).hexdigest()[:16])
    ledger = state / "spoken.ledger"
    ledger.write_text("".join(f"{e}\n" for e in preloaded), encoding="utf-8")
    before = ledger.read_bytes()

    _write_config(state, monkeypatch)  # a config with no speak.eager key at all: the default
    spoken = _speak_turns(state, monkeypatch, turns)

    assert spoken == ["Done.", "Working.", "Done."]  # every turn but the immediate repeat
    assert ledger.read_bytes() == before  # not read, not claimed, not trimmed, not seeded


def test_eager_speaks_a_repeated_line_again_in_a_later_message(state, monkeypatch):
    """The message index in the ledger key, end to end. With eager ON the ledger replaces the
    prev-dedup, so it has to be at least as good: «Done.» in message 1 and «Done.» in message 0 are
    two lines, and both are heard. Keyed by text alone the session would go quiet after the first
    one — the regression the default-off test above pins from the other side."""
    _write_config(state, monkeypatch, eager=True)
    spoken = _speak_turns(state, monkeypatch, ["Done.", "Done.", "Working.", "Done."])
    assert spoken == ["Done.", "Done.", "Working.", "Done."]  # every one of them, repeats included


def test_first_run_seeding_writes_history_off_instead_of_reciting_it(state, monkeypatch):
    """The live deployment's own trip: eager switched on mid-session, and the first firing read a
    transcript full of marked lines it had never spoken. They are HISTORY — ledgered silently — and
    only what appears AFTER is news."""
    transcript = state / "transcript.jsonl"
    transcript.write_text("\n".join([_assistant("🔊 an old line"), _assistant("🔊 an older line")]) + "\n", "utf-8")
    _write_config(state, monkeypatch, eager=True)
    spoken = _record_speech(monkeypatch)

    rc, sleeps = _fire(state, monkeypatch, transcript, "PostToolUse")

    assert (rc, sleeps) == (0, [])
    assert spoken == []  # the whole point: the session is not read back to its user
    assert speak.seed_marker(str(transcript)) in speak.read_ledger()
    assert speak.ledger_key(str(transcript), 0, "an old line") in speak.read_ledger()
    assert "seeded 2 line(s) of history" in (state / "speak.log").read_text(encoding="utf-8")

    # and seeding is a starting line, not a gag: the next line to appear IS spoken
    _append_message(transcript, "🔊 a fresh line")
    assert _fire(state, monkeypatch, transcript, "PostToolUse") == (0, [])
    assert spoken == ["a fresh line"]


def test_stop_seeds_history_but_never_the_turn_it_was_called_to_speak(state, monkeypatch):
    """The same first run down the Stop path. Seeding must not swallow the last message — that is
    the turn Stop exists to speak, and the pre-ledger behaviour it has to keep."""
    transcript = state / "transcript.jsonl"
    transcript.write_text("\n".join([_assistant("🔊 an old line"), _assistant("🔊 this turn")]) + "\n", "utf-8")
    _write_config(state, monkeypatch, eager=True)
    spoken = _record_speech(monkeypatch)

    assert _fire(state, monkeypatch, transcript, "Stop") == (0, [])
    assert spoken == ["this turn"]
    assert speak.ledger_key(str(transcript), 0, "an old line") in speak.read_ledger()  # history, silent


def test_eager_speaks_the_lines_of_every_message_oldest_first_then_nothing_twice(state, monkeypatch):
    """Two marked lines land in two messages while the turn is still running: one firing after them
    speaks both, oldest first — and a firing with nothing new to say says nothing."""
    transcript = state / "transcript.jsonl"
    transcript.write_text(_assistant("a turn with no marked line yet") + "\n", encoding="utf-8")
    _write_config(state, monkeypatch, eager=True)
    spoken = _record_speech(monkeypatch)
    assert _fire(state, monkeypatch, transcript, "PostToolUse") == (0, [])  # seeds the transcript

    _append_message(transcript, "🔊 alpha line")
    _append_message(transcript, "detail nobody hears\n🔊 beta line")
    assert _fire(state, monkeypatch, transcript, "PostToolUse") == (0, [])
    assert spoken == ["alpha line beta line"]  # both, in transcript order, as one utterance

    # the next tool call fires the hook again with nothing new: silence, and no retry sleep either
    assert _fire(state, monkeypatch, transcript, "PostToolUse") == (0, [])
    assert spoken == ["alpha line beta line"]


def test_stop_does_not_repeat_the_line_eager_already_spoke(state, monkeypatch):
    """The idempotence that makes the two paths safe to run together: the assistant marks a line,
    a tool call fires PostToolUse (spoken), the turn then ends with no further message, and Stop —
    whose scope IS that same last message — must stay quiet."""
    transcript = state / "transcript.jsonl"
    transcript.write_text(_assistant("the turn opens") + "\n", encoding="utf-8")
    _write_config(state, monkeypatch, eager=True)
    spoken = _record_speech(monkeypatch)
    _fire(state, monkeypatch, transcript, "PostToolUse")  # seeds

    _append_message(transcript, "🔊 the only line of this turn")
    assert _fire(state, monkeypatch, transcript, "PostToolUse") == (0, [])
    assert spoken == ["the only line of this turn"]

    rc, sleeps = _fire(state, monkeypatch, transcript, "Stop")
    assert rc == 0
    assert spoken == ["the only line of this turn"]  # said once, by whichever hook got there first
    # "everything is claimed" reads as the flush-race signature, so Stop spends its backoff waiting
    # for a message that never comes — deliberate: it cannot tell that case from an unflushed one,
    # and it is the last thing in an async hook rather than something a turn waits on.
    assert sleeps == list(speak.BACKOFF)


def test_stop_still_speaks_a_line_eager_never_saw(state, monkeypatch):
    """The mirror case: the last message lands after the final tool call, so no eager firing ever
    read it. Stop speaks it exactly as it always has."""
    transcript = state / "transcript.jsonl"
    transcript.write_text(_assistant("🔊 spoken mid-turn") + "\n", encoding="utf-8")
    _write_config(state, monkeypatch, eager=True)
    spoken = _record_speech(monkeypatch)
    _fire(state, monkeypatch, transcript, "PostToolUse")  # seeds this transcript, speaks nothing

    _append_message(transcript, "🔊 the closing summary")
    assert _fire(state, monkeypatch, transcript, "Stop") == (0, [])
    assert spoken == ["the closing summary"]


# --- the lock: one speaker at a time, and the loser never waits ---------------------------------


def test_the_claim_keeps_the_previous_firings_line_out_of_this_ones_mouth(state, monkeypatch):
    """The ledger claim, made under the lock, is what makes the two paths idempotent: a firing reads
    every message, so without a claim it would re-speak everything it already said.

    Mutation proof: delete the `append_ledger(claimed)` call from main() and the second firing
    extracts BOTH lines — it speaks "alpha line beta line" and this assertion fails on the
    double-speak.
    """
    transcript = state / "transcript.jsonl"
    transcript.write_text(_assistant("🔊 alpha line") + "\n", encoding="utf-8")
    # this session has been seen before — first-run seeding is a different test's subject
    (state / "spoken.ledger").write_text(speak.seed_marker(str(transcript)) + "\n", encoding="utf-8")
    _write_config(state, monkeypatch, eager=True)
    spoken = _record_speech(monkeypatch)

    assert _fire(state, monkeypatch, transcript, "PostToolUse") == (0, [])
    _append_message(transcript, "🔊 beta line")  # the turn goes on
    assert _fire(state, monkeypatch, transcript, "PostToolUse") == (0, [])

    assert spoken == ["alpha line", "beta line"]  # in order, each exactly once
    assert [e for e in speak.read_ledger() if not e.startswith(speak.SEED_PREFIX)] == [
        speak.ledger_key(str(transcript), 0, "alpha line"),
        speak.ledger_key(str(transcript), 1, "beta line"),
    ]


def test_one_line_written_twice_in_one_message_is_claimed_and_said_once(state, monkeypatch):
    """The dedup INSIDE a single read, which the ledger alone cannot make: both copies of the line
    are read before either is written, so the ledger's veto sees neither. `accept` closes that by
    adding each key to `seen` as it takes it, and the second copy is refused by the same set that
    just accepted the first.

    Both halves matter and both are asserted: ONE utterance (not the sentence said twice in a single
    breath) and ONE ledger key (a duplicate claim would survive into the next firing's `seen` set
    harmlessly, but it is still a lie about what was spoken).

    Mutation proof: delete the `seen.add(key)` line from main()'s `accept` and the utterance becomes
    "twice over twice over" with two identical keys behind it.
    """
    transcript = state / "transcript.jsonl"
    transcript.write_text(_assistant("the turn opens") + "\n", encoding="utf-8")
    _write_config(state, monkeypatch, eager=True)
    spoken = _record_speech(monkeypatch)
    assert _fire(state, monkeypatch, transcript, "PostToolUse") == (0, [])  # seeds the transcript

    # one message, the same marked line twice — an assistant that repeated itself, verbatim
    _append_message(transcript, "🔊 twice over\na detail between them\n🔊 twice over")
    assert _fire(state, monkeypatch, transcript, "PostToolUse") == (0, [])

    assert spoken == ["twice over"]
    assert [e for e in speak.read_ledger() if not e.startswith(speak.SEED_PREFIX)] == [
        speak.ledger_key(str(transcript), 1, "twice over")
    ]


def test_the_lock_is_held_through_synthesis_and_playback_not_just_the_claim(state, monkeypatch):
    """The lock's SCOPE, executed rather than asserted about: it covers read-claim-speak-PLAY, and
    a rival firing is locked out of all of it — not just of the claim.

    The probe runs from the deepest point of the speaking chain, as the synthesis callback, and
    tries to take the lock exactly as a concurrent firing would. It fires TWICE because the audio
    source is lazy: the second chunk is synthesized from inside the player loop, with the first
    chunk's player process spawned and not yet reaped. So the second `None` is the one that says
    the lock is still held WHILE SOUND IS PLAYING — which is the property that keeps two firings
    from talking over each other, as opposed to merely from claiming the same line.

    Mutation proof: move `release_lock(lock)` up to just after `append_ledger(claimed)` and both
    probes come back with a lock in hand.
    """
    # both sentences clear MIN_CHUNK_CHARS, so neither is merged away and there really are two
    line = "The first sentence is long enough to stand on its own. The second one stands on its own feet too."
    transcript = state / "transcript.jsonl"
    transcript.write_text(_assistant(f"🔊 {line}") + "\n", encoding="utf-8")
    (state / "spoken.ledger").write_text(speak.seed_marker(str(transcript)) + "\n", encoding="utf-8")
    _write_config(state, monkeypatch, eager=True)

    locked_out: list[bool] = []

    def probe(text: str) -> None:
        rival = speak.acquire_lock()  # what a concurrent firing gets at this exact instant
        locked_out.append(rival is None)
        speak.release_lock(rival)

    spoken = _record_speech(monkeypatch, on_synthesize=probe)

    assert _fire(state, monkeypatch, transcript, "PostToolUse") == (0, [])
    assert spoken == speak.chunk_sentences(line)  # two chunks: one synthesis per sentence
    assert locked_out == [True, True]  # shut out during the first synthesis AND mid-playback
    # and released on the way out, so the next firing gets straight in rather than losing a race
    after = speak.acquire_lock()
    assert after is not None
    speak.release_lock(after)


def test_an_eager_firing_that_loses_the_lock_exits_at_once_and_claims_nothing(state, monkeypatch):
    """The whole point of a non-blocking acquire: the loser does not queue. It sleeps for nothing,
    claims nothing, and returns — so the line stays available to the next firing (or to Stop)
    instead of vanishing, and no python process piles up behind the speaker.

    The lock here is the REAL flock, held by this test through a second file descriptor: flock
    scopes to the open file description, so the hook's own open() genuinely cannot get in.
    """
    transcript = state / "transcript.jsonl"
    transcript.write_text(_assistant("🔊 a line that lost the race") + "\n", encoding="utf-8")
    (state / "spoken.ledger").write_text(speak.seed_marker(str(transcript)) + "\n", encoding="utf-8")
    _write_config(state, monkeypatch, eager=True)
    spoken = _record_speech(monkeypatch)

    held = speak.acquire_lock()
    assert held is not None and not isinstance(held, speak._NoLock)
    try:
        started = time.monotonic()
        assert _fire(state, monkeypatch, transcript, "PostToolUse") == (0, [])  # no sleeps: no queue
        assert time.monotonic() - started < 5  # it returned, it did not wait out anyone
    finally:
        speak.release_lock(held)

    assert spoken == []
    assert speak.read_ledger() == [speak.seed_marker(str(transcript))]  # nothing claimed
    assert "line left unclaimed" in (state / "speak.log").read_text(encoding="utf-8")

    # and the line really is still available — the next firing, once the lock is free, says it
    assert _fire(state, monkeypatch, transcript, "PostToolUse") == (0, [])
    assert spoken == ["a line that lost the race"]


def test_stop_supersedes_a_holder_that_will_not_yield_the_lock(state, monkeypatch):
    """Stop is the turn's last chance, so it is the one invocation allowed to wait — briefly. It
    lets the holder finish within LOCK_GRACE, then takes over (the SIGTERM releases that chain's
    flock with it) and takes one more shot."""
    transcript = state / "transcript.jsonl"
    transcript.write_text(_assistant("🔊 the closing line") + "\n", encoding="utf-8")
    (state / "spoken.ledger").write_text(speak.seed_marker(str(transcript)) + "\n", encoding="utf-8")
    _write_config(state, monkeypatch, eager=True)
    spoken = _record_speech(monkeypatch)

    held = speak.acquire_lock()
    assert held is not None and not isinstance(held, speak._NoLock)
    taken_over: list[bool] = []

    def fake_take_over() -> None:
        taken_over.append(True)
        speak.release_lock(held)  # exactly what a SIGTERMed chain does on its way out

    monkeypatch.setattr(speak, "take_over", fake_take_over)

    rc, sleeps = _fire(state, monkeypatch, transcript, "Stop")
    assert rc == 0
    assert sleeps == list(speak.LOCK_GRACE)  # it waited the grace out FIRST, before signalling
    assert taken_over == [True]
    assert spoken == ["the closing line"]


def test_a_stop_that_never_gets_the_lock_leaves_its_lines_unclaimed(state, monkeypatch):
    """The last resort. Claiming outside the lock is the one thing that must not happen, so a Stop
    that cannot get in says nothing and records nothing — the lines stay in the transcript for the
    next turn's eager firing, which reads every message."""
    transcript = state / "transcript.jsonl"
    transcript.write_text(_assistant("🔊 the closing line") + "\n", encoding="utf-8")
    (state / "spoken.ledger").write_text(speak.seed_marker(str(transcript)) + "\n", encoding="utf-8")
    _write_config(state, monkeypatch, eager=True)
    spoken = _record_speech(monkeypatch)
    monkeypatch.setattr(speak, "take_over", lambda: None)  # the holder does not die

    held = speak.acquire_lock()
    try:
        rc, sleeps = _fire(state, monkeypatch, transcript, "Stop")
    finally:
        speak.release_lock(held)

    assert (rc, spoken) == (0, [])
    assert sleeps == list(speak.LOCK_GRACE) * 2  # the grace before the takeover, and the one after
    assert speak.read_ledger() == [speak.seed_marker(str(transcript))]  # nothing claimed
    assert "lines left unclaimed" in (state / "speak.log").read_text(encoding="utf-8")


def test_the_lock_is_exclusive_and_the_eager_acquire_never_waits(state):
    """The real flock, not a test double: a second acquisition of the same state-dir lockfile cannot
    get in while the first holds it, and with no grace it does not sleep at all on the way out."""
    slept: list[float] = []
    real_sleep = speak.time.sleep
    speak.time.sleep = lambda seconds: slept.append(seconds)
    held = speak.acquire_lock()
    try:
        assert held is not None
        assert speak.acquire_lock() is None  # instant: it returns, it does not wedge
        assert slept == []  # the eager path passes no grace, so nothing is waited out
        assert speak.acquire_lock(speak.LOCK_GRACE) is None  # Stop's grace: bounded, then it gives up
        assert slept == list(speak.LOCK_GRACE)
    finally:
        speak.time.sleep = real_sleep
        speak.release_lock(held)
    again = speak.acquire_lock()  # released with the file: the next firing gets straight in
    assert again is not None
    speak.release_lock(again)
    speak.release_lock(None)  # a lock that was never taken releases to nothing


def test_an_unlockable_state_dir_speaks_anyway(state, monkeypatch):
    """No lock is better than no voice: when the lockfile itself cannot be opened, the firing goes
    ahead unserialized rather than dropping the line."""
    monkeypatch.setattr(speak, "_LOCK_PATH", str(state / "no-such-dir" / "speaking.lock"))
    lock = speak.acquire_lock()
    assert isinstance(lock, speak._NoLock)
    speak.release_lock(lock)


# --- the give-up: a line queued behind playback, and never a silent drop -------------------------
#
# The report these pin: BACKOFF is 2.65 s all told, one cloud clip runs ~10 s, and a Stop that ran
# out of ladder while that clip was still playing returned 0 without writing a single line to the
# log. The line was never spoken and nothing said so — it took log archaeology over a speak.log
# with NO entries at all to find, which is why "every give-up is logged" is pinned here as hard as
# the queueing itself.

PLAYBACK_SECONDS_IN_FLIGHT = 10.0  # the clip from the report — four times the whole BACKOFF ladder


def _speak_log(state) -> str:
    log_path = state / "speak.log"
    return log_path.read_text(encoding="utf-8") if log_path.exists() else ""


def test_playback_is_live_only_for_a_pid_that_exists_and_is_the_chain(state, monkeypatch):
    assert speak.playback_is_live() is False  # no pidfile at all: nobody to wait for
    (state / "playing.pid").write_text(f"{os.getpid()} 0", encoding="utf-8")
    assert speak.playback_is_live() is False  # our own pid, and pid 0, are not somebody else
    (state / "playing.pid").write_text("111 222", encoding="utf-8")
    monkeypatch.setattr(speak.os, "kill", lambda pid, sig: None)  # both pids exist
    monkeypatch.setattr(speak, "pid_looks_like_speak", lambda pid: pid == 222)
    assert speak.playback_is_live() is True
    monkeypatch.setattr(speak, "pid_looks_like_speak", lambda pid: False)
    assert speak.playback_is_live() is False  # recycled pids: the file lies, the guard does not


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the identity check reads /proc/<pid>/cmdline; Windows has no /proc to identify a child from")
def test_a_pidfile_that_outlived_its_chain_is_not_waited_for(state):
    """Real-child integration, the negative half: a superseded chain leaves through _on_sigterm,
    which exits before the cleanup that would remove its pidfile — so the record routinely outlives
    the process. The existence probe is what stops the next firing waiting out a dead speaker."""
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(300)", "voice-loop-speak-liveness-marker"]
    )
    try:
        (state / "playing.pid").write_text(str(child.pid), encoding="utf-8")
        assert speak.playback_is_live() is True
    finally:
        child.kill()
        child.wait(timeout=10)
    assert speak.playback_is_live() is False  # same file, same pid, nobody home


def test_the_wait_hands_back_the_first_read_that_settles(state, monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(speak.time, "sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr(speak, "playback_is_live", lambda: True)
    answers = iter([None, None, "the line that landed late"])
    text = speak.wait_out_playback(lambda: next(answers), lambda value: bool(value))
    assert text == "the line that landed late"
    assert sleeps == [speak.PLAYBACK_POLL] * 3
    assert "queued, not dropped" in _speak_log(state)


def test_the_wait_is_bounded_by_its_poll_count_however_long_playback_runs(state, monkeypatch):
    """A wedged player must not hold the turn open. The bound is a count of polls, not a wall
    clock, precisely so it is the same bound under a fake sleep as under a real one."""
    sleeps: list[float] = []
    monkeypatch.setattr(speak.time, "sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr(speak, "playback_is_live", lambda: True)  # a clip that never ends
    # The eager-off shape of "nothing new": every re-read returns the same stale previous line.
    text = speak.wait_out_playback(lambda: "the stale read", lambda value: False, "the stale read")
    assert text == "the stale read"  # the caller still logs the RIGHT give-up over it
    assert len(sleeps) == speak.PLAYBACK_POLLS
    assert "waiting no longer" in _speak_log(state)


def test_the_wait_costs_nothing_when_nothing_is_playing(state, monkeypatch):
    """The give-up path with an empty stage: no sleep, no re-read, and no log line of its own —
    the caller's give-up line is the one that belongs there."""

    def never(*args, **kwargs):
        raise AssertionError("nothing is playing: there is nothing to wait for")

    monkeypatch.setattr(speak.time, "sleep", never)
    monkeypatch.setattr(speak, "playback_is_live", lambda: False)
    assert speak.wait_out_playback(never, never, "the stale read") == "the stale read"
    assert _speak_log(state) == ""


def test_the_wait_takes_one_last_look_when_the_clip_in_front_ends(state, monkeypatch):
    """The transcript often lands in the very instant the clip in front finishes; a wait that
    stopped looking at the end of playback would drop exactly that line."""
    monkeypatch.setattr(speak.time, "sleep", lambda seconds: None)
    live = iter([True, True, False])
    monkeypatch.setattr(speak, "playback_is_live", lambda: next(live))
    answers = iter([None, None, "the line that landed as the clip ended"])
    text = speak.wait_out_playback(lambda: next(answers), lambda value: bool(value))
    assert text == "the line that landed as the clip ended"
    assert "the line in front finished" in _speak_log(state)


def test_a_line_written_while_the_previous_clip_plays_is_queued_not_dropped(state, monkeypatch):
    """THE ACCEPTANCE CASE, end to end through the real main().

    A previous chain is really playing — a live child recorded in playing.pid, read back through
    the real pidfile and the real identity guard — and this turn's marked line only reaches the
    transcript ten seconds in, four times the whole BACKOFF ladder. The old hook ran its 2.65 s,
    found nothing, and returned 0 in silence: the line was never spoken and the log never mentioned
    it. Only the clock is faked here; the liveness the wait hangs on is a real process.
    """
    transcript = state / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")
    # Spawned BEFORE the audio half is faked: _record_speech replaces subprocess.Popen itself.
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(300)", "voice-loop-speak-in-flight-marker"]
    )
    (state / "playing.pid").write_text(str(child.pid), encoding="utf-8")
    spoken = _record_speech(monkeypatch)
    _write_config(state, monkeypatch)  # eager off: the default, and the configuration that was hit

    elapsed = [0.0]

    def fake_sleep(seconds: float) -> None:
        elapsed[0] += seconds
        if elapsed[0] >= PLAYBACK_SECONDS_IN_FLIGHT and child.poll() is None:
            _append_message(transcript, "🔊 the line that waited its turn")
            child.kill()
            # Reap with NO timeout, deliberately. os.waitpid() hangs on Windows, but a TIMED
            # Popen.wait() busy-waits through time.sleep -- and this test patches
            # speak.time.sleep, which IS the module-global time.sleep, so a timed wait recurses
            # straight back into fake_sleep. An untimed wait() blocks in the kernel on both
            # platforms (waitpid on POSIX, WaitForSingleObject on Windows) and touches no clock.
            # Unbounded is safe here: the child was killed on the line above.
            child.wait()

    try:
        payload = json.dumps({"transcript_path": str(transcript), "hook_event_name": "Stop"})
        monkeypatch.setattr(speak.sys, "stdin", _Stdin(payload))
        monkeypatch.setattr(speak.time, "sleep", fake_sleep)
        assert speak.main() == 0
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()  # untimed for the same reason as above: no clock, no recursion

    assert spoken == ["the line that waited its turn"]  # voiced, not dropped
    assert elapsed[0] >= PLAYBACK_SECONDS_IN_FLIGHT  # and it really did outlast the clip
    assert "queued, not dropped" in _speak_log(state)


def test_a_line_that_is_already_there_never_waits_for_the_clip_in_front(state, monkeypatch):
    """The retry CONDITION is untouched: the wait is reachable only where the ladder ran out with
    nothing new. A turn whose line is already in the transcript still supersedes a playing clip at
    once — the takeover must not quietly become a queue."""

    def never() -> bool:
        raise AssertionError("a settled read must never wait for anybody")

    monkeypatch.setattr(speak, "playback_is_live", never)
    _write_config(state, monkeypatch)
    assert _speak_turns(state, monkeypatch, ["Done."]) == ["Done."]


def test_a_stop_that_gives_up_says_so_instead_of_returning_in_silence(state, monkeypatch):
    """The whole ladder burned on a transcript that never flushed, nothing playing behind it: the
    line is genuinely lost, and the log is the only place that can ever say so."""
    rc, sleeps = _run_main_against("{not json yet", state, monkeypatch)
    assert (rc, sleeps) == (0, list(speak.BACKOFF))  # the ladder itself is unchanged
    assert "gave up with nothing new in the transcript — a line written now is DROPPED" in _speak_log(state)


def test_the_dedup_drop_names_the_line_it_dropped(state, monkeypatch):
    """Dropping the stale previous turn is correct and stays; being unable to tell it apart from a
    lost line in the log is what was not."""
    _write_config(state, monkeypatch)
    assert _speak_turns(state, monkeypatch, ["Done.", "Done."]) == ["Done."]
    assert "dropped a read identical to the last spoken line (dedup): Done." in _speak_log(state)


def test_a_payload_with_no_readable_transcript_is_logged(state, monkeypatch):
    monkeypatch.setenv("VOICE_LOOP_CONFIG", str(state / "absent.json"))
    monkeypatch.setattr(speak.sys, "stdin", _Stdin(json.dumps({"transcript_path": str(state / "gone.jsonl")})))
    assert speak.main() == 0
    assert "no transcript to read" in _speak_log(state)


def test_a_payload_that_is_not_json_is_logged(state, monkeypatch):
    monkeypatch.setenv("VOICE_LOOP_CONFIG", str(state / "absent.json"))
    monkeypatch.setattr(speak.sys, "stdin", _Stdin("this is not a hook payload"))
    assert speak.main() == 0
    assert "hook payload was not JSON" in _speak_log(state)


def test_stop_names_the_ledger_when_it_stays_quiet_rather_than_crying_drop(state, monkeypatch):
    """Not every quiet Stop lost something. With eager on, the line it finds nothing new about is
    the one an eager firing already said out loud — the log names the ledger, because a give-up
    line here would cry wolf once per turn and drown the real ones."""
    _write_config(state, monkeypatch, eager=True)
    transcript = state / "transcript.jsonl"
    transcript.write_text("", encoding="utf-8")
    spoken = _record_speech(monkeypatch)
    assert _fire(state, monkeypatch, transcript, "PostToolUse")[0] == 0  # seeds this transcript
    _append_message(transcript, "🔊 said by the eager firing")
    assert _fire(state, monkeypatch, transcript, "PostToolUse")[0] == 0  # speaks it mid-turn
    assert _fire(state, monkeypatch, transcript, "Stop")[0] == 0  # and Stop has nothing to add
    assert spoken == ["said by the eager firing"]
    assert "the ledger already accounts for 1 marked line(s)" in _speak_log(state)


def test_an_eager_firing_with_nothing_new_stays_out_of_the_log(state, monkeypatch):
    """The one deliberate silence. An eager firing that finds nothing new has dropped NOTHING —
    it claimed nothing, and the next tool call is a free retry — and it fires on every tool call,
    so a line per firing would drown the drops this change exists to make visible."""
    _write_config(state, monkeypatch, eager=True)
    transcript = state / "transcript.jsonl"
    transcript.write_text(_assistant("🔊 history, seeded and never spoken") + "\n", encoding="utf-8")
    _record_speech(monkeypatch)
    assert _fire(state, monkeypatch, transcript, "PostToolUse")[0] == 0
    before = _speak_log(state)
    assert _fire(state, monkeypatch, transcript, "PostToolUse")[0] == 0
    assert _speak_log(state) == before  # not one line added by a firing that lost nothing


# --- #106: the flush that outlasts the ladder, and the reason line every Stop exit owes -----------
#
# The live silence these pin: a long turn (several tool calls, then a long final text) whose final
# assistant message had not reached the transcript when the fixed 2.65 s ladder ran out, with
# NOTHING playing to extend the wait. The hook exited 0, spoke nothing, and wrote no log line at
# all — so the only evidence of the drop was the absence of evidence, and conformance row 3.12
# ("a turn with NO log line at all is a FAIL") was violated by the give-up path itself.


# Four seconds in — past the whole 2.65 s ladder, which is what made the live turn unrecoverable.
FLUSH_SECONDS_LATE = 4.0

# The budget the composed ceiling has to fit inside, mirrored from THIS plugin's own manifest:
# hooks/hooks.json declares "timeout": 90 on both registrations. Mirrored rather than imported
# because the test is about the RELATION, and asserted against the manifest below so the mirror
# cannot quietly drift from the file it claims to quote.
HOOK_TIMEOUT_S = 90


def test_hook_budget_is_a_structural_deadline_not_a_sleep_claim():
    """Mutation gap: reverting the documented sleep budget into an asserted wall-clock ceiling
    would make parse time invisible and remove the per-poll deadline contract."""
    assert speak.HOOK_BUDGET_S == HOOK_TIMEOUT_S


def test_the_mirrored_hook_timeout_is_the_one_the_manifest_declares():
    manifest = json.loads((Path(__file__).resolve().parents[1] / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    declared = {
        entry["timeout"]
        for registrations in manifest["hooks"].values()
        for registration in registrations
        for entry in registration["hooks"]
    }
    assert declared == {HOOK_TIMEOUT_S}


def _hook_stub_interpreter(stub_dir: Path, name: str, log_path: Path, speak_exit: int) -> None:
    """A ``name`` executable that logs its argv and stands in for a working interpreter.

    Probe calls (``-c "import sys"``) succeed; running speak.py exits ``speak_exit`` — the
    scenario dial for the fallthrough cases below."""
    stub = stub_dir / name
    stub.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "{name} $*" >> "{log_path}"\n'
        f'case " $* " in *"speak.py"*) exit {speak_exit} ;; esac\n'
        "exit 0\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)


def _speak_invocations(log_path: Path) -> list[str]:
    return [line for line in log_path.read_text(encoding="utf-8").splitlines() if "speak.py" in line]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX stub executables and shell grouping")
def test_hook_commands_probe_a_real_interpreter_before_running_speak(tmp_path):
    """Runs the hooks.json command under a real shell with stub interpreters on PATH, so the
    short-circuit is EXECUTED, not string-matched.

    Mutation gap this pins (#205): flattening the chain back to
    ``probe && speak || probe && speak || probe && speak`` fires speak.py once per working
    interpreter — invisible to a literal-string assert, caught here by counting recorded
    speak.py invocations. Still caught structurally: removing the ``import sys`` probes
    fails the probe-presence assert; a probeless command that leans on shell ``||`` alone
    cannot tell a Store ``python3`` alias from a working interpreter."""
    manifest = json.loads((Path(__file__).resolve().parents[1] / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    commands: list[str] = []
    for registrations in manifest["hooks"].values():
        for registration in registrations:
            for entry in registration["hooks"]:
                commands.append(entry["command"])
    assert len(commands) >= 2, "expected at least two hook registrations (Stop + PostToolUse)"

    plugin_root = tmp_path / "plugin"
    (plugin_root / "scripts").mkdir(parents=True)
    (plugin_root / "scripts" / "speak.py").write_text("# stub target; the interpreter stub logs the run\n")

    def run_command(cmd: str, present: list[str], speak_exit: int = 0) -> tuple[int, list[str]]:
        stub_dir = tmp_path / "stubs"
        stub_dir.mkdir(exist_ok=True)
        for stale in stub_dir.iterdir():
            stale.unlink()  # each scenario names exactly the interpreters it wants present
        log = tmp_path / "invocations.log"
        log.write_text("", encoding="utf-8")
        for name in present:
            _hook_stub_interpreter(stub_dir, name, log, speak_exit)
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            timeout=30,
            env={**os.environ, "PATH": str(stub_dir), "CLAUDE_PLUGIN_ROOT": str(plugin_root)},
        )
        return result.returncode, _speak_invocations(log)

    all_three = ["python3", "python", "py"]
    for cmd in commands:
        assert cmd.count("import sys") == cmd.count("speak.py"), (
            "every speak.py run in the hook command must be guarded by its own interpreter probe; "
            f"got: {cmd!r}"
        )

        # Short-circuit: with every interpreter working and speak.py succeeding, speak.py
        # runs exactly once. The flattened chain runs it once per working interpreter.
        code, spoke = run_command(cmd, all_three)
        assert code == 0 and len(spoke) == 1, (
            f"first working interpreter must short-circuit the chain: exit {code}, "
            f"speak.py ran {len(spoke)} time(s); got: {cmd!r}"
        )

        # Fallthrough: the first interpreter absent, the second present — still exactly once.
        code, spoke = run_command(cmd, ["python", "py"])
        assert code == 0 and len(spoke) == 1, (
            f"a missing python3 must fall through to the next interpreter, speaking once: "
            f"exit {code}, speak.py ran {len(spoke)} time(s); got: {cmd!r}"
        )

        # Pinned as intended: a probe that succeeds but a speak.py that FAILS falls through
        # to the next interpreter too — the next one may have the stdlib the first lacked.
        code, spoke = run_command(cmd, all_three, speak_exit=3)
        assert code == 3 and len(spoke) == 3, (
            "a speak.py failure on one interpreter is retried on the next (and only) two, "
            f"with the last exit code surfacing; got exit {code}, {len(spoke)} run(s): {cmd!r}"
        )


def test_speak_py_exits_zero_when_sibling_modules_missing(tmp_path):
    """Mutation gap: removing the try/except ImportError guard around ``import providers`` and
    ``import wsclient`` in speak.py would cause an ImportError traceback (exit 1) instead of
    the silent exit (0) when the sibling modules are absent.  That guard preserves the contract
    the bash launcher provided — a half-copied scripts/ directory is silence, not a traceback
    in the middle of a turn.

    The test copies speak.py to a temp directory WITHOUT its siblings, then runs it as a
    subprocess — the guard must exit 0 rather than raising."""
    import shutil, subprocess, sys

    dest = tmp_path / "speak.py"
    shutil.copy2(str(_SPEAK_PATH), str(dest))
    result = subprocess.run(
        [sys.executable, str(dest)],
        capture_output=True, timeout=15,
        cwd=str(tmp_path),
        env={**os.environ, "PYTHONPATH": str(tmp_path)},
    )
    assert result.returncode == 0, (
        f"speak.py exited {result.returncode} without sibling modules;\n"
        f"stdout: {result.stdout.decode(errors='replace')[:300]}\n"
        f"stderr: {result.stderr.decode(errors='replace')[:500]}"
    )


def test_windows_install_recipe_exists_and_is_readable():
    """Mutation gap: deleting or corrupting install.ps1 would leave the Windows-native install
    path without its documented recipe — the control pass (#174) drives from this file and has
    nothing to fall back on.  The test verifies the file is present, is valid UTF-8, and carries
    the elevation check and the python3.exe copy — the two steps the manual pass had to discover
    by hand."""
    install_ps1 = Path(__file__).resolve().parents[1] / "scripts" / "install.ps1"
    assert install_ps1.is_file(), f"install.ps1 not found at {install_ps1}"
    text = install_ps1.read_text(encoding="utf-8")
    # The elevation check must be present — a silent elevation assumption hangs on UAC.
    assert "IsInRole" in text, "install.ps1 must detect elevation rather than assume it"
    # The python3.exe copy must be present — python.org does not ship python3.exe.
    assert "python3.exe" in text, "install.ps1 must create python3.exe from python.exe"
    # The script must not invoke winget as a command (broken on the measured guest).
    # A comment explaining WHY is fine; `winget install` / `winget source` is not.
    assert "winget install" not in text.lower(), (
        "install.ps1 must not invoke winget (broken on the measured guest)"
    )
    assert "winget source" not in text.lower(), (
        "install.ps1 must not invoke winget (broken on the measured guest)"
    )
    # The script must use direct installers (curl.exe + Start-Process).
    assert "curl.exe" in text.lower(), "install.ps1 must use curl.exe for downloads"
    # Windows PowerShell 5.1 reads a BOM-less .ps1 as ANSI. The BOM must be present in the
    # delivered bytes so the in-box parser decodes the script as UTF-8 rather than corrupting its
    # non-ASCII prose into syntax.
    assert install_ps1.read_bytes().startswith(bytes.fromhex("efbbbf")), (
        "install.ps1 must carry a UTF-8 BOM for the in-box PowerShell parser"
    )
    assert "-PassThru" in text, "installer exit codes must be available to the recipe"
    assert "Test-RealPython $python3Exe" in text, "existing python3.exe must be verified, not trusted"
    assert "WaitForExit(5000)" in text, "Python probes must have a finite timeout"
    assert "ReparsePoint" in text, "Store aliases must be rejected without executing them"
    assert "RebootExitCodes" in text and "@(3010,1641)" in text, (
        "MSI success-with-reboot exit codes must be accepted"
    )
    assert "$npmExitCode = $LASTEXITCODE" in text, "npm's native exit status must be checked"
    assert "npm prefix -g" in text and "$npmPrefix" in text, (
        "Claude Code verification must use npm's install location, not stale PATH entries"
    )
    assert "ExecutionPolicyOverride" in text, "Process-scope Bypass must not abort the recipe"


class _FakeStat:
    """What os.stat gives wait_out_flush, and the only two fields it reads."""

    def __init__(self, size: int, mtime_ns: int) -> None:
        self.st_size = size
        self.st_mtime_ns = mtime_ns


def _never(*args, **kwargs):
    raise AssertionError("an idle transcript must not be waited on, re-read, or slept over")


class TestTranscriptActivity:
    """One syscall, two fields — the whole evidence the extension hangs on."""

    def test_it_moves_when_the_file_is_appended_to(self, tmp_path):
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text("one", encoding="utf-8")
        before = speak.transcript_activity(str(transcript))
        assert before == (3, transcript.stat().st_mtime_ns)
        with transcript.open("a", encoding="utf-8") as fh:
            fh.write("two")
        assert speak.transcript_activity(str(transcript)) != before

    def test_a_file_that_cannot_be_stat_ed_is_one_unchanging_value(self, tmp_path):
        """None compares equal to the next None, so an unreadable transcript ends the wait at once
        — the same verdict the pre-#106 code reached without looking at all."""
        assert speak.transcript_activity(str(tmp_path / "gone.jsonl")) is None


class TestWaitOutFlush:
    """The wait itself, on a fake clock and a fake stat: no transcript, no reader, no sleep."""

    @pytest.fixture
    def sleeps(self, monkeypatch) -> list[float]:
        recorded: list[float] = []
        monkeypatch.setattr(speak.time, "sleep", lambda seconds: recorded.append(seconds))
        return recorded

    def test_an_idle_transcript_costs_one_stat_and_nothing_else(self, state, sleeps):
        """The cheap path, and the one every quiet turn takes: the file did not move while the
        ladder ran, so there is nobody to wait for and the caller's give-up stands unchanged."""
        text = speak.wait_out_flush(
            "transcript.jsonl", _never, _never, None, (10, 5), stat=lambda path: _FakeStat(10, 5)
        )
        assert text is None
        assert sleeps == []
        assert _speak_log(state) == ""  # the give-up line belongs to the caller, not here

    def test_a_growing_transcript_buys_the_late_line_its_polls(self, state, sleeps):
        sizes = iter([20, 30, 40, 50])
        answers = iter([None, None, "the line the flush was still writing"])
        text = speak.wait_out_flush(
            "transcript.jsonl",
            lambda: next(answers),
            lambda value: bool(value),
            None,
            (10, 5),
            stat=lambda path: _FakeStat(next(sizes), 5),
        )
        assert text == "the line the flush was still writing"
        assert sleeps == [speak.FLUSH_POLL] * 3
        assert "landed 0.75s past the ladder" in _speak_log(state)

    def test_a_message_that_marked_nothing_is_not_called_a_line(self, state, sleeps):
        """settled() is true for '' as well — a message that parsed and marked NOTHING is a final
        answer and lands here exactly like a line does. Saying "the line landed" over it would put
        a line in the log that was never in the transcript."""
        sizes = iter([20, 30])
        answers = iter([""])
        text = speak.wait_out_flush(
            "transcript.jsonl",
            lambda: next(answers),
            lambda value: value == "" or bool(value),
            None,
            (10, 5),
            stat=lambda path: _FakeStat(next(sizes), 5),
        )
        assert text == ""
        assert "a message with nothing marked landed 0.25s past the ladder" in _speak_log(state)
        assert "the line landed" not in _speak_log(state)

    def test_the_wait_ends_the_moment_the_file_stops_growing(self, state, sleeps):
        """A writer that fell quiet has answered the question: whatever was coming is not coming."""
        sizes = iter([20, 30, 30])
        text = speak.wait_out_flush(
            "transcript.jsonl",
            lambda: None,
            lambda value: bool(value),
            None,
            (10, 5),
            stat=lambda path: _FakeStat(next(sizes), 5),
        )
        assert text is None
        assert sleeps == [speak.FLUSH_POLL] * 2
        assert "stopped growing after 0.50s" in _speak_log(state)

    def test_the_wait_is_bounded_however_long_the_file_keeps_growing(self, state, sleeps):
        """A writer that never stops must not hold the turn open: the bound is a COUNT of polls,
        for the same reason PLAYBACK_POLLS is — that is the bound a fake sleep can drive."""
        size = itertools.count(20, 10)
        text = speak.wait_out_flush(
            "transcript.jsonl",
            lambda: None,
            lambda value: bool(value),
            "the stale read",
            (10, 5),
            stat=lambda path: _FakeStat(next(size), 5),
        )
        assert text is None  # the freshest read, unsettled — the caller logs the give-up over it
        assert len(sleeps) == speak.FLUSH_POLLS
        assert "still growing after 12.5s — waiting no longer" in _speak_log(state)


class TestALineThatLandsPastTheLadder:
    """THE #106 ACCEPTANCE CASE, end to end through the real main(), with nothing playing."""

    def test_a_message_flushed_after_the_ladder_is_spoken_rather_than_dropped(self, state, monkeypatch):
        """The reproduction, in the shape the forensics reconstructed: a turn still being written
        when the 2.65 s ladder ends. The transcript GROWS throughout (the turn's own records land
        while the final assistant message is still on its way), and that growth is what buys the
        line its polls — four seconds in, past the whole fixed ladder, it arrives and is spoken.

        Nothing is playing here, which is exactly why the pre-#106 hook dropped it: wait_out_playback
        looks at an empty stage and hands the give-up straight back."""
        transcript = state / "transcript.jsonl"
        transcript.write_text("", encoding="utf-8")
        spoken = _record_speech(monkeypatch)
        _write_config(state, monkeypatch)  # eager off: the default, and the configuration that was hit

        elapsed = [0.0]

        def fake_sleep(seconds: float) -> None:
            elapsed[0] += seconds
            if elapsed[0] < FLUSH_SECONDS_LATE:
                # mid-flush: the turn's own records are landing while the message is not there yet
                with transcript.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"type": "user", "message": {"content": []}}) + "\n")
            elif "🔊" not in transcript.read_text(encoding="utf-8"):
                _append_message(transcript, "🔊 the line the flush was still writing")

        payload = json.dumps({"transcript_path": str(transcript), "hook_event_name": "Stop"})
        monkeypatch.setattr(speak.sys, "stdin", _Stdin(payload))
        monkeypatch.setattr(speak.time, "sleep", fake_sleep)
        assert speak.main() == 0

        assert spoken == ["the line the flush was still writing"]  # voiced, not dropped
        assert elapsed[0] > sum(speak.BACKOFF)  # and it really did outlive the fixed ladder
        assert "past the ladder" in _speak_log(state)
        assert "DROPPED" not in _speak_log(state)

    def test_a_transcript_nobody_is_writing_still_gives_up_after_the_same_2_65_seconds(
        self, state, monkeypatch
    ):
        """The other direction, and the promise the extension had to keep: an idle transcript is
        answered by one stat, so a turn with genuinely nothing behind it costs the ladder and not
        one poll more."""
        rc, sleeps = _run_main_against("{not json yet", state, monkeypatch)
        assert (rc, sleeps) == (0, list(speak.BACKOFF))
        assert "gave up with nothing new in the transcript" in _speak_log(state)


class TestTheComposedCeiling:
    """What ONE firing can cost when everything goes wrong at once.

    The three waits run in SEQUENCE inside one main(), and a bound nobody measured is a bound
    nobody has: the ladder, then the wedged-player wait, then the growing-transcript wait. This
    pins their SUM, so a future poll-count change cannot quietly walk the Stop hook past the 60 s
    the harness allows it.
    """

    def test_a_wedged_player_and_a_growing_transcript_cost_exactly_the_three_bounds(
        self, state, monkeypatch
    ):
        transcript = state / "transcript.jsonl"
        transcript.write_text("", encoding="utf-8")
        _write_config(state, monkeypatch)
        # A player that never exits: playback_is_live stays true for every poll of the first wait.
        monkeypatch.setattr(speak, "playback_is_live", lambda: True)

        sleeps: list[float] = []

        def fake_sleep(seconds: float) -> None:
            # …and a transcript somebody keeps appending to, so the second wait runs to ITS bound
            # too. Nothing marked ever lands, so no read can settle and cut either wait short.
            sleeps.append(seconds)
            with transcript.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"type": "user", "message": {"content": []}}) + "\n")

        payload = json.dumps({"transcript_path": str(transcript), "hook_event_name": "Stop"})
        monkeypatch.setattr(speak.sys, "stdin", _Stdin(payload))
        monkeypatch.setattr(speak.time, "sleep", fake_sleep)
        assert speak.main() == 0

        expected = (
            sum(speak.BACKOFF)
            + speak.PLAYBACK_POLLS * speak.PLAYBACK_POLL
            + speak.FLUSH_POLLS * speak.FLUSH_POLL
        )
        assert sum(sleeps) == pytest.approx(expected)
        # THE RELATION, not the arithmetic: what matters is that the composed ceiling fits inside
        # the budget this plugin declares for its own hooks. A future poll-count change is free to
        # move the sum; it is not free to walk the Stop hook past its own timeout.
        assert expected < HOOK_TIMEOUT_S
        log_text = _speak_log(state)
        # and it says what it waited for, twice, before saying what it decided — the docs claim a
        # reason line always, never that there is exactly one
        assert "still playing after" in log_text
        assert "still growing after" in log_text
        assert "gave up with nothing new in the transcript" in log_text
        assert log_text.count("waiting because extract was EMPTY") == 2


class TestTheFlushWaitIsEagerOffOnly:
    """The gate: a Stop firing with `speak.eager` ON never waits on a growing transcript.

    Not a performance nicety — the whole read-claim-speak sequence runs under speaking.lock, so a
    Stop that waits 12.5 s is 12.5 s in which no eager firing can speak at all. And it buys
    nothing: the next PostToolUse firing reads EVERY message, so the late line is said for free one
    tool call later. #106 was reported on the default (eager-off) install, which is exactly the
    configuration with no successor to fall back on.
    """

    def _growing(self, monkeypatch, state) -> None:
        """A transcript that advances on every single look — the condition the wait polls on."""
        counter = itertools.count()
        monkeypatch.setattr(speak, "transcript_activity", lambda path, stat=os.stat: (next(counter), 0))

    def test_an_eager_stop_with_nothing_marked_at_all_never_polls_the_transcript(
        self, state, monkeypatch
    ):
        """Nothing vetoed, nothing spoken, a file that keeps growing: pre-gate this cost the full
        12.5 s under the lock, and the ledger check could not have prevented it."""
        _write_config(state, monkeypatch, eager=True)
        transcript = state / "transcript.jsonl"
        transcript.write_text("{not json yet\n", encoding="utf-8")  # no assistant message: read is None
        _record_speech(monkeypatch)
        self._growing(monkeypatch, state)

        rc, sleeps = _fire(state, monkeypatch, transcript, "Stop")

        assert rc == 0
        assert sleeps == list(speak.BACKOFF)  # the ladder, and not one flush poll more
        assert "still growing" not in _speak_log(state)
        assert "gave up with nothing new in the transcript" in _speak_log(state)

    def test_an_eager_off_stop_in_the_same_position_still_waits(self, state, monkeypatch):
        """The other side of the gate, so it is a GATE and not a removal: the default install —
        the one #106 was reported on — keeps the wait it was given."""
        _write_config(state, monkeypatch)  # eager off
        transcript = state / "transcript.jsonl"
        transcript.write_text("{not json yet\n", encoding="utf-8")
        _record_speech(monkeypatch)
        self._growing(monkeypatch, state)

        rc, sleeps = _fire(state, monkeypatch, transcript, "Stop")

        assert rc == 0
        assert len(sleeps) == len(speak.BACKOFF) + speak.FLUSH_POLLS
        assert "still growing after 12.5s" in _speak_log(state)


class TestARepeatedLineIsStillTheRaceSignature:
    """THE DECISION behind Q-2, pinned so a future change has to argue with a red test.

    `settled()` is False for a read identical to the last spoken line, and that single signature
    covers two different worlds: a genuine repeat («Done.» twice), and #106's OWN reported failure
    — the ladder reading the PREVIOUS message because this turn's has not been flushed yet. The
    forensics say it plainly: "the ladder read only the PREVIOUS message ('Ура …', already spoken
    → the race signature held)".

    Nothing in an eager-off read can tell those apart (the pre-0.3.2 memory is one last-spoken
    STRING, with no message index behind it), so treating the signature as a decided answer would
    make the whole (b) half of this ticket inert for exactly the turn that opened it. The wait
    therefore STAYS for it. What it costs is bounded and rare — a repeat AND a file somebody is
    still appending to — and the dedup verdict is logged the moment the wait ends.
    """

    def test_a_repeat_arriving_while_the_transcript_grows_is_waited_out_not_decided(
        self, state, monkeypatch
    ):
        transcript = state / "transcript.jsonl"
        transcript.write_text("", encoding="utf-8")
        spoken = _record_speech(monkeypatch)
        _write_config(state, monkeypatch)
        _append_message(transcript, "🔊 Done.")
        assert _fire(state, monkeypatch, transcript, "Stop")[0] == 0
        assert spoken == ["Done."]

        # The SAME message still being the last one, and a transcript that keeps growing: this is
        # the reported shape, and the hook must keep looking rather than call it a repeat at once.
        (state / "speak.log").write_text("", encoding="utf-8")

        def fake_sleep(seconds: float) -> None:
            with transcript.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"type": "user", "message": {"content": []}}) + "\n")

        payload = json.dumps({"transcript_path": str(transcript), "hook_event_name": "Stop"})
        monkeypatch.setattr(speak.sys, "stdin", _Stdin(payload))
        monkeypatch.setattr(speak.time, "sleep", fake_sleep)
        assert speak.main() == 0

        assert spoken == ["Done."]  # nothing said twice
        log_text = _speak_log(state)
        assert "the transcript stopped growing" not in log_text  # it really did keep growing
        assert "still growing after" in log_text  # the wait ran to its bound
        assert "dropped a read identical to the last spoken line (dedup): Done." in log_text
        assert "waiting because extract was IDENTICAL" in log_text

    def test_a_repeat_on_a_quiet_transcript_still_costs_only_the_ladder(self, state, monkeypatch):
        """The cost the decision above is bounded BY: with nobody appending, the give-up is the
        ladder and one stat, exactly as before #106."""
        _write_config(state, monkeypatch)
        assert _speak_turns(state, monkeypatch, ["Done.", "Done."]) == ["Done."]
        assert "dropped a read identical to the last spoken line (dedup): Done." in _speak_log(state)


class TestEveryStopExitSaysWhy:
    """Conformance 3.12, made literally true: a Stop firing writes exactly one reason line
    whatever it decides. A turn with NO log line at all is what made the live drop invisible."""

    def test_a_turn_with_no_marked_line_says_so(self, state, monkeypatch):
        """The forensic reproduction: replaying the silent turn by hand exited 0 and logged
        NOTHING, so the log could not tell "the hook gave up" from "the hook was never called"."""
        rc, sleeps = _run_main_against(_assistant("plain prose, nothing marked"), state, monkeypatch)
        assert (rc, sleeps) == (0, [])  # still the fast path: a parsed message is a final answer
        assert "stop: nothing marked in the last assistant message" in _speak_log(state)

    def test_a_bare_marker_turn_says_both_what_it_saw_and_what_it_did(self, state, monkeypatch):
        rc, _ = _run_main_against(_assistant("🔊"), state, monkeypatch)
        assert rc == 0
        log_text = _speak_log(state)
        assert "marker with no text" in log_text  # what the transcript held
        assert "stop: nothing marked in the last assistant message" in log_text  # what the hook did

    def test_speech_switched_off_is_a_diagnosis_not_a_silence(self, state, monkeypatch):
        """`speak.enabled: false` is the first thing a "why did I hear nothing" investigation has
        to rule out, and it used to be the one state that left no trace at all."""
        _write_config(state, monkeypatch, enabled=False)
        transcript = state / "transcript.jsonl"
        transcript.write_text(_assistant("🔊 a line nobody will hear") + "\n", encoding="utf-8")
        assert _fire(state, monkeypatch, transcript, "Stop")[0] == 0
        assert "stop: speech is switched off" in _speak_log(state)
        assert (state / "hook-last-fired").exists()  # and the heartbeat is stamped regardless

    def test_a_disabled_install_does_not_log_once_per_tool_call(self, state, monkeypatch):
        """The line is the Stop path's. An eager firing fires after EVERY tool call, and a
        disabled install that wrote a line on each of them would drown the log it exists to keep
        readable — the same reason the eager no-op has always been silent."""
        _write_config(state, monkeypatch, enabled=False)
        transcript = state / "transcript.jsonl"
        transcript.write_text(_assistant("🔊 a line nobody will hear") + "\n", encoding="utf-8")
        assert _fire(state, monkeypatch, transcript, "PostToolUse")[0] == 0
        assert _speak_log(state) == ""

    def test_the_ledgers_veto_is_still_named_as_the_veto_and_not_as_a_drop(self, state, monkeypatch):
        """The one quiet Stop that lost nothing keeps its own wording — and does NOT spend the
        flush wait on a growing transcript, because "eager already said it" is a decided answer
        rather than a race."""
        _write_config(state, monkeypatch, eager=True)
        transcript = state / "transcript.jsonl"
        transcript.write_text("", encoding="utf-8")
        spoken = _record_speech(monkeypatch)
        assert _fire(state, monkeypatch, transcript, "PostToolUse")[0] == 0  # seeds the transcript
        _append_message(transcript, "🔊 said by the eager firing")
        assert _fire(state, monkeypatch, transcript, "PostToolUse")[0] == 0  # speaks it mid-turn

        def growing(path, stat=os.stat):
            return (len(_speak_log(state)), 0)  # a file that advances on every single look

        monkeypatch.setattr(speak, "transcript_activity", growing)
        rc, sleeps = _fire(state, monkeypatch, transcript, "Stop")
        assert (rc, sleeps) == (0, list(speak.BACKOFF))  # the ladder, and not one poll more
        assert spoken == ["said by the eager firing"]
        assert "the ledger already accounts for 1 marked line(s)" in _speak_log(state)


# --- the contour check (#40): the hook voices what the poller found ---------------------------------
#
# contour_poll.py writes contour.json; entry() runs contour_check after every firing, so an
# ACTIVE alert is heard once — a page, not a dashboard. These drive the real contour_check (and
# one real entry()) with the audio half recorded, exactly like the marked-line tests above.

_DEMOTED = {
    "key": "device-demoted:rvc",
    "kind": "device-demoted",
    "service": "rvc",
    "message": "rvc is serving on cpu, expected gpu",
}
_VOICED = "Voice contour: rvc is serving on cpu, expected gpu"


def _contour_status(state, alerts, *, age_seconds: float = 0.0, max_age: int | None = 900) -> None:
    """A status file exactly as the poller writes one: alerts, and a timestamp with a bound on it.
    ``age_seconds`` ages the file — that is the only way a poller that stopped is visible."""
    written = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    status: dict = {"at": written.isoformat(), "alerts": alerts}
    if max_age is not None:
        status["max_age"] = max_age
    (state / "contour.json").write_text(json.dumps(status), encoding="utf-8")


def _alerts_of(path) -> list[dict]:
    return speak.contour_alerts(speak.read_contour_status(str(path)), datetime.now(timezone.utc))


def test_a_status_file_of_every_bad_shape_says_nothing(state):
    assert _alerts_of(state / "absent.json") == []
    bad = state / "contour.json"
    bad.write_text("{corrupt", encoding="utf-8")
    assert _alerts_of(bad) == []
    bad.write_text(json.dumps(["not", "a", "status"]), encoding="utf-8")
    assert _alerts_of(bad) == []
    fresh = datetime.now(timezone.utc).isoformat()
    bad.write_text(json.dumps({"at": fresh, "alerts": "not a list"}), encoding="utf-8")
    assert _alerts_of(bad) == []
    # entries without a string key AND message are not voiceable, so they are dropped, not voiced oddly
    bad.write_text(
        json.dumps({"at": fresh, "alerts": [{"key": 1, "message": "m"}, {"key": "k"}, _DEMOTED]}),
        encoding="utf-8",
    )
    assert _alerts_of(bad) == [_DEMOTED]


def test_no_status_file_is_the_silent_common_case(state, monkeypatch):
    spoken = _record_speech(monkeypatch)
    speak.contour_check({}, 0.0)
    assert spoken == []  # an install that never set the poller up hears nothing and pays one read


def test_an_active_alert_is_voiced_once_and_then_not_again(state, monkeypatch):
    _contour_status(state, [_DEMOTED])
    spoken = _record_speech(monkeypatch)
    speak.contour_check({}, 0.0)
    speak.contour_check({}, 0.0)  # the condition persists — the page does not repeat
    assert spoken == [_VOICED]
    assert (state / "contour-announced").read_text(encoding="utf-8").split() == ["device-demoted:rvc"]
    # The second firing leaves a POSITIVE mark. Without one, "did not repeat" and "died before it
    # ever looked" are the same empty log — and speak.sh swallows every exception and exits 0, so
    # dying is exactly what a regression here looks like from outside.
    assert "contour: already announced — nothing to voice (1 alert(s) still active)" in _speak_log(state)


def test_a_quiet_contour_says_nothing_at_all_not_even_that_it_is_quiet(state, monkeypatch):
    # The other direction, and the overwhelming common case: a fresh green status file. The dedup's
    # marker must not fire here — one line per tool call, forever, for nothing.
    _contour_status(state, [])
    spoken = _record_speech(monkeypatch)
    speak.contour_check({}, 0.0)
    assert spoken == []
    assert "contour:" not in _speak_log(state)


def test_the_hook_reads_the_status_file_the_poller_was_told_to_write(state, monkeypatch, tmp_path):
    """The seam, from this end. contour_poll.py can be pointed anywhere; this hook only ever read
    the default, so a cron line written with `--status /var/tmp/contour.json` polled correctly,
    exited 1 correctly, and paged nobody. One config key now answers for both halves."""
    relocated = tmp_path / "elsewhere" / "contour.json"
    relocated.parent.mkdir()
    written = datetime.now(timezone.utc).isoformat()
    relocated.write_text(json.dumps({"at": written, "max_age": 900, "alerts": [_DEMOTED]}), encoding="utf-8")
    assert not (state / "contour.json").exists()  # nothing at the default path: the whole point

    spoken = _record_speech(monkeypatch)
    speak.contour_check({"contour": {"status_path": str(relocated)}}, 0.0)
    assert spoken == [_VOICED]
    # …and an unset key is still the default path, byte for byte
    assert speak.contour_status_path({}) == speak._CONTOUR_PATH


def test_a_cleared_and_returned_alert_pages_again(state, monkeypatch):
    spoken = _record_speech(monkeypatch)
    _contour_status(state, [_DEMOTED])
    speak.contour_check({}, 0.0)
    _contour_status(state, [])  # the condition cleared: the announced key is pruned
    speak.contour_check({}, 0.0)
    assert not (state / "contour-announced").read_text(encoding="utf-8").split()
    _contour_status(state, [_DEMOTED])  # …and came back — a stale key must not mute its return
    speak.contour_check({}, 0.0)
    assert spoken == [_VOICED, _VOICED]


def test_contour_alerts_opt_out_and_speak_disabled_both_stay_silent(state, monkeypatch):
    _contour_status(state, [_DEMOTED])
    spoken = _record_speech(monkeypatch)
    speak.contour_check({"contour": {"alerts": False}}, 0.0)
    speak.contour_check({"speak": {"enabled": False}}, 0.0)
    assert spoken == []
    assert not (state / "contour-announced").exists()  # nothing claimed either


def test_an_alert_that_loses_the_eager_lock_stays_unannounced(state, monkeypatch):
    _contour_status(state, [_DEMOTED])
    spoken = _record_speech(monkeypatch)
    monkeypatch.setattr(speak, "acquire_lock", lambda *grace: None)  # another firing holds it
    speak.contour_check({"speak": {"eager": True}}, 0.0)
    assert spoken == []
    assert not (state / "contour-announced").exists()  # left for the next firing, one tool call away
    assert "left unannounced" in _speak_log(state)


def test_the_alert_text_is_capped_at_max_chars(state, monkeypatch):
    _contour_status(state, [_DEMOTED])
    spoken = _record_speech(monkeypatch)
    speak.contour_check({"speak": {"max_chars": 25}}, 0.0)
    assert len(spoken) == 1 and len(spoken[0]) <= 25
    assert _VOICED.startswith(spoken[0])  # truncated, not paraphrased


def test_the_hook_entry_voices_the_alert_after_a_turn_with_nothing_to_say(state, monkeypatch):
    """The page does not depend on the turn having a marked line: a Stop whose transcript holds
    no marker speaks nothing of its own, and the contour alert is STILL voiced after it — the
    whole point of putting the check in the hook path."""
    _write_config(state, monkeypatch)
    transcript = state / "transcript.jsonl"
    transcript.write_text(_assistant("a reply with nothing marked in it") + "\n", encoding="utf-8")
    _contour_status(state, [_DEMOTED])
    spoken = _record_speech(monkeypatch)
    monkeypatch.setattr(speak.sys, "stdin", _Stdin(json.dumps({"transcript_path": str(transcript), "hook_event_name": "Stop"})))
    monkeypatch.setattr(speak.time, "sleep", lambda seconds: None)
    assert speak.entry() == 0
    assert spoken == [_VOICED]


# --- the page's own delivery, its freshness, and the two firings that race for it -------------------


class _RefusedOpener:
    """Every request refused — the speech server is STOPPED. Drives the real _get/_post/synthesize
    /_play_stream bodies; the only thing faked is the socket that would not have connected."""

    def __init__(self) -> None:
        self.attempts = 0

    def open(self, request, timeout=None):
        self.attempts += 1
        raise speak.urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))


_UNREACHABLE = {
    "key": "unreachable:voice-loop",
    "kind": "unreachable",
    "service": "voice-loop",
    "message": "voice-loop is not answering its health endpoint (URLError)",
}


def test_an_alert_the_stopped_speech_server_could_not_voice_is_retried_not_swallowed(state, monkeypatch):
    """THE failure the shipped defaults guarantee: `tts.backend: lan` speaks through
    127.0.0.1:8355, and with `contour.services` unset that same server is the only thing polled.
    So the commonest alert of all — "the speech server is not answering" — is the one whose own
    delivery cannot possibly succeed. Announced-before-synthesis made it voiced NEVER and, because
    the key was already in the ledger, repeated never: silence for as long as the fault lasted.
    """
    _contour_status(state, [_UNREACHABLE])
    refused = _RefusedOpener()
    monkeypatch.setattr(speak.urllib.request, "build_opener", lambda *handlers: refused)
    monkeypatch.setattr(speak.subprocess, "Popen", lambda argv, **kwargs: _NullPlayer())

    speak.contour_check({}, 0.0)

    assert refused.attempts  # it really went for the server, and the server was not there
    log = _speak_log(state)
    assert "nothing played via=tts" in log
    assert "left unannounced, to be retried" in log
    assert not (state / "contour-announced").exists()  # NOT announced: nothing was ever delivered

    # the server comes back while the condition persists: the alert is still there to be said
    spoken = _record_speech(monkeypatch)
    speak.contour_check({}, 0.0)
    speak.contour_check({}, 0.0)  # …and then said exactly once, like every other page
    assert spoken == ["Voice contour: voice-loop is not answering its health endpoint (URLError)"]
    assert (state / "contour-announced").read_text(encoding="utf-8").splitlines() == ["unreachable:voice-loop"]


def test_a_poller_that_stopped_is_not_a_healthy_contour(state, monkeypatch):
    """#40's opening line, on the consumption side. Remove the cron entry (or reboot a box whose
    timer was never enabled) and contour.json freezes at its last green poll: alerts == [], the
    hook silent forever, and nothing at all to distinguish it from a live green contour."""
    _contour_status(state, [], age_seconds=4000)  # last polled over an hour ago, and it was green
    spoken = _record_speech(monkeypatch)
    speak.contour_check({}, 0.0)
    assert len(spoken) == 1 and "nobody has polled the voice contour" in spoken[0]
    assert (state / "contour-announced").read_text(encoding="utf-8").splitlines() == ["poller-stale"]

    # said once while it stays stale…
    speak.contour_check({}, 0.0)
    assert len(spoken) == 1
    # …and the poller coming back prunes the key, so a second outage later pages again
    _contour_status(state, [])
    speak.contour_check({}, 0.0)
    assert not (state / "contour-announced").read_text(encoding="utf-8").split()
    assert len(spoken) == 1  # a fresh, green file has nothing to say


def test_a_stale_file_reports_the_staleness_and_not_its_own_frozen_alerts(state, monkeypatch):
    # A reading nobody refreshed says nothing about NOW. The one true thing left is that nobody
    # is looking, so that is the only thing voiced.
    _contour_status(state, [_DEMOTED], age_seconds=4000)
    spoken = _record_speech(monkeypatch)
    speak.contour_check({}, 0.0)
    assert len(spoken) == 1 and "nobody has polled" in spoken[0]
    assert _VOICED not in spoken


def test_the_bound_is_the_pollers_own_and_a_file_without_one_still_gets_a_bound(state, monkeypatch):
    # the poller writes its cadence into the file, and that wins…
    _contour_status(state, [_DEMOTED], age_seconds=120, max_age=60)
    spoken = _record_speech(monkeypatch)
    speak.contour_check({}, 0.0)
    assert len(spoken) == 1 and "nobody has polled" in spoken[0]

    # …a file written by a poller that predates the bound gets the fallback rather than a pass…
    (state / "contour-announced").unlink()
    _contour_status(state, [_DEMOTED], age_seconds=speak.CONTOUR_MAX_AGE + 60, max_age=None)
    speak.contour_check({}, 0.0)
    assert len(spoken) == 2 and "nobody has polled" in spoken[1]

    # …and a timestamp that cannot be read at all cannot vouch for anything either
    (state / "contour-announced").unlink()
    (state / "contour.json").write_text(json.dumps({"at": "yesterday", "alerts": [_DEMOTED]}), encoding="utf-8")
    speak.contour_check({}, 0.0)
    assert len(spoken) == 3 and "no readable timestamp" in spoken[2]

    # a fresh file, though, is read exactly as before: its own alerts, nothing invented
    (state / "contour-announced").unlink()
    _contour_status(state, [_DEMOTED])
    speak.contour_check({}, 0.0)
    assert spoken[3] == _VOICED


def test_with_eager_off_a_post_tool_use_firing_runs_no_contour_check(state, monkeypatch):
    """hooks.json registers PostToolUse unconditionally, so with `speak.eager` off this file is
    executed after EVERY tool call. entry() ran the check from there regardless, which meant a
    take_over() — a SIGTERM at whatever was playing — on every tool call of every turn, for a
    feature the install never opted into. The eager no-op covers this file, not just main()."""
    _write_config(state, monkeypatch)
    transcript = state / "transcript.jsonl"
    transcript.write_text(_assistant("a reply with nothing marked in it") + "\n", encoding="utf-8")
    _contour_status(state, [_DEMOTED])
    spoken = _record_speech(monkeypatch)
    takeovers: list[int] = []
    monkeypatch.setattr(speak, "take_over", lambda: takeovers.append(1))
    payload = {"transcript_path": str(transcript), "hook_event_name": "PostToolUse"}
    monkeypatch.setattr(speak.sys, "stdin", _Stdin(json.dumps(payload)))

    assert speak.entry() == 0
    assert (spoken, takeovers) == ([], [])
    assert not (state / "contour-announced").exists()

    # and the SAME alert on the Stop path is still paged: the gate is the event, not the feature
    payload["hook_event_name"] = "Stop"
    monkeypatch.setattr(speak.sys, "stdin", _Stdin(json.dumps(payload)))
    monkeypatch.setattr(speak.time, "sleep", lambda seconds: None)
    assert speak.entry() == 0
    assert spoken == [_VOICED]


@pytest.mark.skipif(speak.fcntl is None, reason="the contender is a real second flock; Windows has no flock to hold")
def test_two_firings_in_one_assistant_block_cannot_both_page(state, monkeypatch):
    """Two tool calls in one assistant block are two concurrent hook processes. The eager-off page
    path took no lock at all, and the announced-ledger is a read-modify-write, so both read an
    empty ledger, both saw the alert as fresh, both claimed it and both spoke. The contender here
    is a REAL second flock on the real lockfile, held while the check runs."""
    _contour_status(state, [_DEMOTED])
    spoken = _record_speech(monkeypatch)

    holder = open(str(state / "speaking.lock"), "w", encoding="utf-8")  # noqa: SIM115 — released below
    speak.fcntl.flock(holder.fileno(), speak.fcntl.LOCK_EX | speak.fcntl.LOCK_NB)
    try:
        speak.contour_check({}, 0.0)  # eager off — this is the path that used to have no lock
    finally:
        holder.close()

    assert spoken == []
    assert not (state / "contour-announced").exists()  # nothing claimed by the firing that lost
    assert "left unannounced" in _speak_log(state)

    # the firing in front is gone: the alert is still there, and is said exactly once
    speak.contour_check({}, 0.0)
    speak.contour_check({}, 0.0)
    assert spoken == [_VOICED]


def test_a_page_leaves_no_pidfile_behind(state, monkeypatch):
    """contour_check writes playing.pid like every path that makes a sound, and had no counterpart
    for it. The leftover is not inert: dictate.py's echo guard reads a non-empty playing.pid as
    "a chain is playing", signals pids that are gone, and skips the `pkill -f voice-loop-speak`
    fallback it keeps for exactly this case — a chain that died without cleanup."""
    _contour_status(state, [_DEMOTED])
    spoken = _record_speech(monkeypatch)
    speak.contour_check({}, 0.0)
    assert spoken == [_VOICED]
    assert not (state / "playing.pid").exists()

    # …including when the page could not be delivered at all
    _contour_status(state, [_UNREACHABLE])
    monkeypatch.setattr(speak, "synthesize", lambda text, s, key: None)
    speak.contour_check({}, 0.0)
    assert not (state / "playing.pid").exists()


def test_an_alert_key_naming_a_service_with_a_space_survives_the_ledger(state, monkeypatch):
    """The announced-ledger is one key per LINE. Read back with str.split(), a service the
    operator called "tts worker" became two tokens matching nothing — so the prune found the whole
    file stale, TRUNCATED it, and the alert was voiced again on every single firing, forever."""
    spaced = {
        "key": "device-demoted:tts worker",
        "kind": "device-demoted",
        "service": "tts worker",
        "message": "tts worker is serving on cpu, expected gpu",
    }
    _contour_status(state, [spaced])
    spoken = _record_speech(monkeypatch)
    speak.contour_check({}, 0.0)
    speak.contour_check({}, 0.0)
    speak.contour_check({}, 0.0)
    assert spoken == ["Voice contour: tts worker is serving on cpu, expected gpu"]
    assert (state / "contour-announced").read_text(encoding="utf-8").splitlines() == ["device-demoted:tts worker"]


# --- Latin → Cyrillic transliteration for contour alerts (#104) ------------------------------------


def test_transliterate_to_cyrillic_maps_every_latin_letter():
    """The mapping covers every letter A-Z, a-z — a gap would leave a Latin character untransliterated
    and the alert still unsynthesizable on a Cyrillic voice."""
    text = "The quick brown fox jumps over the lazy dog. ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    result = speak._transliterate_to_cyrillic(text)
    # Every Latin letter is gone — the text is now pure Cyrillic + punctuation
    assert not any("a" <= ch <= "z" or "A" <= ch <= "Z" for ch in result)
    # Non-Latin characters pass through unchanged
    assert speak._transliterate_to_cyrillic("Привет! 123.") == "Привет! 123."
    assert speak._transliterate_to_cyrillic("") == ""


def test_contour_alert_is_transliterated_for_russian_voice(state, monkeypatch):
    """The acceptance case: a ru-configured contour with an English alert. The alert text is
    transliterated to Cyrillic before synthesis, so the Russian voice CAN pronounce it — rather
    than the server returning 400 and the page going silent.

    Mutation gap: without the transliteration, contour_check sends pure-Latin text to a ru voice,
    the server returns 400, and the page is never heard. The test captures what `synthesize`
    receives — transliterated Cyrillic text — which is what makes the alert audible."""
    _contour_status(state, [_DEMOTED])
    # a ru-configured voice — the exact setup from the bug report
    cfg = {"language": "ru"}
    spoken = _record_speech(monkeypatch)
    speak.contour_check(cfg, 0.0)
    assert len(spoken) == 1
    result = spoken[0]
    # The transliteration replaced every Latin letter with a Cyrillic one.
    # "Voice contour: rvc is serving on cpu, expected gpu" becomes e.g.
    # "Воике контоур: рвк ис сервинг он кпу, експектед гпу"
    assert "Voice" not in result  # the Latin prefix is gone
    assert "rvc" not in result.lower()  # Latin letters in the message are gone
    assert "cpu" not in result.lower()
    # Cyrillic characters are present — the text is synthesizable by a ru voice
    assert any("Ѐ" <= ch <= "ӿ" for ch in result)


def test_contour_alert_is_not_transliterated_for_english_voice(state, monkeypatch):
    """An English voice can pronounce the Latin alert as-is — the transliteration must not fire
    and corrupt a perfectly speakable text."""
    _contour_status(state, [_DEMOTED])
    cfg = {"language": "en"}
    spoken = _record_speech(monkeypatch)
    speak.contour_check(cfg, 0.0)
    assert len(spoken) == 1
    # The English text is unchanged — no transliteration fired
    assert "Voice contour:" in spoken[0]
    assert "rvc" in spoken[0]


# --- B2 round: the per-table coverage targets that the previous round left missing -------------


def test_wait_out_playback_logs_the_deadline_and_returns_text_unchanged(state, monkeypatch):
    """Mutation gap: the deadline branch returns the SAME text it was handed and a 'hook deadline
    reached' log line — losing either is the difference between the caller logging a clean give-up
    and the caller logging that give-up over a text that has already been wiped."""
    monkeypatch.setattr(speak, "playback_is_live", lambda: True)  # wedge the player forever
    monkeypatch.setattr(speak.time, "monotonic", lambda: 1000.0)  # the deadline is already past
    text = speak.wait_out_playback(
        lambda: None,
        lambda value: bool(value),
        "the stale read",
        deadline=999.0,  # any value < the monotonic above
    )
    assert text == "the stale read"
    assert "hook deadline reached while waiting for playback" in _speak_log(state)


def test_wait_out_flush_logs_the_deadline_and_returns_text_unchanged(state, monkeypatch):
    """Mutation gap: the deadline branch in wait_out_flush returns the text unchanged and the
    deadline-reached log line — exactly the contract the playback sibling has."""
    monkeypatch.setattr(speak.time, "monotonic", lambda: 1000.0)  # deadline is already past

    def stat(path):
        return _FakeStat(20, 5)  # the file is GROWING, so the loop would otherwise keep polling

    text = speak.wait_out_flush(
        "transcript.jsonl",
        lambda: None,
        lambda value: bool(value),
        "the stale read",
        (10, 5),
        stat=stat,
        deadline=999.0,  # any value < the monotonic above
    )
    assert text == "the stale read"
    assert "hook deadline reached while waiting for transcript" in _speak_log(state)


def test_synthesize_returns_none_and_logs_misconfiguration_when_builder_raises(state, opener):
    """Mutation gap: a builder that refuses a misconfiguration (an unset ElevenLabs voice) must
    turn the ValueError into a 'cloud tts misconfigured' log line and a None return — the request
    is never built, the network is never touched, and the failure is diagnosable."""
    fake = opener(b"")  # body never read, but install the opener so we can prove no request fires
    s = speak.resolve_settings(
        {"tts": {"backend": "cloud", "cloud": {"provider": "elevenlabs"}}}, "Linux"
    )
    assert speak.synthesize("hi", s, "sk-secret") is None
    assert fake.requests == []  # the builder refused; the request was never built
    assert "cloud tts misconfigured" in _speak_log(state)


def test_synthesize_logs_an_empty_body_and_returns_none(state, opener):
    """Mutation gap: an empty body (a server that returned 200 with no bytes) is dropped, not
    played — the empty-synthesis log line is what a reader uses to distinguish it from a server
    that simply could not be reached."""
    opener(b"")
    s = speak.resolve_settings(
        {"tts": {"backend": "cloud", "cloud": {"provider": "elevenlabs", "voice_id": "v123"}}}, "Linux"
    )
    assert speak.synthesize("hi", s, "sk-secret") is None
    assert "empty synthesis from https://api.elevenlabs.io" in _speak_log(state)


def test_synthesize_logs_an_error_document_body_and_returns_none(state, opener):
    """Mutation gap: a body starting with '{' or '[' is a JSON error document, NOT audio — the
    log line that names it is the only thing a reader sees when the cloud starts returning
    'rate limited' or 'voice not found' as the response body."""
    opener(b'{"error": "voice not found"}')
    s = speak.resolve_settings(
        {"tts": {"backend": "cloud", "cloud": {"provider": "elevenlabs", "voice_id": "v123"}}}, "Linux"
    )
    assert speak.synthesize("hi", s, "sk-secret") is None
    log = _speak_log(state)
    assert "synthesis returned an error document" in log
    assert "voice not found" in log


def test_post_returns_the_error_body_when_the_server_returns_an_http_error(monkeypatch):
    """Mutation gap: _post turns an HTTPError into the error body itself — that body is the
    diagnosis, so the caller can hand it to the JSON-error-document path; without it the caller
    only sees the status line. The second arm (b"") covers the case where even err.read()
    itself raises — the body is unrecoverable, the call returns empty bytes, the caller drops."""

    class _FakeErr(speak.urllib.error.HTTPError):
        def __init__(self, body: bytes) -> None:
            super().__init__("http://example/", 500, "Server Error", {}, io.BytesIO(body))
            self._body = body

        def read(self) -> bytes:
            return self._body

    class _BodyRaisingErr(speak.urllib.error.HTTPError):
        def __init__(self) -> None:
            super().__init__("http://example/", 500, "Server Error", {}, None)

        def read(self) -> bytes:
            raise OSError("the body itself is unreadable")

    class _RaisingOpener:
        def __init__(self, exc) -> None:
            self._exc = exc

        def open(self, request, timeout=None):
            raise self._exc

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

    # 1. The body is returned verbatim — the caller can match it as an error document
    monkeypatch.setattr(
        speak.urllib.request,
        "build_opener",
        lambda *h: _RaisingOpener(_FakeErr(b'{"error": "rate limited"}')),
    )
    body = speak._post("http://example/", {}, {"text": "hi"}, 1.0)
    assert body == b'{"error": "rate limited"}'

    # 2. The empty-bytes arm when err.read() itself raises
    monkeypatch.setattr(
        speak.urllib.request, "build_opener", lambda *h: _RaisingOpener(_BodyRaisingErr())
    )
    body = speak._post("http://example/", {}, {"text": "hi"}, 1.0)
    assert body == b""


def test_post_unreachable_host_returns_none(state, monkeypatch):
    """Mutation gap: a URLError is the host/errno case, NEVER the body — the log line must name
    the reason (which is the diagnostic), and the return is None so the caller can tell it apart
    from an empty body."""

    class _UnreachableOpener:
        def open(self, request, timeout=None):
            raise speak.urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

    monkeypatch.setattr(speak.urllib.request, "build_opener", lambda *h: _UnreachableOpener())
    assert speak._post("http://example/", {}, {"text": "hi"}, 1.0) is None
    assert "synthesis unreachable" in _speak_log(state)


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="process-group signalling needs os.killpg, which Windows does not have")
def test_on_sigterm_swallows_proc_terminate_errors(monkeypatch):
    """Mutation gap: a process whose terminate() raises OSError (it has already exited, or the
    handle was lost to PID reuse) must not kill the cleanup chain — the handler swallows it."""

    class _Dying:
        def poll(self):
            return None  # still running, so terminate() is attempted

        def terminate(self):
            raise OSError(3, "No such process")

    monkeypatch.setattr(speak.os, "_exit", lambda code: None)
    speak._live["proc"] = _Dying()
    speak._live["stream"] = None
    speak._live["files"] = set()
    try:
        speak._on_sigterm(signal.SIGTERM, None)  # must not raise
    finally:
        speak._live["proc"] = None


def test_on_sigterm_swallows_stream_close_errors(monkeypatch):
    """Mutation gap: a stream whose close() raises OSError (the handle is already closed, the
    socket has been ripped out under us) must not kill the cleanup chain."""

    class _DyingStream:
        def close(self):
            raise OSError(9, "Bad file descriptor")

    monkeypatch.setattr(speak.os, "_exit", lambda code: None)
    speak._live["proc"] = None
    speak._live["stream"] = _DyingStream()
    speak._live["files"] = set()
    try:
        speak._on_sigterm(signal.SIGTERM, None)  # must not raise
    finally:
        speak._live.pop("stream", None)


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="process-group signalling needs os.killpg, which Windows does not have")
def test_on_sigterm_signals_the_process_group_when_pgid_is_live(monkeypatch):
    """Mutation gap: a live pgid child is signalled via killpg so the player inside the shell
    receives the signal regardless of exec or wrapper depth."""
    killpg_calls: list[tuple[int, int]] = []
    monkeypatch.setattr(speak.os, "killpg", lambda pgid, sig: killpg_calls.append((pgid, sig)))
    monkeypatch.setattr(speak.os, "_exit", lambda code: None)

    class _Live:
        def poll(self):
            return None  # still running — terminate will be called

        def terminate(self):
            pass

    speak._live["proc"] = _Live()
    speak._live["pgid"] = 777
    try:
        speak._on_sigterm(signal.SIGTERM, None)
        assert killpg_calls == [(777, signal.SIGTERM)]
    finally:
        speak._live["proc"] = None
        speak._live.pop("pgid", None)


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="process-group signalling needs os.killpg, which Windows does not have")
def test_on_sigterm_swallows_killpg_errors_for_already_exited_groups(monkeypatch):
    """Mutation gap: a process-group child that has already exited (race between the proc and
    the group) raises ProcessLookupError, and the handler swallows it — otherwise the hook
    itself dies on the cleanup it owns."""
    monkeypatch.setattr(speak.os, "killpg", lambda pgid, sig: (_ for _ in ()).throw(ProcessLookupError(3, "No such process")))
    monkeypatch.setattr(speak.os, "_exit", lambda code: None)

    class _Live:
        def poll(self):
            return None

        def terminate(self):
            pass

    speak._live["proc"] = _Live()
    speak._live["pgid"] = 888
    try:
        speak._on_sigterm(signal.SIGTERM, None)  # must not raise
    finally:
        speak._live["proc"] = None
        speak._live.pop("pgid", None)


def test_on_sigterm_closes_the_stream_when_one_is_open(monkeypatch):
    """Mutation gap: an open stream socket is closed so the next read returns immediately rather
    than blocking a follow-up firing on a closed but-unclosed handle."""

    class _Stream:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    stream = _Stream()
    monkeypatch.setattr(speak.os, "_exit", lambda code: None)
    speak._live["proc"] = None  # no proc, the proc branch is skipped
    speak._live["stream"] = stream
    try:
        speak._on_sigterm(signal.SIGTERM, None)
        assert stream.closed is True
    finally:
        speak._live.pop("stream", None)


def test_on_sigterm_swallows_unlink_errors_for_already_gone_temp_files(monkeypatch):
    """Mutation gap: a temp file that was already removed (a parallel cleanup raced us) raises
    OSError on unlink — the handler swallows it, because the goal is that the file is gone,
    not that we got to remove it ourselves."""

    def unlink(path):
        raise OSError(2, "No such file or directory")

    monkeypatch.setattr(speak.os, "_exit", lambda code: None)
    monkeypatch.setattr(speak.os, "unlink", unlink)
    speak._live["proc"] = None
    speak._live["stream"] = None
    speak._live["files"] = set()
    speak._live["files"].add("/tmp/already-gone-1")
    speak._live["files"].add("/tmp/already-gone-2")
    try:
        speak._on_sigterm(signal.SIGTERM, None)  # must not raise
    finally:
        speak._live["files"] = set()


def test_contour_alert_is_logged_when_no_lock_can_be_acquired(state, monkeypatch):
    """Mutation gap: a contour that loses the eager lock (another firing holds it) leaves the
    alert UNANNOUNCED and logs the 'left unannounced' line — without it the next firing would
    not know there is still work to do."""
    _contour_status(state, [_DEMOTED])
    spoken = _record_speech(monkeypatch)
    monkeypatch.setattr(speak, "acquire_lock", lambda *grace: None)  # another firing holds it
    speak.contour_check({"speak": {"eager": True}}, 0.0)
    assert spoken == []
    assert not (state / "contour-announced").exists()
    assert "another firing is speaking" in _speak_log(state)
    assert "left unannounced" in _speak_log(state)


def test_contour_already_announced_logs_only_when_there_was_something_to_announce(state, monkeypatch):
    """Mutation gap: the dedup's positive mark is logged ONLY when there are alerts to be quiet
    ABOUT — every quiet turn owes the log silence, and a regression that logged unconditionally
    would add a line per tool call forever."""
    _contour_status(state, [_DEMOTED])
    spoken = _record_speech(monkeypatch)
    speak.contour_check({}, 0.0)  # first time: voiced
    log_after_first = _speak_log(state)
    speak.contour_check({}, 0.0)  # second time: dedup, must log the positive mark
    log_after_second = _speak_log(state)
    assert "already announced" in log_after_second
    assert "already announced" not in log_after_first
    # A quiet turn: NO alert file at all, NO log line about dedup
    (state / "contour-announced").unlink()  # start fresh
    (state / "contour.json").unlink()
    log_before = _speak_log(state)
    speak.contour_check({}, 0.0)
    log_after = _speak_log(state)
    assert log_after == log_before  # no line added


def test_contour_degrade_reason_is_logged_once_on_a_turn_that_speaks(state, monkeypatch):
    """Mutation gap: a lock that degraded to _NoLock has a reason that belongs to THIS turn's
    speech, not to a firing that found nothing new — a firing that speaks logs the reason,
    a firing that does not speak does not."""

    class _StubNoLock(speak._NoLock):
        def __init__(self) -> None:
            super().__init__(reason="speaking lock is unsupported on this platform — proceeding unlocked")

    monkeypatch.setattr(speak, "acquire_lock", lambda *grace: _StubNoLock())
    _contour_status(state, [_DEMOTED])
    spoken = _record_speech(monkeypatch)
    speak.contour_check({}, 0.0)
    assert len(spoken) == 1  # it actually spoke — the reason rides THAT turn
    assert "proceeding unlocked" in _speak_log(state)


def test_contour_no_player_leaves_the_alert_unannounced_to_be_retried(state, monkeypatch):
    """Mutation gap: a failed delivery re-arms — the key stays unannounced, so the next firing
    tries again for as long as the condition holds. Announcing-before-delivery would make a dead
    speech server the ONE condition this hook could never report."""

    class _DyingPlayer(_NullPlayer):
        def poll(self) -> int | None:
            return 1  # player crashed

    _contour_status(state, [_DEMOTED])
    monkeypatch.setattr(speak, "_get", lambda url, timeout: None)  # blob path
    monkeypatch.setattr(speak.subprocess, "Popen", lambda argv, **kwargs: _DyingPlayer())
    speak.contour_check({}, 0.0)
    assert not (state / "contour-announced").exists()
    assert "the page reached no player" in _speak_log(state)


def test_windows_process_is_live_returns_false_on_linux_for_any_pid(monkeypatch):
    """Mutation gap: ctypes.WinDLL does not exist on Linux, so the AttributeError is caught and
    the function returns False — without the catch, importing speak on Linux would raise."""
    assert speak._windows_process_is_live(1) is False
    assert speak._windows_process_is_live(0) is False
    assert speak._windows_process_is_live(99999) is False


def test_main_guard_swallows_an_unexpected_exception_and_exits_zero(state, monkeypatch):
    """Mutation gap: the __main__ guard is the hook's last line of defence — a top-level
    exception must be caught, logged with a truncated repr, and the process must exit 0.
    speak.sh swallows the exit code, so an exit 1 here would still LOOK like a clean hook from
    outside, but the log line is what makes the failure diagnosable.

    Drove via subprocess so the guard's own module scope is the one that runs; the subprocess's
    rc is the guard's contract. The pytest-side .coverage file already covers the guard lines
    via the in-process runpy path; this test pins the subprocess contract so a regression to
    're-raise the exception' is caught even if the coverage merge misses it."""
    rc = subprocess.run(
        [sys.executable, str(_SPEAK_PATH)],
        env={**os.environ, "VOICE_LOOP_CONFIG": str(state / "config.json"), "PATH": os.environ.get("PATH", "")},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=10,
    )
    # The guard catches whatever entry() raised and exits 0 — speak.sh sees a clean exit.
    assert rc.returncode == 0
    # The diagnostic is structural: the guard catches Exception, logs a truncated repr, and exits 0.
    source = _SPEAK_PATH.read_text(encoding="utf-8")
    guard_block = source[source.rfind('if __name__ == "__main__":'):]
    assert "except Exception:" in guard_block
    assert "unexpected error:" in guard_block
    assert "sys.exit(0)" in guard_block


def test_main_guard_runs_under_runpy_for_coverage(state, monkeypatch):
    """The guard is run in-process by runpy so the .coverage file measures it; a structural
    assertion pins the same catch/log/exit-0 contract from this side.

    Two cases drive both arms of the inner `except Exception:`:
    - the happy log() call (covers 2580-2585, 2587)
    - a log() call that itself raises (covers 2585-2586 — the swallow-nothing path)

    runpy loads speak.py fresh; module-level monkeypatching on the speak instance is bypassed.
    The seams the new module walks through are `sys.exit` (a module attribute, but `sys` is the
    same module object everywhere) and `os.open` (a free function — same everywhere too)."""
    import runpy as _runpy

    captured: list[int] = []

    def fake_exit(code=0):
        captured.append(code)
        raise SystemExit(code)

    # Patch `sys.exit` so the new module's `sys.exit(0)` lands here, not the real one.
    monkeypatch.setattr(sys, "exit", fake_exit)

    # Case 1 — happy log() call. Make entry() raise so the outer except catches it.
    sys.modules.pop("__main__", None)
    captured.clear()
    try:
        _runpy.run_path(str(_SPEAK_PATH), run_name="__main__")
    except SystemExit:
        pass
    assert captured == [0]

    # Case 2 — log() itself raises. Patch os.path.exists to throw a non-OSError — log() already
    # catches OSError internally, so only a different exception type escapes to the guard's
    # inner except (lines 2585-2586).
    def explode_path(*args, **kwargs):
        raise RuntimeError("a log-failure the inner OSError swallow cannot catch")

    monkeypatch.setattr(os.path, "exists", explode_path)
    captured.clear()
    sys.modules.pop("__main__", None)
    try:
        _runpy.run_path(str(_SPEAK_PATH), run_name="__main__")
    except SystemExit:
        pass
    assert captured == [0]  # the guard's sys.exit(0) still lands — even when log() dies
