"""The HTTP contract — the shape the plugin scripts and the selftest depend on.

The models are faked; the request handling, the language routing and the WAV encoding are real. A
`/tts` response really is a RIFF file produced by soundfile, which is exactly what `selftest.sh`
checks before it hands the bytes back to recognition.
"""

from __future__ import annotations

import json

import voice_server


def test_health_reports_capabilities(client, monkeypatch):
    monkeypatch.setattr(voice_server, "DEVICE", "cpu")
    monkeypatch.setattr(voice_server, "STT_MODEL", "small")

    body = client.get("/health").json()

    assert body["ok"] is True
    assert body["device"] == "cpu"
    assert body["language"] == "ru"
    assert body["stt_model"] == "small"
    assert "en" in body["tts_languages"]
    assert body["accentuated_languages"] == ["ru", "uk"]
    assert body["stt_loaded"] is False
    assert body["tts_loaded"] == []


def test_health_reports_a_loaded_voice(client, monkeypatch):
    monkeypatch.setattr(voice_server.torch.cuda, "is_available", lambda: True)
    monkeypatch.setattr(voice_server, "DEVICE", "auto")
    voice_server._tts["en"] = object()

    body = client.get("/health").json()

    assert body["cuda"] is True
    assert body["device"] == "cuda"
    assert body["tts_loaded"] == ["en"]


# --- /stt ------------------------------------------------------------------------------------------


def test_stt_joins_and_strips_segments(client, fake_whisper):
    response = client.post("/stt", files={"audio": ("clip.wav", b"RIFFfake", "audio/wav")})

    assert response.status_code == 200
    assert response.json() == {"text": "hello world", "language": "ru", "duration": 1.25}


def test_stt_defaults_to_the_configured_language(client, fake_whisper, monkeypatch):
    monkeypatch.setattr(voice_server, "LANGUAGE", "en")
    client.post("/stt", files={"audio": ("clip.wav", b"RIFFfake", "audio/wav")})
    assert fake_whisper.calls[0]["language"] == "en"


def test_stt_takes_the_language_from_the_query_and_lowercases_it(client, fake_whisper):
    client.post("/stt?language=EN", files={"audio": ("clip.wav", b"RIFFfake", "audio/wav")})
    assert fake_whisper.calls[0]["language"] == "en"


def test_stt_passes_the_lexicon_hint_and_uses_vad(client, fake_whisper, monkeypatch):
    monkeypatch.setattr(voice_server, "STT_HINT", "Acme, kubectl")
    client.post("/stt", files={"audio": ("clip.wav", b"RIFFfake", "audio/wav")})
    assert fake_whisper.calls[0]["initial_prompt"] == "Acme, kubectl"
    assert fake_whisper.calls[0]["vad_filter"] is True


# --- /tts ------------------------------------------------------------------------------------------


def test_tts_returns_a_real_wav(client, fake_silero):
    response = client.post("/tts", json={"text": "Привет."})

    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    assert response.content[:4] == b"RIFF"
    assert fake_silero.calls[0]["speaker"] == "baya"
    assert fake_silero.calls[0]["sample_rate"] == voice_server.TTS_SR


def test_tts_english_uses_the_english_voice_and_skips_stress_marking(client, fake_silero):
    response = client.post("/tts", json={"text": "Build finished. Two tests failed.", "language": "en"})

    assert response.status_code == 200
    assert response.content[:4] == b"RIFF"
    assert fake_silero.calls[0]["speaker"] == "en_0"
    assert fake_silero.calls[0]["text"] == "Build finished. Two tests failed."


def test_tts_applies_the_stress_pipeline_before_synthesis(client, fake_silero, stress_file, monkeypatch):
    stress_file.write_text(json.dumps({r"\bАкме\b": "+Акме"}), encoding="utf-8")
    monkeypatch.setattr(voice_server, "USE_ACCENT", False)

    client.post("/tts", json={"text": "Акме готово."})

    assert fake_silero.calls[0]["text"] == "+Акме готово."


def test_tts_honours_an_explicit_speaker(client, fake_silero):
    client.post("/tts", json={"text": "Привет.", "speaker": "xenia"})
    assert fake_silero.calls[0]["speaker"] == "xenia"


def test_tts_chunks_a_long_text_into_several_calls(client, fake_silero):
    text = " ".join(f"Предложение номер {i}." for i in range(120))
    client.post("/tts", json={"text": text})
    assert len(fake_silero.calls) > 1


def test_tts_rejects_an_unsupported_language(client, fake_silero):
    response = client.post("/tts", json={"text": "Merhaba.", "language": "tr"})

    assert response.status_code == 400
    body = response.json()
    assert "tr" in body["error"]
    assert "en" in body["supported"] and "ru" in body["supported"]
    assert fake_silero.calls == []


def test_tts_rejects_empty_text(client, fake_silero):
    response = client.post("/tts", json={"text": "   "})

    assert response.status_code == 400
    assert response.json()["error"] == "empty text"
    assert fake_silero.calls == []


def test_tts_language_is_case_insensitive(client, fake_silero):
    response = client.post("/tts", json={"text": "Hello.", "language": "EN"})
    assert response.status_code == 200
    assert fake_silero.calls[0]["speaker"] == "en_0"
