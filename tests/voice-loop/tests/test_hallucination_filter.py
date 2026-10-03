"""The STT hallucination blocklist: silence clips must come back empty, real speech untouched.

Whisper on a near-silent clip invents well-known junk — TV end-credits, "Спасибо за просмотр",
"Thank you for watching" — and one such line once reached a user as a fake dictation. The filter
drops a transcript only when its FULL normalized form is a blocklist entry (or extends one at a
word boundary); genuine speech that merely contains a phrase is never touched.

The same blocklist has a second job (#64): on a long dictation the caption arrives APPENDED to a
complete transcript ("…и покажи покрытие. Продолжение следует…"), so a trailing sentence that
matches is stripped and the speech before it kept. That is a tail-strip, not a whole-drop, and the
two are tested separately below — including the case that must NOT strip, real speech whose last
words merely are a blocklisted phrase with no sentence break in front of them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import voice_server
from conftest import FakeWhisper

SHIPPED_FILE = Path(voice_server.__file__).with_name("stt_hallucinations.txt")


@pytest.fixture
def whisper_saying(monkeypatch):
    """Install a fake recognizer that transcribes exactly the given segments."""

    def install(*segments: str) -> FakeWhisper:
        model = FakeWhisper(segments=segments)
        monkeypatch.setattr(voice_server, "whisper", lambda: model)
        return model

    return install


# --- normalization --------------------------------------------------------------------------------


def test_normalize_collapses_whitespace_lowercases_and_strips_trailing_punctuation():
    assert voice_server.normalize_transcript("  Спасибо  за\tпросмотр!  ") == "спасибо за просмотр"
    assert voice_server.normalize_transcript("Продолжение следует...") == "продолжение следует"
    assert voice_server.normalize_transcript("Thank You For Watching.") == "thank you for watching"


def test_normalize_keeps_internal_punctuation():
    assert voice_server.normalize_transcript("раз, два — три") == "раз, два — три"


# --- the blocklist loader -------------------------------------------------------------------------


def test_blocklist_skips_comments_and_blank_lines_and_normalizes_entries(hallucinations_file):
    hallucinations_file.write_text(
        "# a comment\n\n  Спасибо за просмотр!  \nThank you for watching.\n...\n", encoding="utf-8"
    )
    assert voice_server.hallucination_blocklist() == ["спасибо за просмотр", "thank you for watching"]


def test_blocklist_is_empty_when_the_file_is_missing(hallucinations_file):
    assert voice_server.hallucination_blocklist() == []


def test_blocklist_unreadable_file_is_ignored(hallucinations_file, caplog):
    hallucinations_file.write_bytes(b"\xff\xfe broken")
    with caplog.at_level("WARNING"):
        assert voice_server.hallucination_blocklist() == []
    assert "ignored" in caplog.text


def test_blocklist_is_cached_until_reset(hallucinations_file):
    hallucinations_file.write_text("Спасибо за просмотр\n", encoding="utf-8")
    assert voice_server.hallucination_blocklist() == ["спасибо за просмотр"]
    hallucinations_file.unlink()
    assert voice_server.hallucination_blocklist() == ["спасибо за просмотр"]  # cached
    voice_server.reset_caches()
    assert voice_server.hallucination_blocklist() == []  # re-read after reset


# --- matching -------------------------------------------------------------------------------------


@pytest.fixture
def seeded(hallucinations_file):
    hallucinations_file.write_text("Спасибо за просмотр\nThank you for watching\n", encoding="utf-8")


def test_exact_match_is_dropped_whatever_the_case_and_punctuation(seeded):
    assert voice_server.matched_hallucination("СПАСИБО ЗА ПРОСМОТР!!!") == "спасибо за просмотр"
    assert voice_server.matched_hallucination("Thank you for watching.") == "thank you for watching"


def test_a_transcript_extending_a_pattern_at_a_word_boundary_matches(seeded):
    text = "Спасибо за просмотр, до новых встреч"
    assert voice_server.matched_hallucination(text) == "спасибо за просмотр"


def test_a_pattern_prefix_inside_a_longer_word_does_not_match(seeded):
    assert voice_server.matched_hallucination("Спасибо за просмотренное кино") is None


def test_speech_containing_a_phrase_mid_sentence_passes_through(seeded):
    assert voice_server.matched_hallucination("скажи ему спасибо за просмотр логов") is None


def test_unrelated_speech_passes_through(seeded):
    assert voice_server.matched_hallucination("запусти тесты и покажи покрытие") is None


def test_an_empty_transcript_is_not_a_match(seeded):
    assert voice_server.matched_hallucination("   ") is None


# --- the tail-strip (#64) -------------------------------------------------------------------------


@pytest.fixture
def seeded_captions(hallucinations_file):
    hallucinations_file.write_text(
        "Продолжение следует...\nСпасибо за просмотр\nThanks for watching\n", encoding="utf-8"
    )


def test_a_trailing_caption_is_stripped_and_the_speech_before_it_kept(seeded_captions):
    kept, peeled = voice_server.strip_hallucinated_tail(
        "Запусти тесты и покажи покрытие. Продолжение следует..."
    )

    assert kept == "Запусти тесты и покажи покрытие."
    assert peeled == [("Продолжение следует...", "продолжение следует")]


def test_the_caption_is_stripped_whatever_its_case_and_ellipsis(seeded_captions):
    kept, _ = voice_server.strip_hallucinated_tail("Привет, мир! ПРОДОЛЖЕНИЕ СЛЕДУЕТ…")
    assert kept == "Привет, мир!"


def test_stacked_captions_are_all_peeled(seeded_captions):
    kept, peeled = voice_server.strip_hallucinated_tail(
        "Открой файл. Продолжение следует... Спасибо за просмотр!"
    )

    assert kept == "Открой файл."
    assert [pattern for _, pattern in peeled] == ["спасибо за просмотр", "продолжение следует"]


def test_peeling_stops_at_the_first_sentence_that_is_not_a_caption(seeded_captions):
    text = "Открой файл. Продолжение следует... И закрой его."

    assert voice_server.strip_hallucinated_tail(text) == (text, [])


def test_speech_ending_in_a_blocklisted_phrase_without_a_sentence_break_is_kept(seeded_captions):
    """The whole point of requiring the break: a quiet loss would be worse than the addition."""
    text = "Напиши ему спасибо за просмотр"

    assert voice_server.strip_hallucinated_tail(text) == (text, [])


def test_a_transcript_that_is_only_a_caption_is_left_to_the_whole_drop(seeded_captions):
    text = "Продолжение следует..."

    assert voice_server.strip_hallucinated_tail(text) == (text, [])
    assert voice_server.filter_transcript(text) == ""


def test_filter_strips_the_tail_counts_it_and_logs_what_it_removed(seeded_captions, caplog):
    with caplog.at_level("INFO"):
        kept = voice_server.filter_transcript("Покажи покрытие. Thanks for watching.")

    assert kept == "Покажи покрытие."
    assert "stripped a hallucinated tail" in caplog.text
    assert "Thanks for watching." in caplog.text
    assert voice_server._hallucination_tails_stripped == 1
    assert voice_server._hallucinations_dropped == 0


def test_filter_leaves_genuine_speech_alone(seeded_captions):
    assert voice_server.filter_transcript("Покажи покрытие. И запусти линтер.") == (
        "Покажи покрытие. И запусти линтер."
    )
    assert voice_server._hallucinations_by_pattern == {}


# --- the /stt endpoint and the counter ------------------------------------------------------------


def test_stt_drops_a_hallucination_counts_it_and_logs_the_pattern(client, whisper_saying, seeded, caplog):
    whisper_saying(" Спасибо за просмотр! ")

    with caplog.at_level("INFO"):
        response = client.post("/stt", files={"audio": ("clip.wav", b"RIFFfake", "audio/wav")})

    assert response.status_code == 200
    assert response.json() == {"text": "", "language": "ru", "duration": 1.25}  # the empty-transcript shape
    assert "спасибо за просмотр" in caplog.text
    assert client.get("/health").json()["stt_hallucinations_dropped"] == 1


def test_stt_counts_every_drop(client, whisper_saying, seeded):
    whisper_saying("Thank you for watching.")
    client.post("/stt", files={"audio": ("clip.wav", b"RIFFfake", "audio/wav")})
    client.post("/stt", files={"audio": ("clip.wav", b"RIFFfake", "audio/wav")})
    assert client.get("/health").json()["stt_hallucinations_dropped"] == 2


def test_stt_passes_genuine_speech_untouched(client, whisper_saying, seeded):
    whisper_saying(" привет ", " мир ")

    response = client.post("/stt", files={"audio": ("clip.wav", b"RIFFfake", "audio/wav")})

    assert response.json()["text"] == "привет мир"
    assert client.get("/health").json()["stt_hallucinations_dropped"] == 0


def test_stt_strips_a_trailing_caption_and_returns_the_rest(client, whisper_saying, seeded_captions, caplog):
    whisper_saying(" Запусти тесты и покажи покрытие. ", " Продолжение следует... ")

    with caplog.at_level("INFO"):
        response = client.post("/stt", files={"audio": ("clip.wav", b"RIFFfake", "audio/wav")})

    assert response.json()["text"] == "Запусти тесты и покажи покрытие."
    assert "stripped a hallucinated tail" in caplog.text
    body = client.get("/health").json()
    assert body["stt_hallucination_tails_stripped"] == 1
    assert body["stt_hallucinations_dropped"] == 0  # a strip is not a drop
    assert body["stt_hallucinations_by_pattern"] == {"продолжение следует": 1}


def test_health_reports_the_corpus_of_what_this_microphone_hallucinates(client, whisper_saying, seeded):
    whisper_saying("Спасибо за просмотр")
    client.post("/stt", files={"audio": ("clip.wav", b"RIFFfake", "audio/wav")})
    whisper_saying("Thank you for watching.")
    client.post("/stt", files={"audio": ("clip.wav", b"RIFFfake", "audio/wav")})
    client.post("/stt", files={"audio": ("clip.wav", b"RIFFfake", "audio/wav")})

    assert client.get("/health").json()["stt_hallucinations_by_pattern"] == {
        "спасибо за просмотр": 1,
        "thank you for watching": 2,
    }


def test_reset_caches_zeroes_the_counters(client, whisper_saying, seeded_captions):
    whisper_saying(" Покажи покрытие. ", " Спасибо за просмотр! ")
    client.post("/stt", files={"audio": ("clip.wav", b"RIFFfake", "audio/wav")})
    whisper_saying("Спасибо за просмотр")
    client.post("/stt", files={"audio": ("clip.wav", b"RIFFfake", "audio/wav")})
    assert voice_server._hallucinations_dropped == 1
    assert voice_server._hallucination_tails_stripped == 1

    voice_server.reset_caches()

    assert voice_server._hallucinations_dropped == 0
    assert voice_server._hallucination_tails_stripped == 0
    body = client.get("/health").json()
    assert body["stt_hallucinations_dropped"] == 0
    assert body["stt_hallucination_tails_stripped"] == 0
    assert body["stt_hallucinations_by_pattern"] == {}


# --- the shipped seed file ------------------------------------------------------------------------


def test_the_shipped_blocklist_parses_and_covers_the_known_classes(monkeypatch):
    monkeypatch.setattr(voice_server, "HALLUCINATIONS_FILE", SHIPPED_FILE)

    patterns = voice_server.hallucination_blocklist()

    assert len(patterns) >= 15
    assert "спасибо за просмотр" in patterns
    assert "субтитры делал dimatorzok" in patterns
    assert "редактор субтитров" in patterns
    assert "продолжение следует" in patterns
    assert "подписывайтесь на канал" in patterns
    assert "thank you for watching" in patterns
    assert "subtitles by the amara.org community" in patterns


def test_the_shipped_blocklist_catches_the_tv_credit_that_reached_a_user(monkeypatch):
    monkeypatch.setattr(voice_server, "HALLUCINATIONS_FILE", SHIPPED_FILE)
    text = "Редактор субтитров А.Семкин Корректор А.Егорова"
    assert voice_server.matched_hallucination(text) == "редактор субтитров"


def test_the_shipped_blocklist_covers_the_closing_caption_class(monkeypatch):
    monkeypatch.setattr(voice_server, "HALLUCINATIONS_FILE", SHIPPED_FILE)

    patterns = voice_server.hallucination_blocklist()

    assert "субтитры" in patterns  # bare: the studio name after it is unbounded
    assert "спасибо за внимание" in patterns
    assert "to be continued" in patterns
    assert "subtitles by" in patterns


def test_the_shipped_blocklist_strips_the_caption_reported_in_64(monkeypatch):
    """The report: a complete dictation with «продолжение следует…» appended after it."""
    monkeypatch.setattr(voice_server, "HALLUCINATIONS_FILE", SHIPPED_FILE)
    text = "Открой воркфлоу и посмотри, почему упала матрица. Продолжение следует…"

    assert voice_server.filter_transcript(text) == "Открой воркфлоу и посмотри, почему упала матрица."


@pytest.mark.parametrize(
    "text",
    [
        "Субтитры сделал DimaTorzok",
        "Субтитры и озвучка канала",
        "Thanks for watching!",
        "Subtitles by the Amara.org community",
    ],
)
def test_the_shipped_blocklist_strips_each_caption_off_a_real_transcript(monkeypatch, text):
    monkeypatch.setattr(voice_server, "HALLUCINATIONS_FILE", SHIPPED_FILE)

    assert voice_server.filter_transcript(f"Покажи покрытие. {text}") == "Покажи покрытие."
