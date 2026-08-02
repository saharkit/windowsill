"""The Stop hook's pure functions: chunking, extraction, config precedence, key reading — plus the
audit-hardening seams: the identity-checked takeover (PID-reuse guard, with a real-child
integration case), the urllib-level cloud synthesis request shapes, and the bare-marker fast path
that must never burn the flush-race backoff.

speak.py is glue around subprocess players and an HTTP synthesis call, so its full runtime
contract is proven by the REAL Stop-hook invocation in CI (see TESTING.md). What is tested here
never reaches the network, a player, or the live state dir: state paths are monkeypatched into
tmp_path, HTTP openers are faked at the urllib seam, and the only real subprocesses are
short-lived pythons owned by the tests themselves.
"""

from __future__ import annotations

import base64
import importlib.util
import io
import json
import os
import signal
import subprocess
import sys
import time
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
    monkeypatch.setattr(speak, "_PID_PATH", str(tmp_path / "playing.pid"))
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
    assert s["max_chars"] == 600
    assert s["timeout"] == 60.0
    assert s["backend"] == "lan"
    assert s["language"] == "en"  # explicit-language setups always write the key; the default is English
    assert s["key_env"] == "VOICE_LOOP_TTS_API_KEY"


def test_default_player_on_macos():
    assert speak.resolve_settings({}, "Darwin")["player"] == "afplay"


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

    def slow_extract(path, marker, limit):
        clock.advance(EXTRACT_SECONDS)  # the transcript read, incl. any flush-race retries
        return real_extract(path, marker, limit)

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


def test_pid_identity_check_is_skipped_off_linux():
    def never(pid):
        raise AssertionError("cmdline must not be read off Linux")

    assert speak.pid_looks_like_speak(9, read_cmdline=never, platform_id="darwin") is True


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
