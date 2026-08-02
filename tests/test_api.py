"""The HTTP contract — the shape the plugin scripts and the selftest depend on.

The models are faked; the request handling, the language routing and the WAV encoding are real. A
`/tts` response really is a RIFF file produced by soundfile, which is exactly what `selftest.sh`
checks before it hands the bytes back to recognition.
"""

from __future__ import annotations

import io
import json
import struct
import wave

import pytest

import voice_server


def wav_upload(seconds: float, rate: int = 8000) -> bytes:
    """A real, minimal PCM WAV of the requested duration — what the duration guard parses."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x00" * int(rate * seconds))
    return buf.getvalue()


def test_health_reports_capabilities(client, monkeypatch):
    monkeypatch.setattr(voice_server, "DEVICE", "cpu")
    monkeypatch.setattr(voice_server, "STT_MODEL", "small")

    body = client.get("/health").json()

    assert body["ok"] is True
    assert body["version"] == voice_server.SERVER_VERSION == "0.4.0"
    assert body["model_concurrency"] == voice_server.MODEL_CONCURRENCY >= 1
    assert body["model_in_flight"] == 0
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


# --- request-level caps ----------------------------------------------------------------------------


def test_tts_blob_carries_the_small_cap_and_points_at_the_stream(client, fake_silero, monkeypatch):
    monkeypatch.setattr(voice_server, "MAX_TTS_TEXT_BLOB", 50)

    response = client.post("/tts", json={"text": "Слово. " * 20})

    assert response.status_code == 400
    assert "the limit is 50" in response.json()["error"]
    assert "/tts/stream" in response.json()["hint"]
    assert "VOICE_LOOP_MAX_TTS_TEXT_BLOB" in response.json()["hint"]
    assert fake_silero.calls == []


def test_tts_stream_carries_its_own_larger_cap(client, fake_silero, monkeypatch):
    monkeypatch.setattr(voice_server, "MAX_TTS_TEXT", 50)
    monkeypatch.setattr(voice_server, "MAX_TTS_TEXT_BLOB", 10)  # must NOT apply to the stream

    over_stream_cap = client.post("/tts/stream", json={"text": "Слово. " * 20})
    assert over_stream_cap.status_code == 400
    assert "the limit is 50" in over_stream_cap.json()["error"]
    assert "VOICE_LOOP_MAX_TTS_TEXT" in over_stream_cap.json()["hint"]

    over_blob_only = client.post("/tts/stream", json={"text": "Тридцать три символа ровно тут."})
    assert over_blob_only.status_code == 200  # longer than the blob cap, fine on the stream
    assert fake_silero.calls != []


def test_tts_text_cap_defaults_are_the_documented_ones(client, fake_silero):
    assert voice_server.MAX_TTS_TEXT == 20000
    assert voice_server.MAX_TTS_TEXT_BLOB == 3000
    response = client.post("/tts", json={"text": "Ok."})
    assert response.status_code == 200


def test_stt_rejects_an_upload_over_the_size_cap(client, fake_whisper, monkeypatch):
    monkeypatch.setattr(voice_server, "MAX_UPLOAD_BYTES", 16)

    response = client.post("/stt", files={"audio": ("clip.wav", b"R" * 64, "audio/wav")})

    assert response.status_code == 413
    assert "16 byte limit" in response.json()["error"]
    assert "VOICE_LOOP_MAX_UPLOAD_BYTES" in response.json()["hint"]
    assert fake_whisper.calls == []


def test_stt_accepts_an_upload_at_the_size_cap(client, fake_whisper, monkeypatch):
    monkeypatch.setattr(voice_server, "MAX_UPLOAD_BYTES", 8)
    response = client.post("/stt", files={"audio": ("clip.wav", b"RIFFfake", "audio/wav")})
    assert response.status_code == 200
    assert len(fake_whisper.calls) == 1


def test_stt_rejects_a_wav_longer_than_the_duration_cap(client, fake_whisper, monkeypatch):
    monkeypatch.setattr(voice_server, "MAX_STT_SECONDS", 1.0)

    response = client.post("/stt", files={"audio": ("clip.wav", wav_upload(2.0), "audio/wav")})

    assert response.status_code == 413
    assert response.json()["error"] == "audio too long: 2 seconds, the limit is 1"
    assert "VOICE_LOOP_MAX_STT_SECONDS" in response.json()["hint"]
    assert fake_whisper.calls == []


def test_stt_accepts_a_wav_under_the_duration_cap(client, fake_whisper):
    assert voice_server.MAX_STT_SECONDS == 600.0  # the documented default
    response = client.post("/stt", files={"audio": ("clip.wav", wav_upload(0.5), "audio/wav")})
    assert response.status_code == 200
    assert len(fake_whisper.calls) == 1


def test_stt_duration_cap_passes_non_wav_codecs_on_the_byte_cap_alone(client, fake_whisper, monkeypatch):
    """Compressed audio reveals duration only by decoding — the very work the cap avoids."""
    monkeypatch.setattr(voice_server, "MAX_STT_SECONDS", 0.0)  # any measured duration would refuse
    response = client.post("/stt", files={"audio": ("clip.ogg", b"OggS" + b"\x00" * 64, "audio/ogg")})
    assert response.status_code == 200
    assert len(fake_whisper.calls) == 1


def test_stt_duration_cap_passes_an_unparseable_riff(client, fake_whisper, monkeypatch):
    monkeypatch.setattr(voice_server, "MAX_STT_SECONDS", 0.0)
    response = client.post("/stt", files={"audio": ("clip.wav", b"RIFFfake", "audio/wav")})
    assert response.status_code == 200
    assert len(fake_whisper.calls) == 1


def test_wav_duration_skips_a_zero_rate_header():
    """A crafted header must not divide by zero — it degrades to 'unmeasurable', not an error."""
    data = bytearray(wav_upload(1.0))
    struct.pack_into("<I", data, 24, 0)  # the fmt chunk's sample-rate field
    assert voice_server.wav_duration_seconds(bytes(data)) is None


# --- cross-site browser guard ----------------------------------------------------------------------
# Multipart is a CORS-"simple" body, so a malicious page can fire real cross-origin POSTs at a
# loopback server. Browser-labelled cross-site requests get a plain 403; header-less clients pass.


@pytest.mark.parametrize("path", ["/stt", "/tts", "/tts/stream"])
def test_post_endpoints_refuse_a_cross_site_browser_request(client, fake_whisper, fake_silero, path):
    response = client.post(
        path,
        files={"audio": ("clip.wav", b"RIFFfake", "audio/wav")} if path == "/stt" else None,
        json=None if path == "/stt" else {"text": "Привет."},
        headers={"Sec-Fetch-Site": "cross-site"},
    )

    assert response.status_code == 403
    assert response.json() == {"error": "cross-site browser requests are not accepted"}
    assert fake_whisper.calls == [] and fake_silero.calls == []


def test_stt_refuses_a_foreign_origin(client, fake_whisper):
    response = client.post(
        "/stt",
        files={"audio": ("clip.wav", b"RIFFfake", "audio/wav")},
        headers={"Origin": "https://evil.example"},
    )

    assert response.status_code == 403
    assert response.json() == {"error": "cross-origin browser requests are not accepted"}
    assert fake_whisper.calls == []


@pytest.mark.parametrize(
    "origin", ["http://127.0.0.1:8355", "http://localhost:3000", "http://[::1]:8355", "null"]
)
def test_stt_accepts_loopback_and_null_origins(client, fake_whisper, origin):
    response = client.post(
        "/stt", files={"audio": ("clip.wav", b"RIFFfake", "audio/wav")}, headers={"Origin": origin}
    )
    assert response.status_code == 200


def test_stt_refuses_a_malformed_origin(client, fake_whisper):
    response = client.post(
        "/stt", files={"audio": ("clip.wav", b"RIFFfake", "audio/wav")}, headers={"Origin": "http://[::1"}
    )
    assert response.status_code == 403


def test_same_origin_sec_fetch_site_passes(client, fake_silero):
    response = client.post("/tts", json={"text": "Привет."}, headers={"Sec-Fetch-Site": "same-origin"})
    assert response.status_code == 200
