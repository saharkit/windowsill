"""The XTTS-v2 engine: request routing, refusals, the lazy loader, the OOM fallback.

coqui-tts is an OPTIONAL dependency and is never installed here — the import seam is faked exactly
like faster-whisper and the Silero hub. Every refusal is request-level by design: the server must
boot and serve /stt no matter how broken the TTS configuration is.
"""

from __future__ import annotations

import io

import pytest
sf = pytest.importorskip("soundfile")

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


def test_turkish_xtts_request_preserves_the_cloned_voice_and_language(client, fake_xtts, xtts_engine, monkeypatch):
    """L2 GAP: deleting this composition test could route tr through a local-only path or lose the
    request language, even though XTTS's standalone language set still contains Turkish."""
    monkeypatch.setattr(voice_server, "TTS_ENGINE", "silero")
    monkeypatch.setattr(voice_server, "TTS_ENGINE_BY_LANGUAGE", {"tr": "xtts"})
    response = client.post("/tts", json={"text": "Merhaba dünya.", "language": "tr"})

    assert response.status_code == 200
    assert response.headers["x-voice-loop-engine"] == "xtts"
    assert fake_xtts.calls[0]["language"] == "tr"
    assert fake_xtts.calls[0]["speaker_wav"] == str(xtts_engine)


def test_tts_xtts_rejects_a_language_xtts_lacks(client, fake_xtts):
    response = client.post("/tts", json={"text": "Привіт.", "language": "uk"})

    assert response.status_code == 400
    body = response.json()
    assert "uk" in body["error"]
    assert "ru" in body["supported"] and "tr" in body["supported"]
    assert fake_xtts.calls == []


# --- request-level refusals (the server still boots for /stt) -------------------------------------


SECRET_PATH = "/home/user/.secret/venvs/xtts/lib/python3.12/site-packages/transformers/__init__.py"


@pytest.fixture
def xtts_engine_bare(monkeypatch, tmp_path):
    """The xtts engine with a reference on disk and NO import seam installed.

    `xtts_engine` cannot serve these tests: it installs the fake `TTS.api`, which the import probe
    would then find. The caller plants the import failure it wants with `import_raises`.
    """
    reference = tmp_path / "reference.wav"
    reference.write_bytes(b"RIFFfake")
    monkeypatch.setattr(voice_server, "TTS_ENGINE", "xtts")
    monkeypatch.setattr(voice_server, "XTTS_REFERENCE", str(reference))
    return reference


def test_tts_xtts_without_coqui_tts_is_a_clear_500(client, xtts_engine_bare, import_raises, caplog):
    """The one case that really IS a missing package: it says so, and hands over the install hint."""
    import_raises("TTS", ModuleNotFoundError(f"No module named 'TTS' (searched {SECRET_PATH})", name="TTS"))

    with caplog.at_level("WARNING"):
        response = client.post("/tts", json={"text": "Привет."})

    assert response.status_code == 500
    body = response.json()
    assert body["error"] == (
        "xtts import failed: ModuleNotFoundError for 'TTS' — the optional coqui-tts package is not installed"
    )
    assert "pip install coqui-tts" in body["hint"]
    assert "COQUI_TOS_AGREED" in body["hint"]
    assert SECRET_PATH not in response.text  # the exception's own text never reaches the client
    assert SECRET_PATH in caplog.text  # it reaches the operator, where it belongs


def test_tts_xtts_with_a_broken_dependency_stack_names_the_class_not_the_message(
    client, xtts_engine_bare, import_raises, caplog
):
    """coqui-tts installed but unimportable is NOT "not installed" — and the wire gets the SHAPE.

    This is the failure people actually hit (#34/#35): coqui-tts pins neither torch nor torchaudio,
    an unpinned transformers moves to 5.x and takes `isin_mps_friendly` away, and the import dies
    inside a dependency. Reported as "coqui-tts is not installed", it sends you to reinstall a
    package that is already there. Reported with `str(err)` attached, it ships paths and config to
    an external client (CodeQL: information exposure through an exception) — so the client is told
    the exception class, and the message stays in the log.
    """
    import_raises(
        "TTS",
        ImportError(f"cannot import name 'isin_mps_friendly' from 'transformers'\n({SECRET_PATH})"),
    )

    with caplog.at_level("WARNING"):
        response = client.post("/tts", json={"text": "Привет."})

    assert response.status_code == 500
    body = response.json()
    assert body["error"] == "xtts import failed: ImportError — the message is in the server log"
    assert "not installed" not in body["error"]
    assert "pip install coqui-tts" not in body["hint"]  # the misleading hint stays away
    assert "transformers<5" in body["hint"] and "torch==2.8.0" in body["hint"]
    assert SECRET_PATH not in response.text
    assert "isin_mps_friendly" not in response.text
    # the operator's copy keeps the truth, on one line, with no traceback
    assert "ImportError: cannot import name 'isin_mps_friendly'" in caplog.text
    assert SECRET_PATH in caplog.text
    assert "Traceback" not in caplog.text


def test_tts_xtts_with_a_missing_transitive_dependency_names_that_module(
    client, xtts_engine_bare, import_raises, caplog
):
    """A ModuleNotFoundError for something else is not a missing coqui-tts (torchaudio, #34).

    The failing module's NAME is safe to ship — it is a fact about the dependency graph, not about
    this machine — and it is the single most useful word in the refusal, so it goes on the wire.
    """
    import_raises("TTS", ModuleNotFoundError(f"No module named 'torchaudio' ({SECRET_PATH})", name="torchaudio"))

    with caplog.at_level("WARNING"):
        response = client.post("/tts", json={"text": "Привет."})

    assert response.status_code == 500
    body = response.json()
    assert body["error"] == (
        "xtts import failed: ModuleNotFoundError in dependency 'torchaudio' — the message is in the server log"
    )
    assert "not installed" not in body["error"]
    assert "pip install coqui-tts" not in body["hint"]
    assert SECRET_PATH not in response.text
    assert SECRET_PATH in caplog.text


def test_tts_xtts_import_failure_without_a_module_name_still_refuses_cleanly(
    client, xtts_engine_bare, import_raises
):
    """`ModuleNotFoundError` carries no `name` when raised by hand — no `'None'` on the wire."""
    import_raises("TTS", ModuleNotFoundError(f"something went wrong in {SECRET_PATH}"))

    response = client.post("/tts", json={"text": "Привет."})

    assert response.status_code == 500
    body = response.json()
    assert body["error"] == "xtts import failed: ModuleNotFoundError — the message is in the server log"
    assert "None" not in body["error"]
    assert SECRET_PATH not in response.text


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
    assert body["supported"] == ["silero", "xtts", "ukrainian"]


def test_stt_works_with_the_xtts_engine_misconfigured(client, fake_whisper, monkeypatch, import_raises):
    monkeypatch.setattr(voice_server, "TTS_ENGINE", "xtts")
    import_raises("TTS", ModuleNotFoundError("No module named 'TTS'", name="TTS"))

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
    assert voice_server._xtts_device == "cpu"  # the queue synthesis_device() will send work to


def test_xtts_model_dir_override_loads_from_disk(monkeypatch, coqui_installed):
    monkeypatch.setattr(voice_server, "DEVICE", "cpu")
    monkeypatch.setattr(voice_server, "XTTS_MODEL_DIR", "/models/xtts-v2")

    model = voice_server.xtts()

    assert model.kwargs == {"model_path": "/models/xtts-v2", "config_path": "/models/xtts-v2/config.json"}


def test_xtts_uses_cpu_when_free_vram_is_below_the_tenant_floor(monkeypatch, coqui_installed, caplog):
    """L2 GAP: deleting this test lets XTTS claim a crowded 6 GB card and evict whisper/RVC.

    The guard is the language-independent tenancy decision; the Turkish route is covered above.
    """
    monkeypatch.setattr(voice_server, "DEVICE", "cuda")
    monkeypatch.setattr(voice_server, "XTTS_MIN_FREE_VRAM_BYTES", 3 * 1024**3)
    monkeypatch.setattr(voice_server.torch.cuda, "mem_get_info", lambda device: (2 * 1024**3, 6 * 1024**3))

    with caplog.at_level("WARNING"):
        model = voice_server.xtts()

    assert model.device == "cpu"
    assert voice_server._xtts_device == "cpu"
    assert "preserve GPU tenants" in caplog.text


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
    # And the gate follows the hardware, not the request: this synthesis now queues on the CPU,
    # which is the case no reading of VOICE_LOOP_DEVICE could have guessed.
    assert voice_server._xtts_device == "cpu"
    assert voice_server.synthesis_device("xtts") == "cpu"


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
