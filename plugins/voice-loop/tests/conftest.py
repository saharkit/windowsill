"""Shared fixtures. No network, no models, no audio hardware — ever.

Everything expensive in the server is loaded through a seam: the recognizer comes from an importable
module (`faster_whisper`), the voices from `torch.hub.load`, the accentuators from importable
packages listed in `ACCENTUATORS`. Tests replace those seams with fakes, so the real code paths run
end to end against objects that answer instantly.
"""

from __future__ import annotations

import sys
import types

import pytest
from fastapi.testclient import TestClient

import voice_server


def fake_module(name: str, **attrs: object) -> types.ModuleType:
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


class FakeSegment:
    def __init__(self, text: str) -> None:
        self.text = text


class FakeInfo:
    def __init__(self, language: str = "ru", duration: float = 1.25) -> None:
        self.language = language
        self.duration = duration


class FakeWhisper:
    """Stands in for a loaded faster-whisper model and records how it was called."""

    def __init__(self, segments: tuple[str, ...] = (" hello ", " world ")) -> None:
        self.segments = segments
        self.calls: list[dict[str, object]] = []

    def transcribe(self, path: str, **kwargs: object) -> tuple[list[FakeSegment], FakeInfo]:
        self.calls.append({"path": path, **kwargs})
        return [FakeSegment(text) for text in self.segments], FakeInfo()


class FakeSilero:
    """Stands in for a loaded Silero voice: returns silence of a plausible length."""

    def __init__(self) -> None:
        self.device: object = None
        self.calls: list[dict[str, object]] = []

    def to(self, device: object) -> "FakeSilero":
        self.device = device
        return self

    def apply_tts(self, text: str, speaker: str, sample_rate: int):
        import torch

        self.calls.append({"text": text, "speaker": speaker, "sample_rate": sample_rate})
        return torch.zeros(int(sample_rate * 0.05))


class FakeXtts:
    """Stands in for a loaded coqui TTS wrapper: returns silence and records how it was called."""

    def __init__(self) -> None:
        self.device: object = None
        self.calls: list[dict[str, object]] = []

    def to(self, device: object) -> "FakeXtts":
        self.device = device
        return self

    def tts(self, text: str, speaker_wav: str, language: str) -> list[float]:
        self.calls.append({"text": text, "speaker_wav": speaker_wav, "language": language})
        return [0.0] * 1200  # the real model returns a plain list of floats at 24 kHz


@pytest.fixture(autouse=True)
def clean_state(monkeypatch, tmp_path):
    """Every test starts with empty caches, its own (absent) stress file, and accentuation OFF.

    Accentuation is off by default on purpose: if a real language package happened to be installed
    in the environment, loading it would reach for models over the network. Tests that want an
    accentuator ask for the `accent_enabled` fixture and supply a fake one.
    """
    voice_server.reset_caches()
    monkeypatch.setattr(voice_server, "STRESS_FILE", tmp_path / "stress.json")
    monkeypatch.setattr(voice_server, "HALLUCINATIONS_FILE", tmp_path / "stt_hallucinations.txt")
    monkeypatch.setattr(voice_server, "USE_ACCENT", False)
    monkeypatch.setattr(voice_server, "TTS_MODEL_OVERRIDE", "")
    monkeypatch.setattr(voice_server, "TTS_SPEAKER_OVERRIDE", "")
    monkeypatch.setattr(voice_server, "TTS_ENGINE", "silero")
    monkeypatch.setattr(voice_server, "TTS_FALLBACK_ENGINE", "none")  # opt-in per test, never ambient
    monkeypatch.setattr(voice_server, "XTTS_REFERENCE", "")
    monkeypatch.setattr(voice_server, "XTTS_MODEL_DIR", "")
    monkeypatch.setattr(voice_server, "LANGUAGE", "ru")
    yield
    voice_server.reset_caches()


@pytest.fixture
def accent_enabled(monkeypatch):
    """Turn automatic accentuation on — pair it with a fake entry in ACCENTUATORS."""
    monkeypatch.setattr(voice_server, "USE_ACCENT", True)


@pytest.fixture
def stress_file(tmp_path, monkeypatch):
    path = tmp_path / "stress.json"
    monkeypatch.setattr(voice_server, "STRESS_FILE", path)
    return path


@pytest.fixture
def hallucinations_file(tmp_path, monkeypatch):
    path = tmp_path / "stt_hallucinations.txt"
    monkeypatch.setattr(voice_server, "HALLUCINATIONS_FILE", path)
    return path


@pytest.fixture
def fake_whisper(monkeypatch):
    model = FakeWhisper()
    monkeypatch.setattr(voice_server, "whisper", lambda: model)
    return model


@pytest.fixture
def fake_silero(monkeypatch):
    model = FakeSilero()
    monkeypatch.setattr(voice_server, "tts", lambda language: model)
    return model


@pytest.fixture
def coqui_installed(import_fake):
    """A fake importable coqui-tts (`from TTS.api import TTS`), enough for the probe and the loader."""
    instances: list[FakeXtts] = []

    class FakeTTS(FakeXtts):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__()
            self.args, self.kwargs = args, kwargs
            instances.append(self)

    FakeTTS.instances = instances
    api = import_fake("TTS.api", TTS=FakeTTS)
    import_fake("TTS", api=api)
    return FakeTTS


@pytest.fixture
def xtts_engine(monkeypatch, tmp_path, coqui_installed):
    """The xtts engine selected with a reference wav on disk — the endpoint-level happy setup."""
    reference = tmp_path / "reference.wav"
    reference.write_bytes(b"RIFFfake")
    monkeypatch.setattr(voice_server, "TTS_ENGINE", "xtts")
    monkeypatch.setattr(voice_server, "XTTS_REFERENCE", str(reference))
    return reference


@pytest.fixture
def fake_xtts(monkeypatch, xtts_engine):
    model = FakeXtts()
    monkeypatch.setattr(voice_server, "xtts", lambda: model)
    return model


@pytest.fixture
def client():
    with TestClient(voice_server.app) as test_client:
        yield test_client


@pytest.fixture
def import_fake(monkeypatch):
    """Install a fake importable module for the duration of a test."""

    def install(name: str, **attrs: object) -> types.ModuleType:
        module = fake_module(name, **attrs)
        monkeypatch.setitem(sys.modules, name, module)
        return module

    return install
