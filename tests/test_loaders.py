"""The lazy loaders, exercised for real against fake packages.

These are the seams: `whisper()` imports `faster_whisper`, `tts()` calls `torch.hub.load`, the
accentuators import their language packages. Each is patchable, so the real function bodies run —
no model is downloaded, nothing touches the network.
"""

from __future__ import annotations

import sys

import pytest

import voice_server
from conftest import FakeSilero

ACUTE = "́"


class FakeWhisperModel:
    instances: list["FakeWhisperModel"] = []

    def __init__(self, name: str, device: str, compute_type: str) -> None:
        self.name, self.device, self.compute_type = name, device, compute_type
        FakeWhisperModel.instances.append(self)


def test_whisper_is_built_once_with_the_resolved_device(monkeypatch, import_fake):
    FakeWhisperModel.instances.clear()
    import_fake("faster_whisper", WhisperModel=FakeWhisperModel)
    monkeypatch.setattr(voice_server, "STT_MODEL", "tiny")
    monkeypatch.setattr(voice_server, "DEVICE", "cpu")
    monkeypatch.setattr(voice_server, "COMPUTE_TYPE", "auto")

    first = voice_server.whisper()
    second = voice_server.whisper()

    assert first is second
    assert len(FakeWhisperModel.instances) == 1
    assert (first.name, first.device, first.compute_type) == ("tiny", "cpu", "int8")


def test_ru_accentuator_loads_and_marks(import_fake):
    class FakeRUAccent:
        def __init__(self) -> None:
            self.options: dict[str, object] = {}

        def load(self, **options: object) -> None:
            self.options = options

        def process_all(self, text: str) -> str:
            return text.replace("тест", "т+ест")

    import_fake("ruaccent", RUAccent=FakeRUAccent)
    process = voice_server._load_ru_accentuator()
    assert process("это тест") == "это т+ест"


def test_uk_accentuator_normalizes_the_acute_it_emits(import_fake):
    class FakeStressifier:
        def __call__(self, text: str) -> str:
            return text.replace("робота", f"робо{ACUTE}та")

    import_fake("ukrainian_word_stress", Stressifier=FakeStressifier)
    process = voice_server._load_uk_accentuator()
    assert process("робота") == "роб+ота"


def test_accentuator_registry_wires_the_real_loaders():
    assert voice_server.ACCENTUATORS["ru"] is voice_server._load_ru_accentuator
    assert voice_server.ACCENTUATORS["uk"] is voice_server._load_uk_accentuator


def test_missing_language_package_degrades_without_raising(monkeypatch, caplog, accent_enabled):
    monkeypatch.setitem(sys.modules, "ruaccent", None)  # importing None raises ImportError
    with caplog.at_level("WARNING"):
        assert voice_server.accentuator("ru") is None
    assert "accentuation unavailable for ru" in caplog.text


# --- silero voices --------------------------------------------------------------------------------


@pytest.fixture
def hub(monkeypatch):
    calls: list[dict[str, object]] = []

    def fake_load(repo: str, entry: str, **kwargs: object):
        calls.append({"repo": repo, "entry": entry, **kwargs})
        return FakeSilero(), "example"

    monkeypatch.setattr(voice_server.torch.hub, "load", fake_load)
    return calls


def test_voice_is_loaded_from_the_language_table_and_cached(hub):
    first = voice_server.tts("ru")
    again = voice_server.tts("ru")

    assert first is again
    assert len(hub) == 1
    assert (hub[0]["language"], hub[0]["speaker"]) == ("ru", "v4_ru")
    assert first.device is not None  # moved to CPU on purpose: the GPU stays free for recognition


def test_ukrainian_uses_the_upstream_language_key(hub):
    voice_server.tts("uk")
    assert (hub[0]["language"], hub[0]["speaker"]) == ("ua", "v4_ua")


def test_english_is_a_first_class_language(hub):
    voice_server.tts("en")
    assert (hub[0]["language"], hub[0]["speaker"]) == ("en", "v3_en")
    assert voice_server.default_speaker("en") == "en_0"


def test_model_override_applies_only_to_the_default_language(hub, monkeypatch):
    monkeypatch.setattr(voice_server, "LANGUAGE", "ru")
    monkeypatch.setattr(voice_server, "TTS_MODEL_OVERRIDE", "v3_1_ru")

    voice_server.tts("ru")
    voice_server.tts("en")

    assert hub[0]["speaker"] == "v3_1_ru"
    assert hub[1]["speaker"] == "v3_en"


def test_speaker_override_applies_only_to_the_default_language(monkeypatch):
    monkeypatch.setattr(voice_server, "LANGUAGE", "ru")
    monkeypatch.setattr(voice_server, "TTS_SPEAKER_OVERRIDE", "xenia")
    assert voice_server.default_speaker("ru") == "xenia"
    assert voice_server.default_speaker("uk") == "mykyta"


# --- process entry points -------------------------------------------------------------------------


def test_require_python_accepts_the_running_interpreter():
    assert voice_server.require_python() is None


def test_require_python_rejects_an_old_interpreter():
    with pytest.raises(SystemExit) as exc:
        voice_server.require_python((3, 9))
    assert "3.10 or newer" in str(exc.value)
    assert "3.9" in str(exc.value)


def test_main_starts_uvicorn_on_the_configured_socket(monkeypatch):
    started: list[dict[str, object]] = []
    monkeypatch.setattr(voice_server, "HOST", "127.0.0.1")
    monkeypatch.setattr(voice_server, "PORT", 8355)
    monkeypatch.setattr(voice_server.uvicorn, "run", lambda app, host, port: started.append({"host": host, "port": port, "app": app}))

    voice_server.main()

    assert started == [{"host": "127.0.0.1", "port": 8355, "app": voice_server.app}]
