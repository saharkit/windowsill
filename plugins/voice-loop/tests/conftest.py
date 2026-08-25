"""Shared fixtures. No network, no models, no audio hardware — ever.

Everything expensive in the server is loaded through a seam: the recognizer comes from an importable
module (`faster_whisper`), the voices from `torch.hub.load`, the accentuators from importable
packages listed in `ACCENTUATORS`. Tests replace those seams with fakes, so the real code paths run
end to end against objects that answer instantly.
"""

from __future__ import annotations

import importlib
import io
import sys
import threading
import types
import wave

import pytest

try:
    from fastapi.testclient import TestClient
    import voice_server
except ImportError:
    TestClient = None  # type: ignore[assignment]
    voice_server = None  # type: ignore[assignment]


@pytest.fixture(autouse=True)
def _no_extra_dictate_spawns(request, monkeypatch):
    """For tests in this directory that exercise dictate.main, suppress the stream-worker and
    preview spawns the production flow now triggers (macOS CI otherwise counts two Popen calls
    per main() and the test fixtures' `assert len(spawned) == 1` fails). Pure no-op everywhere else.

    NB: test_dictate.py loads dictate.py via importlib.util.spec_from_file_location so the
    symbol the test sees is a separate module instance from voice_loop.scripts.dictate. Patch
    BOTH module objects so the mock takes effect either way.
    """
    if "test_dictate" not in str(request.fspath):
        return
    for modname in ("voice_loop.scripts.dictate", "dictate"):
        try:
            mod = importlib.import_module(modname)
        except ImportError:
            continue
        monkeypatch.setattr(mod, "start_stream_worker", lambda recorder_pid: None, raising=False)
        monkeypatch.setattr(mod, "_start_preview", lambda: None, raising=False)


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

    Only the unbounded acquire (`acquire()` with no timeout, what model_slot() does) raises; a probe
    that passes its own timeout gets an ordinary bool back, so a test can still ask "is it free?".
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
    """The model gates narrowed to ONE shared slot for a test, and impatient about it.

    ONE object standing in for BOTH device queues, deliberately: the property these tests pin is
    that a request releases its slot before it retries, and a fallback retry often crosses devices
    (a broken XTTS on the card handing over to Silero on the CPU). Two separate gates would let a
    request hold one of each and the regression would pass unnoticed; one shared slot keeps the
    question honest whichever queue the retry lands on.
    """
    gate = ImpatientGate()
    monkeypatch.setattr(voice_server, "_model_gates", {device: gate for device in voice_server.MODEL_DEVICES})
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


def pcm_wav(seconds: float = 0.05, rate: int = 16000, sample: bytes = b"\x01\x00") -> bytes:
    """A real, minimal PCM WAV — what the recolor stage's `RIFF` check and `sf.read` both accept."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(sample * int(rate * seconds))
    return buf.getvalue()


# The converter's answer, distinguishable from anything this server synthesizes: a different rate
# and a non-zero sample, so "did the recolored audio come back" is a byte comparison.
RECOLORED_WAV = pcm_wav()


class FakeResponse:
    """The context-managed, bounded-read object urllib hands back from `opener.open`."""

    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def read(self, amount: int | None = None) -> bytes:
        return self.body if amount is None else self.body[:amount]


class FakeConverter:
    """Stands in for the RVC recolor service, at the opener seam.

    Everything above it is the real thing — the Request is really built, really posted through this
    opener, and its answer really read back through the server's own size bound.
    """

    def __init__(self, answer: bytes = RECOLORED_WAV, error: BaseException | None = None) -> None:
        self.answer = answer
        self.error = error
        self.posts: list[dict[str, object]] = []

    def open(self, request: object, timeout: float | None = None) -> FakeResponse:
        self.posts.append(
            {
                "url": request.full_url,
                "method": request.get_method(),
                "content_type": request.get_header("Content-type"),
                "body": request.data,
                "timeout": timeout,
            }
        )
        if self.error is not None:
            raise self.error
        return FakeResponse(self.answer)


@pytest.fixture
def rvc_service(monkeypatch):
    """The recolor stage ON, pointed at a fake converter that answers with RECOLORED_WAV."""
    converter = FakeConverter()
    monkeypatch.setattr(voice_server, "RVC_URL", "http://127.0.0.1:7865/convert")
    monkeypatch.setattr(voice_server, "_default_opener", lambda: converter)
    return converter


@pytest.fixture
def corpus_dir(monkeypatch, tmp_path):
    """A training corpus configured at a directory of this test's own."""
    root = tmp_path / "corpus"
    monkeypatch.setattr(voice_server, "CORPUS_DIR", str(root))
    return root


@pytest.fixture(autouse=True)
def clean_state(monkeypatch, tmp_path):
    """Every test owns its XDG paths, starts with empty caches, its own (absent) stress file, and accentuation OFF.

    XDG-derived state and config paths must stay inside the test's temporary directory rather than
    reaching an operator's live voice-loop installation.

    Accentuation is off by default on purpose: if a real language package happened to be installed
    in the environment, loading it would reach for models over the network. Tests that want an
    accentuator ask for the `accent_enabled` fixture and supply a fake one.
    """
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    if voice_server is not None:
        voice_server.reset_caches()
        monkeypatch.setattr(voice_server, "STRESS_FILE", tmp_path / "stress.json")
        monkeypatch.setattr(voice_server, "HALLUCINATIONS_FILE", tmp_path / "stt_hallucinations.txt")
        monkeypatch.setattr(voice_server, "USE_ACCENT", False)
        monkeypatch.setattr(voice_server, "TTS_MODEL_OVERRIDE", "")
        monkeypatch.setattr(voice_server, "TTS_SPEAKER_OVERRIDE", "")
        monkeypatch.setattr(voice_server, "TTS_ENGINE", "silero")
        # Tests opt into language routing explicitly; production defaults route Turkish to XTTS.
        monkeypatch.setattr(voice_server, "TTS_ENGINE_BY_LANGUAGE", {})
        monkeypatch.setattr(voice_server, "TTS_FALLBACK_ENGINE", "none")  # opt-in per test, never ambient
        monkeypatch.setattr(voice_server, "XTTS_REFERENCE", "")
        monkeypatch.setattr(voice_server, "XTTS_MODEL_DIR", "")
        # The recolor stage and the corpus are OFF unless a test asks for them: the first would post to
        # whatever URL the machine running the suite happens to have in its environment, and the second
        # would write clips into it. Opt-in per test, never ambient — same rule as the fallback above.
        monkeypatch.setattr(voice_server, "RVC_URL", "")
        monkeypatch.setattr(voice_server, "CORPUS_DIR", "")
        monkeypatch.setattr(voice_server, "LANGUAGE", "ru")
    yield
    if voice_server is not None:
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
    if TestClient is None:
        pytest.skip("fastapi not installed — server tests unavailable")
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


def pytest_ignore_collect(collection_path, config):
    """When voice_server isn't importable, collect only the conformance tests.

    The shelf-wide verify gate warms a single shared venv with pytest + pytest-cov — it does
    not install per-plugin dependencies such as fastapi or torch.  Tests that need the real
    server are skipped here so that the conformance-only tests (which validate SKILL.md
    structure against the repo, with no server dependency at all) can still run and report.
    """
    if voice_server is not None:
        return False
    path_str = str(collection_path)
    if path_str.endswith(".py") and "test_conformance" not in path_str:
        return True
    return False


@pytest.fixture(autouse=True)
def short_sock_path(monkeypatch, request):
    """Patch speak._STREAM_HOLDER_SOCK to a path that fits macOS's 104-byte AF_UNIX sun_path
    limit. Pytest's tmp_path on macOS is /private/var/folders/... which is too long; the fix is
    a /tmp/ subdir for darwin runners. autouse=True so every test in this directory gets the
    short path without explicitly requesting the fixture.
    """
    if "test_speak_stream" not in str(request.fspath) and "test_speak" not in str(request.fspath):
        return
    import sys
    try:
        import voice_loop.scripts.speak as speak  # noqa: PLC0415
    except ImportError:
        return
    path = "/tmp/vl-test/h.sock"  # 14 chars, well under darwin's 104-byte sun_path limit
    monkeypatch.setattr(speak, "_STREAM_HOLDER_SOCK", path)
    return
