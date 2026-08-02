"""The text pipeline: stress marking, the protect-marked-segments split, chunking.

This is the part that decides whether a synthetic voice sounds like a person or like a drunk robot,
and the part with no hardware in it at all — so it gets the closest tests.
"""

from __future__ import annotations

import json
import time

import pytest

import voice_server

ACUTE = "́"


def bracket(segment: str) -> str:
    """A fake accentuator that shows exactly which runs were handed to the engine."""
    return f"[{segment}]"


@pytest.fixture
def bracketing(monkeypatch, accent_enabled):
    """Accentuation on, with `bracket` as the engine; the returned list records every call."""
    calls: list[str] = []

    def engine(segment: str) -> str:
        calls.append(segment)
        return bracket(segment)

    monkeypatch.setitem(voice_server.ACCENTUATORS, "ru", lambda: engine)
    return calls


def test_acute_to_plus_moves_the_mark_before_the_vowel():
    assert voice_server.acute_to_plus(f"Саха{ACUTE}р") == "Сах+ар"
    assert voice_server.acute_to_plus(f"робо{ACUTE}та") == "роб+ота"


def test_acute_to_plus_leaves_unmarked_text_alone():
    assert voice_server.acute_to_plus("обычный текст") == "обычный текст"


def test_strip_stress_markers_removes_plus_before_a_vowel_in_both_alphabets():
    assert voice_server.strip_stress_markers("Сах+ар и раб+ота") == "Сахар и работа"
    assert voice_server.strip_stress_markers("t+Esto and s+odium") == "tEsto and sodium"


def test_strip_stress_markers_removes_a_combining_acute_after_a_letter():
    assert voice_server.strip_stress_markers(f"робо{ACUTE}та") == "робота"


def test_strip_stress_markers_is_vowel_anchored():
    """'+' not in stress position survives: C++, arithmetic, a stray trailing mark."""
    assert voice_server.strip_stress_markers("C++ и 2+2=4 и х+") == "C++ и 2+2=4 и х+"


def test_language_without_an_accentuator_passes_through_untouched():
    text = f"Plain english with a stray acute{ACUTE} mark"
    assert voice_server.mark_stress(text, "en") == text


def test_user_rules_are_applied(stress_file, monkeypatch):
    stress_file.write_text(json.dumps({r"\bАкме\b": "+Акме"}), encoding="utf-8")
    monkeypatch.setattr(voice_server, "USE_ACCENT", False)
    assert voice_server.mark_stress("привет Акме", "ru") == "привет +Акме"


def test_marked_tokens_are_hidden_from_the_accentuator(monkeypatch, accent_enabled):
    """The regression this split exists for: an accentuator re-stresses what we already marked."""
    monkeypatch.setitem(voice_server.ACCENTUATORS, "ru", lambda: str.upper)

    result = voice_server.mark_stress("Сах+ар и обычные слова", "ru")

    assert "Сах+ар" in result  # the marked token reached the model untouched
    assert " И ОБЫЧНЫЕ СЛОВА" in result  # the free segments went through the accentuator


def test_a_marked_token_at_the_start_protects_only_itself(bracketing):
    assert voice_server.mark_stress("Сах+ар и обычные слова", "ru") == "Сах+ар[ и обычные слова]"
    assert bracketing == [" и обычные слова"]


def test_a_marked_token_in_the_middle_keeps_both_sides_whole(bracketing):
    assert voice_server.mark_stress("мама Сах+ар мыла раму", "ru") == "[мама ]Сах+ар[ мыла раму]"
    assert bracketing == ["мама ", " мыла раму"]


def test_a_marked_token_at_the_end_protects_only_itself(bracketing):
    assert voice_server.mark_stress("обычные слова Сах+ар", "ru") == "[обычные слова ]Сах+ар"
    assert bracketing == ["обычные слова "]


def test_consecutive_marked_tokens_leave_the_whitespace_between_them_untouched(bracketing):
    """The run between two marked tokens is whitespace only — nothing to accentuate."""
    assert voice_server.mark_stress("Сах+ар м+ыло с+ода", "ru") == "Сах+ар м+ыло с+ода"
    assert bracketing == []


def test_text_without_a_marked_token_reaches_the_engine_as_one_run(bracketing):
    """Multi-word context matters to the engine, so an unmarked text is never split into words."""
    assert voice_server.mark_stress("совсем обычные слова", "ru") == "[совсем обычные слова]"
    assert bracketing == ["совсем обычные слова"]


def test_text_of_marked_tokens_only_never_reaches_the_engine(bracketing):
    assert voice_server.mark_stress("Сах+ар", "ru") == "Сах+ар"
    assert bracketing == []


def test_a_long_unbroken_input_is_segmented_in_linear_time(bracketing):
    """Guards the /tts segmentation against a quadratic-backtracking regex (py/polynomial-redos).

    50k characters with no whitespace and no '+' is the worst case for the pattern this replaced;
    the linear pass finishes in milliseconds.
    """
    text = "а" * 50_000

    started = time.perf_counter()
    result = voice_server.mark_stress(text, "ru")
    elapsed = time.perf_counter() - started

    assert result == bracket(text)
    assert bracketing == [text]
    assert elapsed < 1.0


def test_accentuator_failure_on_a_segment_degrades_to_the_raw_text(monkeypatch, accent_enabled):
    def explode(_segment: str) -> str:
        raise RuntimeError("model said no")

    monkeypatch.setitem(voice_server.ACCENTUATORS, "ru", lambda: explode)
    assert voice_server.mark_stress("обычные слова", "ru") == "обычные слова"


def test_accentuator_that_cannot_load_degrades_to_the_rules(stress_file, monkeypatch, accent_enabled):
    def unavailable():
        raise ImportError("no such package")

    stress_file.write_text(json.dumps({r"\bАкме\b": "+Акме"}), encoding="utf-8")
    monkeypatch.setitem(voice_server.ACCENTUATORS, "ru", unavailable)
    assert voice_server.mark_stress("Акме", "ru") == "+Акме"


def test_accentuation_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(voice_server, "USE_ACCENT", False)
    monkeypatch.setitem(voice_server.ACCENTUATORS, "ru", lambda: str.upper)
    assert voice_server.mark_stress("тихо", "ru") == "тихо"
    assert voice_server.accentuator("ru") is None


def test_accentuator_is_loaded_once_and_cached(monkeypatch, accent_enabled):
    loads = []

    def loader():
        loads.append(1)
        return str.strip

    monkeypatch.setitem(voice_server.ACCENTUATORS, "ru", loader)
    assert voice_server.accentuator("ru") is voice_server.accentuator("ru")
    assert loads == [1]


def test_accentuator_is_none_for_a_language_without_one():
    assert voice_server.accentuator("en") is None


# --- stress.json ---------------------------------------------------------------------------------


def test_stress_rules_absent_file_is_not_an_error():
    assert voice_server.stress_rules() == []


def test_stress_rules_object_shape(stress_file):
    stress_file.write_text(json.dumps({r"\bAcme\b": "+Acme"}), encoding="utf-8")
    rules = voice_server.stress_rules()
    assert [pattern.pattern for pattern, _ in rules] == [r"\bAcme\b"]


def test_stress_rules_pair_list_shape_keeps_order(stress_file):
    stress_file.write_text(json.dumps([[r"\bone\b", "+one"], [r"\btwo\b", "+two"]]), encoding="utf-8")
    assert [replacement for _, replacement in voice_server.stress_rules()] == ["+one", "+two"]


def test_stress_rules_skips_an_invalid_regex(stress_file, caplog):
    stress_file.write_text(json.dumps([["[unclosed", "x"], [r"\bok\b", "+ok"]]), encoding="utf-8")
    with caplog.at_level("WARNING"):
        rules = voice_server.stress_rules()
    assert [replacement for _, replacement in rules] == ["+ok"]
    assert "skipping invalid regex" in caplog.text


def test_stress_rules_broken_json_is_ignored(stress_file, caplog):
    stress_file.write_text("{not json", encoding="utf-8")
    with caplog.at_level("WARNING"):
        assert voice_server.stress_rules() == []
    assert "ignored" in caplog.text


def test_stress_rules_are_cached_until_reset(stress_file):
    stress_file.write_text(json.dumps({r"\bAcme\b": "+Acme"}), encoding="utf-8")
    first = voice_server.stress_rules()
    stress_file.unlink()
    assert voice_server.stress_rules() is first  # cached, the file is not re-read per request
    voice_server.reset_caches()
    assert voice_server.stress_rules() == []


# --- chunking ------------------------------------------------------------------------------------


def test_chunk_keeps_a_short_text_whole():
    assert voice_server.chunk("Одно предложение. И второе.") == ["Одно предложение. И второе."]


def test_chunk_splits_on_sentence_boundaries():
    text = " ".join(f"Предложение номер {i}." for i in range(10))
    chunks = voice_server.chunk(text, limit=60)
    assert len(chunks) > 1
    assert all(len(c) <= 60 for c in chunks)
    assert " ".join(chunks) == text


def test_chunk_of_empty_text_is_empty():
    assert voice_server.chunk("") == []


# --- device / compute type ------------------------------------------------------------------------


def test_resolve_device_honours_an_explicit_choice(monkeypatch):
    monkeypatch.setattr(voice_server, "DEVICE", "cuda")
    assert voice_server.resolve_device() == "cuda"


@pytest.mark.parametrize("cuda, expected", [(True, "cuda"), (False, "cpu")])
def test_resolve_device_auto_follows_torch(monkeypatch, cuda, expected):
    monkeypatch.setattr(voice_server, "DEVICE", "auto")
    monkeypatch.setattr(voice_server.torch.cuda, "is_available", lambda: cuda)
    assert voice_server.resolve_device() == expected


def test_resolve_compute_type_explicit(monkeypatch):
    monkeypatch.setattr(voice_server, "COMPUTE_TYPE", "int8_float16")
    assert voice_server.resolve_compute_type("cpu") == "int8_float16"


@pytest.mark.parametrize("device, expected", [("cuda", "float16"), ("cpu", "int8")])
def test_resolve_compute_type_auto(monkeypatch, device, expected):
    monkeypatch.setattr(voice_server, "COMPUTE_TYPE", "auto")
    assert voice_server.resolve_compute_type(device) == expected
