"""The ukrainian engine (robinhad/ukrainian-tts): routing, refusals, the lazy loader, the fallback.

ukrainian-tts is an OPTIONAL dependency and is never installed here — the import seam is faked
exactly like coqui-tts, faster-whisper and the Silero hub, and the synthesis seam answers with real
WAV bytes so the buffer round-trip in ukrainian_pieces() runs for real. Every refusal is
request-level by design: the server must boot and serve /stt no matter how broken the TTS
configuration is.
"""

from __future__ import annotations

import importlib
import io

import pytest
sf = pytest.importorskip("soundfile")

import voice_server
from test_streaming import events, wav_of

ACUTE = "́"  # a combining acute typed by a human — strip_stress_markers() must remove it

SECRET_PATH = "/home/user/.secret/venvs/uk/lib/python3.12/site-packages/ukrainian_tts/tts.py"


class FakeUkrainian:
    """Stands in for a loaded robinhad/ukrainian-tts engine: writes a real WAV of silence."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def tts(self, text: str, voice: str, stress: str, file) -> tuple[None, str]:
        self.calls.append({"text": text, "voice": voice, "stress": stress})
        sf.write(file, [0.0] * (voice_server.UKRAINIAN_SR // 20), voice_server.UKRAINIAN_SR, format="WAV")
        return None, text


class BrokenUkrainian(FakeUkrainian):
    """A loaded engine whose synthesis always raises — the failure the fallback answers."""

    def tts(self, text: str, voice: str, stress: str, file) -> tuple[None, str]:
        self.calls.append({"text": text, "voice": voice, "stress": stress})
        raise RuntimeError("ukrainian-tts said no")


@pytest.fixture
def ukrainian_installed(import_fake):
    """A fake importable ukrainian-tts (`from ukrainian_tts.tts import TTS`) — probe and loader."""
    instances: list[FakeUkrainian] = []

    class FakeTTS(FakeUkrainian):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__()
            self.args, self.kwargs = args, kwargs
            instances.append(self)

    FakeTTS.instances = instances
    tts_module = import_fake("ukrainian_tts.tts", TTS=FakeTTS)
    import_fake("ukrainian_tts", tts=tts_module)
    return FakeTTS


@pytest.fixture
def ukrainian_engine(monkeypatch, ukrainian_installed):
    """The ukrainian engine selected globally, with the importable package — the happy setup."""
    monkeypatch.setattr(voice_server, "TTS_ENGINE", "ukrainian")
    return ukrainian_installed


@pytest.fixture
def fake_ukrainian(monkeypatch, ukrainian_engine):
    model = FakeUkrainian()
    monkeypatch.setattr(voice_server, "ukrainian_tts", lambda: model)
    return model


# --- /tts through the ukrainian engine ------------------------------------------------------------


def test_tts_ukrainian_returns_a_wav_at_the_engine_sample_rate(client, fake_ukrainian):
    response = client.post("/tts", json={"text": "Привіт.", "language": "uk"})

    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    assert response.headers["x-voice-loop-engine"] == "ukrainian"
    assert response.content[:4] == b"RIFF"
    _wav, sample_rate = sf.read(io.BytesIO(response.content))
    assert sample_rate == voice_server.UKRAINIAN_SR
    # The shipped default voice, and the engine's own dictionary stress pass — not Silero's '+' one.
    assert fake_ukrainian.calls[0]["voice"] == "tetiana"
    assert fake_ukrainian.calls[0]["stress"] == "dictionary"


def test_tts_ukrainian_strips_stress_markers_instead_of_applying_them(client, fake_ukrainian):
    client.post("/tts", json={"text": f"Сах+ар і робо{ACUTE}та.", "language": "uk"})
    assert fake_ukrainian.calls[0]["text"] == "Сахар і робота."


def test_tts_ukrainian_takes_the_speaker_field_as_its_voice(client, fake_ukrainian):
    client.post("/tts", json={"text": "Привіт.", "language": "uk", "speaker": "lada"})
    assert fake_ukrainian.calls[0]["voice"] == "lada"


def test_tts_ukrainian_rejects_any_other_language(client, fake_ukrainian):
    response = client.post("/tts", json={"text": "Привет.", "language": "ru"})

    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "the ukrainian engine speaks only 'uk', not 'ru'"
    assert body["supported"] == ["uk"]
    assert body["hint"] == "switch VOICE_LOOP_TTS_ENGINE=silero to serve 'ru'"
    assert fake_ukrainian.calls == []


def test_tts_ukrainian_refusal_stays_hintless_when_no_engine_speaks_the_language(client, fake_ukrainian):
    body = client.post("/tts", json={"text": "Сәлем.", "language": "kk"}).json()

    assert "kk" in body["error"] and "hint" not in body


# --- request-level refusals (the server still boots for /stt) -------------------------------------


@pytest.fixture
def ukrainian_engine_bare(monkeypatch):
    """The ukrainian engine selected with NO import seam installed — the caller plants the failure."""
    monkeypatch.setattr(voice_server, "TTS_ENGINE", "ukrainian")


def test_tts_ukrainian_without_the_package_is_a_clear_500(client, ukrainian_engine_bare, import_raises, caplog):
    """The one case that really IS a missing package: it says so, and hands over the install hint."""
    import_raises(
        "ukrainian_tts",
        ModuleNotFoundError(f"No module named 'ukrainian_tts' (searched {SECRET_PATH})", name="ukrainian_tts"),
    )

    with caplog.at_level("WARNING"):
        response = client.post("/tts", json={"text": "Привіт.", "language": "uk"})

    assert response.status_code == 500
    body = response.json()
    assert body["error"] == (
        "ukrainian import failed: ModuleNotFoundError for 'ukrainian_tts' — "
        "the optional ukrainian-tts package is not installed"
    )
    assert "pip install ukrainian-tts" in body["hint"]
    assert SECRET_PATH not in response.text  # the exception's own text never reaches the client
    assert SECRET_PATH in caplog.text  # it reaches the operator, where it belongs


def test_tts_ukrainian_with_a_broken_dependency_stack_names_the_class_not_the_message(
    client, ukrainian_engine_bare, import_raises, caplog
):
    """Installed but unimportable is NOT "not installed" — the wire gets the SHAPE, the log the text."""
    import_raises("ukrainian_tts", ImportError(f"cannot import name 'kolors' from 'torch'\n({SECRET_PATH})"))

    with caplog.at_level("WARNING"):
        response = client.post("/tts", json={"text": "Привіт.", "language": "uk"})

    assert response.status_code == 500
    body = response.json()
    assert body["error"] == "ukrainian import failed: ImportError — the message is in the server log"
    assert "not installed" not in body["error"]
    assert "pip install ukrainian-tts" not in body["hint"]
    assert SECRET_PATH not in response.text
    assert "ImportError: cannot import name 'kolors'" in caplog.text


def test_tts_ukrainian_with_a_missing_transitive_dependency_names_that_module(
    client, ukrainian_engine_bare, import_raises
):
    import_raises("ukrainian_tts", ModuleNotFoundError(f"No module named 'torch' ({SECRET_PATH})", name="torch"))

    response = client.post("/tts", json={"text": "Привіт.", "language": "uk"})

    assert response.status_code == 500
    body = response.json()
    assert body["error"] == (
        "ukrainian import failed: ModuleNotFoundError in dependency 'torch' — the message is in the server log"
    )
    assert "not installed" not in body["error"]
    assert SECRET_PATH not in response.text


def test_tts_ukrainian_import_failure_without_a_module_name_still_refuses_cleanly(
    client, ukrainian_engine_bare, import_raises
):
    import_raises("ukrainian_tts", ModuleNotFoundError(f"something went wrong in {SECRET_PATH}"))

    response = client.post("/tts", json={"text": "Привіт.", "language": "uk"})

    assert response.status_code == 500
    body = response.json()
    assert body["error"] == "ukrainian import failed: ModuleNotFoundError — the message is in the server log"
    assert "None" not in body["error"]
    assert SECRET_PATH not in response.text


def test_stt_works_with_the_ukrainian_engine_misconfigured(client, fake_whisper, monkeypatch, import_raises):
    monkeypatch.setattr(voice_server, "TTS_ENGINE", "ukrainian")
    import_raises("ukrainian_tts", ModuleNotFoundError("No module named 'ukrainian_tts'", name="ukrainian_tts"))

    response = client.post("/stt", files={"audio": ("clip.wav", b"RIFFfake", "audio/wav")})

    assert response.status_code == 200
    assert response.json()["text"] == "hello world"


# --- the lazy loader ------------------------------------------------------------------------------


def test_ukrainian_engine_is_loaded_once_and_pinned_to_cpu(ukrainian_installed):
    first = voice_server.ukrainian_tts()
    second = voice_server.ukrainian_tts()

    assert first is second
    assert len(ukrainian_installed.instances) == 1
    assert first.kwargs == {"device": "cpu"}  # like Silero: real-time there, the GPU stays free for STT
    assert voice_server.synthesis_device("ukrainian") == "cpu"


def test_reset_caches_drops_the_ukrainian_engine(ukrainian_installed):
    first = voice_server.ukrainian_tts()
    voice_server.reset_caches()
    assert voice_server.ukrainian_tts() is not first


# --- per-language routing -------------------------------------------------------------------------


@pytest.fixture
def uk_routed_to_ukrainian(monkeypatch, ukrainian_installed):
    """The brief's own shape: silero everywhere, VOICE_LOOP_TTS_ENGINE_UK=ukrainian on top."""
    model = FakeUkrainian()
    monkeypatch.setattr(voice_server, "ukrainian_tts", lambda: model)
    monkeypatch.setattr(voice_server, "TTS_ENGINE_BY_LANGUAGE", {"uk": "ukrainian"})
    return model


def test_a_per_language_override_routes_only_its_own_language(client, uk_routed_to_ukrainian, fake_silero):
    ukrainian = client.post("/tts", json={"text": "Привіт.", "language": "uk"})
    russian = client.post("/tts", json={"text": "Привет.", "language": "ru"})

    assert ukrainian.headers["x-voice-loop-engine"] == "ukrainian"
    assert russian.headers["x-voice-loop-engine"] == "silero"
    assert [call["text"] for call in uk_routed_to_ukrainian.calls] == ["Привіт."]
    assert [call["text"] for call in fake_silero.calls] == ["Привет."]


def test_per_language_overrides_are_read_from_the_environment(monkeypatch):
    """The variable name is the language code uppercased, '-' to '_' — and the value is case-folded."""
    monkeypatch.setenv("VOICE_LOOP_TTS_ENGINE_UK", "Ukrainian")
    monkeypatch.setenv("VOICE_LOOP_TTS_ENGINE_ZH_CN", "xtts")
    assert importlib.reload(voice_server).TTS_ENGINE_BY_LANGUAGE == {"uk": "ukrainian", "zh-cn": "xtts"}

    monkeypatch.delenv("VOICE_LOOP_TTS_ENGINE_UK")
    monkeypatch.delenv("VOICE_LOOP_TTS_ENGINE_ZH_CN")
    importlib.reload(voice_server)  # back to the shipped defaults for the rest of the suite


def test_an_unknown_per_language_override_degrades_to_the_fallback(
    client, monkeypatch, ukrainian_installed, fake_silero
):
    """A typo in the override is an engine-level failure, exactly like a bad VOICE_LOOP_TTS_ENGINE."""
    monkeypatch.setattr(voice_server, "TTS_ENGINE_BY_LANGUAGE", {"uk": "espeak"})
    monkeypatch.setattr(voice_server, "TTS_FALLBACK_ENGINE", "silero")

    response = client.post("/tts", json={"text": "Привіт.", "language": "uk"})

    assert response.status_code == 200
    assert response.headers["x-voice-loop-engine"] == "silero (fallback)"
    assert voice_server._tts_fallbacks == 1


def test_the_fallback_default_pairs_any_non_silero_primary_with_silero(monkeypatch):
    """The pairing follows the override too: a ukrainian voice for uk gets silero behind it."""
    monkeypatch.setenv("VOICE_LOOP_TTS_ENGINE", "ukrainian")
    assert importlib.reload(voice_server).TTS_FALLBACK_ENGINE == "silero"

    monkeypatch.delenv("VOICE_LOOP_TTS_ENGINE")
    monkeypatch.setenv("VOICE_LOOP_TTS_ENGINE_UK", "ukrainian")  # global silero, one override
    assert importlib.reload(voice_server).TTS_FALLBACK_ENGINE == "silero"

    monkeypatch.delenv("VOICE_LOOP_TTS_ENGINE_UK")
    importlib.reload(voice_server)  # back to the shipped defaults


# --- engine fallback --------------------------------------------------------------------------------


def test_tts_falls_back_to_silero_when_the_ukrainian_synthesis_raises(
    client, monkeypatch, ukrainian_engine, fake_silero, caplog
):
    model = BrokenUkrainian()
    monkeypatch.setattr(voice_server, "ukrainian_tts", lambda: model)
    monkeypatch.setattr(voice_server, "TTS_FALLBACK_ENGINE", "silero")

    with caplog.at_level("ERROR"):
        response = client.post("/tts", json={"text": "Привіт.", "language": "uk"})

    assert response.status_code == 200
    assert response.headers["x-voice-loop-engine"] == "silero (fallback)"
    _wav, sample_rate = sf.read(io.BytesIO(response.content))
    assert sample_rate == voice_server.TTS_SR  # the FALLBACK engine's rate, not 22050
    assert len(model.calls) == 1 and len(fake_silero.calls) == 1
    assert voice_server._tts_fallbacks == 1
    assert "retrying this request on silero" in caplog.text
    assert "RuntimeError: ukrainian-tts said no" in caplog.text


def test_a_routed_ukrainian_primary_falls_back_while_other_languages_stay_put(
    client, monkeypatch, uk_routed_to_ukrainian, fake_silero
):
    """The override and the fallback compose: uk degrades to silero, ru was never moved."""
    model = BrokenUkrainian()
    monkeypatch.setattr(voice_server, "ukrainian_tts", lambda: model)
    monkeypatch.setattr(voice_server, "TTS_FALLBACK_ENGINE", "silero")

    ukrainian = client.post("/tts", json={"text": "Привіт.", "language": "uk"})
    russian = client.post("/tts", json={"text": "Привет.", "language": "ru"})

    assert ukrainian.headers["x-voice-loop-engine"] == "silero (fallback)"
    assert russian.headers["x-voice-loop-engine"] == "silero"
    assert voice_server._tts_fallbacks == 1  # only the uk request was handed over


# --- /tts/stream ------------------------------------------------------------------------------------


def test_stream_ukrainian_chunks_carry_the_engine_sample_rate(client, fake_ukrainian):
    long_text = " ".join(f"Речення номер {i}." for i in range(120))  # several sentence chunks
    response = client.post("/tts/stream", json={"text": long_text, "language": "uk"})

    parsed = events(response.text)
    chunks, terminal = parsed[:-1], parsed[-1]
    assert len(chunks) > 1
    assert terminal == ("end", {"chunks": len(chunks), "engine": "ukrainian"})
    _wav, sample_rate = wav_of(chunks[0][1])
    assert sample_rate == voice_server.UKRAINIAN_SR


def test_an_engine_that_changed_its_output_rate_fails_loudly_not_flat(client, monkeypatch, ukrainian_engine):
    """The rate guard: a package that silently moved off 22050 Hz must not ship pitched audio."""

    class OffRateUkrainian(FakeUkrainian):
        def tts(self, text: str, voice: str, stress: str, file) -> tuple[None, str]:
            self.calls.append({"text": text, "voice": voice, "stress": stress})
            sf.write(file, [0.0] * 100, 8000, format="WAV")
            return None, text

    monkeypatch.setattr(voice_server, "ukrainian_tts", lambda: OffRateUkrainian())

    response = client.post("/tts/stream", json={"text": "Привіт.", "language": "uk"})

    assert events(response.text) == [("error", {"error": "synthesis failed (RuntimeError)", "chunks": 0})]


# --- /health ----------------------------------------------------------------------------------------


def test_health_reports_the_ukrainian_engine_state(client, monkeypatch, ukrainian_installed):
    body = client.get("/health").json()
    assert body["ukrainian_loaded"] is False
    assert body["tts_engine_overrides"] == {}

    voice_server.ukrainian_tts()
    monkeypatch.setattr(voice_server, "TTS_ENGINE_BY_LANGUAGE", {"uk": "ukrainian"})
    body = client.get("/health").json()
    assert body["ukrainian_loaded"] is True
    assert body["tts_engine_overrides"] == {"uk": "ukrainian"}
