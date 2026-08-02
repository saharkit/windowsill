"""Shared fixtures. No network, no models, no audio hardware — ever.

Everything expensive in the server is loaded through a seam: the recognizer comes from an importable
module (`faster_whisper`), the voices from `torch.hub.load`, the accentuators from importable
packages listed in `ACCENTUATORS`. Tests replace those seams with fakes, so the real code paths run
end to end against objects that answer instantly.
"""

from __future__ import annotations

import sys
import threading
import types

import pytest
from fastapi.testclient import TestClient

import voice_server


class GateHeldTwice(RuntimeError):
    """A second model slot was asked for while the first was still held — the regression, named."""


class ImpatientGate:
    """A capacity-1 model gate that refuses to wait forever.

    `threading.BoundedSemaphore(1)` is the honest production shape, and it is exactly why the
    property "the failed primary releases its slot before the fallback takes one" is awkward to
    test: a regression does not FAIL the assertion below, it blocks on the second acquire and hangs
    the run. This stands in for the gate in those tests — identical semantics right up to the point
    where the real gate would block for good, where this one raises GateHeldTwice instead, which
    surfaces as an ordinary named failure through the endpoint under test.

    Only the unbounded acquire (`with _model_gate:`, what the server does) raises; a probe that
    passes its own timeout gets an ordinary bool back, so a test can still ask "is it free?".
    """

    def __init__(self, patience: float = 2.0) -> None:
        self._semaphore = threading.BoundedSemaphore(1)
        self._patience = patience

    def acquire(self, blocking: bool = True, timeout: float | None = None) -> bool:
        if not blocking:
            return self._semaphore.acquire(blocking=False)
        if timeout is not None:
            return self._semaphore.acquire(timeout=timeout)
        if not self._semaphore.acquire(timeout=self._patience):
            raise GateHeldTwice(
                f"the one model slot was still held after {self._patience}s — a single request "
                "acquired the gate twice instead of releasing before it retried"
            )
        return True

    def release(self) -> None:
        self._semaphore.release()

    def __enter__(self) -> "ImpatientGate":
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


@pytest.fixture
def one_slot_gate(monkeypatch) -> ImpatientGate:
    """The model gate narrowed to ONE slot for the duration of a test, and impatient about it."""
    gate = ImpatientGate()
    monkeypatch.setattr(voice_server, "_model_gate", gate)
    return gate


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


class RaisingFinder:
    """A meta-path finder that fails one package's import with a chosen exception.

    `sys.modules[name] = None` can raise exactly one flavour of ImportError, and the server's refusal
    now DISTINGUISHES flavours: a package that is genuinely absent (`ModuleNotFoundError` naming the
    top-level package) reads differently from one that is installed and cannot import because its own
    dependency stack is broken. Testing that difference needs a seam that raises the real thing.
    """

    def __init__(self, name: str, error: BaseException) -> None:
        self.name = name
        self.error = error

    def find_spec(self, fullname: str, path: object = None, target: object = None) -> None:
        if fullname == self.name:
            raise self.error
        return None


@pytest.fixture
def import_raises(monkeypatch):
    """Make importing `name` (and anything under it) fail with `error` for the duration of a test."""

    def install(name: str, error: BaseException) -> RaisingFinder:
        for loaded in [module for module in sys.modules if module == name or module.startswith(f"{name}.")]:
            monkeypatch.delitem(sys.modules, loaded)
        finder = RaisingFinder(name, error)
        monkeypatch.setattr(sys, "meta_path", [finder, *sys.meta_path])
        return finder

    return install
