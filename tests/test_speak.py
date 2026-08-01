"""The Stop hook's pure functions: chunking, extraction, config precedence, key reading.

speak.py is glue around subprocess players and an HTTP synthesis call, so — like the shell scripts —
its runtime contract is proven by the REAL Stop-hook invocation in CI, not by mocks (see
TESTING.md). What IS unit-tested here is the part with no I/O in it at all: the sentence chunker
that drives streaming, the transcript extractor, and the config-precedence table that every
backend reads. Stdlib + pytest only; nothing here touches the network, a player, or the state dir.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_SPEAK_PATH = Path(__file__).resolve().parents[1] / "plugins" / "voice-loop" / "scripts" / "speak.py"
_spec = importlib.util.spec_from_file_location("speak", _SPEAK_PATH)
speak = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(speak)


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
    assert speak.extract_from_lines(lines, "🔊", 600) == ""


def test_extract_joins_multiple_marked_lines_and_clips():
    lines = [_assistant("🔊 first part\ndetail\n🔊 second part")]
    assert speak.extract_from_lines(lines, "🔊", 600) == "first part second part"
    assert speak.extract_from_lines(lines, "🔊", 10) == "first part"


def test_extract_honours_a_custom_marker_and_leading_space():
    lines = [_assistant("  >> spoken with a custom marker")]
    assert speak.extract_from_lines(lines, ">>", 600) == "spoken with a custom marker"


def test_extract_empty_transcript():
    assert speak.extract_from_lines([], "🔊", 600) == ""


# --- resolve_settings: the config-precedence table ----------------------------------------------


def test_defaults_with_empty_config_linux():
    s = speak.resolve_settings({}, "Linux")
    assert s["enabled"] is True
    assert s["marker"] == "🔊"
    assert s["player"] == "aplay -q"
    assert s["max_chars"] == 600
    assert s["timeout"] == 60.0
    assert s["backend"] == "lan"
    assert s["language"] == "ru"
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
    assert speak.resolve_settings({"language": "en"}, "Linux")["language"] == "en"
    both = {"language": "en", "tts": {"language": "de"}}
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
