"""The XTTS-v2 engine: request routing, refusals, the lazy loader, the OOM fallback.

coqui-tts is an OPTIONAL dependency and is never installed here — the import seam is faked exactly
like faster-whisper and the Silero hub. Every refusal is request-level by design: the server must
boot and serve /stt no matter how broken the TTS configuration is.
"""

from __future__ import annotations

import io
import sys

import soundfile as sf

import voice_server
from conftest import FakeXtts

ACUTE = "́"


# --- /tts through the xtts engine -----------------------------------------------------------------


def test_tts_xtts_returns_a_wav_at_the_xtts_sample_rate(client, fake_xtts, xtts_engine):
    response = client.post("/tts", json={"text": "Привет."})

    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    assert response.content[:4] == b"RIFF"
    _wav, sample_rate = sf.read(io.BytesIO(response.content))
    assert sample_rate == voice_server.XTTS_SR
    assert fake_xtts.calls[0]["speaker_wav"] == str(xtts_engine)
    assert fake_xtts.calls[0]["language"] == "ru"


def test_tts_xtts_strips_stress_markers_instead_of_applying_them(client, fake_xtts):
    client.post("/tts", json={"text": f"Сах+ар и робо{ACUTE}та."})
    assert fake_xtts.calls[0]["text"] == "Сахар и робота."


def test_tts_xtts_speaks_languages_silero_lacks(client, fake_xtts):
    response = client.post("/tts", json={"text": "Merhaba.", "language": "tr"})

    assert response.status_code == 200
    assert fake_xtts.calls[0]["language"] == "tr"


def test_tts_xtts_rejects_a_language_xtts_lacks(client, fake_xtts):
    response = client.post("/tts", json={"text": "Привіт.", "language": "uk"})

    assert response.status_code == 400
    body = response.json()
    assert "uk" in body["error"]
    assert "ru" in body["supported"] and "tr" in body["supported"]
    assert fake_xtts.calls == []


# --- request-level refusals (the server still boots for /stt) -------------------------------------


def test_tts_xtts_without_coqui_tts_is_a_clear_500(client, monkeypatch, tmp_path):
    reference = tmp_path / "reference.wav"
    reference.write_bytes(b"RIFFfake")
    monkeypatch.setattr(voice_server, "TTS_ENGINE", "xtts")
    monkeypatch.setattr(voice_server, "XTTS_REFERENCE", str(reference))
    monkeypatch.setitem(sys.modules, "TTS", None)  # importing None raises ImportError

    response = client.post("/tts", json={"text": "Привет."})

    assert response.status_code == 500
    body = response.json()
    assert "coqui-tts" in body["error"]
    assert "pip install coqui-tts" in body["hint"]
    assert "COQUI_TOS_AGREED" in body["hint"]


def test_tts_xtts_without_a_reference_is_a_clear_500(client, monkeypatch, coqui_installed):
    monkeypatch.setattr(voice_server, "TTS_ENGINE", "xtts")

    response = client.post("/tts", json={"text": "Привет."})

    assert response.status_code == 500
    assert "VOICE_LOOP_XTTS_REFERENCE" in response.json()["error"]


def test_tts_xtts_with_a_missing_reference_file_is_a_clear_500(client, monkeypatch, coqui_installed, tmp_path):
    monkeypatch.setattr(voice_server, "TTS_ENGINE", "xtts")
    monkeypatch.setattr(voice_server, "XTTS_REFERENCE", str(tmp_path / "nowhere.wav"))

    response = client.post("/tts", json={"text": "Привет."})

    assert response.status_code == 500
    assert "not found" in response.json()["error"]


def test_tts_unknown_engine_is_a_clear_500(client, monkeypatch):
    monkeypatch.setattr(voice_server, "TTS_ENGINE", "espeak")

    response = client.post("/tts", json={"text": "Привет."})

    assert response.status_code == 500
    body = response.json()
    assert "espeak" in body["error"]
    assert body["supported"] == ["silero", "xtts"]


def test_stt_works_with_the_xtts_engine_misconfigured(client, fake_whisper, monkeypatch):
    monkeypatch.setattr(voice_server, "TTS_ENGINE", "xtts")
    monkeypatch.setitem(sys.modules, "TTS", None)

    response = client.post("/stt", files={"audio": ("clip.wav", b"RIFFfake", "audio/wav")})

    assert response.status_code == 200
    assert response.json()["text"] == "hello world"


# --- the lazy loader ------------------------------------------------------------------------------


def test_xtts_is_loaded_once_on_the_resolved_device(monkeypatch, coqui_installed):
    monkeypatch.setattr(voice_server, "DEVICE", "cpu")

    first = voice_server.xtts()
    second = voice_server.xtts()

    assert first is second
    assert len(coqui_installed.instances) == 1
    assert first.args == (voice_server.XTTS_MODEL_ID,)
    assert first.device == "cpu"


def test_xtts_model_dir_override_loads_from_disk(monkeypatch, coqui_installed):
    monkeypatch.setattr(voice_server, "DEVICE", "cpu")
    monkeypatch.setattr(voice_server, "XTTS_MODEL_DIR", "/models/xtts-v2")

    model = voice_server.xtts()

    assert model.kwargs == {"model_path": "/models/xtts-v2", "config_path": "/models/xtts-v2/config.json"}


def test_xtts_gpu_oom_falls_back_to_cpu(monkeypatch, import_fake, caplog):
    attempts: list[object] = []

    class OomTTS(FakeXtts):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__()

        def to(self, device: object) -> "OomTTS":
            attempts.append(device)
            if device == "cuda":
                raise voice_server.torch.cuda.OutOfMemoryError("CUDA out of memory")
            return super().to(device)

    api = import_fake("TTS.api", TTS=OomTTS)
    import_fake("TTS", api=api)
    monkeypatch.setattr(voice_server, "DEVICE", "cuda")

    with caplog.at_level("WARNING"):
        model = voice_server.xtts()

    assert attempts == ["cuda", "cpu"]
    assert model.device == "cpu"
    assert "retrying on CPU" in caplog.text


def test_reset_caches_drops_the_xtts_model(coqui_installed, monkeypatch):
    monkeypatch.setattr(voice_server, "DEVICE", "cpu")
    first = voice_server.xtts()
    voice_server.reset_caches()
    assert voice_server.xtts() is not first


# --- /health --------------------------------------------------------------------------------------


def test_health_reports_the_engine_and_the_loaded_cloner(client, monkeypatch):
    body = client.get("/health").json()
    assert body["tts_engine"] == "silero"
    assert body["xtts_loaded"] is False

    monkeypatch.setattr(voice_server, "TTS_ENGINE", "xtts")
    voice_server._xtts = object()
    body = client.get("/health").json()
    assert body["tts_engine"] == "xtts"
    assert body["xtts_loaded"] is True
