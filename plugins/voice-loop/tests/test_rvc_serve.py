"""The bounds on /convert: decoded size, missing duration, queue, timeout, overflow race.

The RVC service lives at rvc/serve/rvc_server.py — operator tooling outside the plugin's runtime
coverage gates (server/ 100% and scripts/ 80%, neither of which covers rvc/). These tests sit at
the function tier because the decisions live there (`_convert_sync` validates and admits; the
chunk loop checks the deadline; `load_with_fallback` pre-builds the overflow) — standing up a
real FastAPI server to test validation logic would be a tier-2 cost for a tier-1 answer (L6).

Each test cites the gap it closes: a behaviour that would survive uncaught without it. Per L3,
the refusal decisions are authority surfaces — showing the service REFUSING is mandatory. We
show the refusal by constructing the offending condition and asserting the response.
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
import wave
from pathlib import Path

import pytest

# Set up the home-directory structure that rvc_server.py expects on import (APPLIO_DIR is chdir'd
# to at module load; SHIM_DIR is sys.path-inserted; the pedalboard shim is verified by realpath).
# Pointing the home env vars at a temp tree means the suite never touches the operator's real
# ~/voice/ — and BOTH vars are needed: posix's expanduser reads HOME, but ntpath's reads
# USERPROFILE first and never HOME, so setting only HOME left APPLIO_DIR pointing at the real
# profile on Windows and the import-time os.chdir aborted pytest at COLLECTION on a stock machine
# (no ~/voice tree exists there). rvc_server bakes the resolved paths in at import, so the vars
# are restored right after — the redirection stays local to this import and leaks nowhere.
_FAKE_HOME = Path(tempfile.mkdtemp(prefix="rvc-serve-tests-"))
for sub in ("voice/rvc/Applio", "voice/rvc/shims"):
    (_FAKE_HOME / sub).mkdir(parents=True, exist_ok=True)
(_FAKE_HOME / "voice/rvc/shims" / "pedalboard.py").write_text("# stub shim for tests\n")

_home_env = {name: os.environ.get(name) for name in ("HOME", "USERPROFILE")}
os.environ["HOME"] = str(_FAKE_HOME)
os.environ["USERPROFILE"] = str(_FAKE_HOME)

# `TMP_DIR` in rvc_server picks /dev/shm when writable, else tempfile.gettempdir() — that decision
# happens at module import, so let the module choose (no override needed for these tests).

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "rvc" / "serve"))
try:
    import rvc_server  # noqa: E402  — import-side-effects are load-bearing; see comment above
finally:
    for _name, _saved in _home_env.items():
        if _saved is None:
            os.environ.pop(_name, None)
        else:
            os.environ[_name] = _saved


# --- helpers ---------------------------------------------------------------------------------------


def _reset_inflight() -> None:
    """Every test starts with a clean admission counter and stats."""
    rvc_server._in_flight = 0
    rvc_server._stats["converted"] = 0
    rvc_server._stats["chunked"] = 0
    rvc_server._stats["oom_overflows"] = 0
    rvc_server._stats["errors"] = 0


def _real_wav_bytes(seconds: float = 0.01, channels: int = 1, rate: int = 16000) -> bytes:
    """A real, decodable PCM WAV — what `_convert_sync` writes through and `sf.info` reads."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x01\x00" * int(rate * seconds))
    return buf.getvalue()


def _info(frames, samplerate=16000, channels=1, duration=None):
    """A `sf.SoundFileInfo` substitute — `_convert_sync` reads frames, samplerate, channels, duration."""
    return type("Info", (), {
        "frames": frames,
        "samplerate": samplerate,
        "channels": channels,
        "format": "WAV",
        "subtype": "PCM_16",
        "duration": duration if duration is not None else (frames / samplerate if frames else 0.0),
    })()


def _dummy_audio(samples: int):
    """A mono float32 numpy array of zeros — what `sf.write` expects for the response."""
    import numpy as np
    return np.zeros(samples, dtype=np.float32)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    """Reset admission counter, stats, and the patches that the size/timeout tests need."""
    _reset_inflight()
    # Default: a small, parseable WAV — tests that don't want to exercise size validation get a
    # benign info object and proceed to the next gate.
    monkeypatch.setattr(
        rvc_server.sf, "info",
        lambda path: _info(frames=160, samplerate=16000, channels=1),
    )
    yield
    _reset_inflight()


# --- D1: decoded PCM product cap (finding 1) ------------------------------------------------------


def test_decoded_size_cap_rejects_a_high_rate_multichannel_file(monkeypatch):
    """GAP: a 600 s multichannel high-rate file decodes to multiple GiB. The previous duration cap
    (only on info.duration) accepted this — `_convert_sync` must reject on the product instead.

    Constructed: 100 M frames * 2 channels * 4 bytes (float32) = 800 MiB > 256 MiB cap.
    """
    monkeypatch.setattr(
        rvc_server.sf, "info",
        lambda path: _info(frames=100_000_000, samplerate=48000, channels=2),
    )
    monkeypatch.setattr(rvc_server, "_vc", object())  # admission must reach the size check

    response = rvc_server._convert_sync(b"RIFFfake", rvc_server._params())

    assert response.status_code == 413
    assert "decoded size" in response.body.decode().lower()
    assert "exceeds max" in response.body.decode().lower()


def test_decoded_size_cap_accepts_a_small_file(monkeypatch):
    """GAP (L3 two-way falsification): the refusal must not fire on inputs that fit — a regression
    that rejects everything would also pass the test above if the test only checked the refusal.
    """
    monkeypatch.setattr(
        rvc_server.sf, "info",
        lambda path: _info(frames=160, samplerate=16000, channels=1),
    )
    # Patch _convert_bounded to a stub so we don't need a real model.
    monkeypatch.setattr(rvc_server, "_vc", object())
    monkeypatch.setattr(
        rvc_server, "_convert_bounded",
        lambda vc, path, p, deadline=None: (_dummy_audio(320), 16000, 1),
    )

    response = rvc_server._convert_sync(b"RIFFfake", rvc_server._params())

    assert response.status_code == 200


# --- D2: missing duration header refusal (finding 2) ----------------------------------------------


def test_missing_duration_header_is_refused(monkeypatch):
    """GAP: `sf.info().frames` is None for some raw / streaming formats. Without a bound, the path
    flows into `sf.read` and `_convert_bounded` with no size visibility — the probe / broken-client
    case the previous code missed.

    The decision the test pins: `info.frames is None` MUST refuse, not silently pass through.
    """
    monkeypatch.setattr(
        rvc_server.sf, "info",
        lambda path: _info(frames=None, samplerate=16000, channels=1, duration=None),
    )
    monkeypatch.setattr(rvc_server, "_vc", object())

    response = rvc_server._convert_sync(b"RIFFfake", rvc_server._params())

    assert response.status_code == 413
    assert "cannot determine input length" in response.body.decode().lower()


# --- D3: queue admission bound (finding 3) --------------------------------------------------------


def test_a_full_pipeline_returns_503(monkeypatch):
    """GAP: with `_lock` serialising conversions, >1 in-flight means queueing. The previous code
    parked bodies indefinitely while they waited. The admission gate must refuse a second request
    rather than accept-and-park it.
    """
    monkeypatch.setattr(rvc_server, "_vc", object())
    # Simulate one in-flight conversion by directly setting the counter.
    rvc_server._in_flight = rvc_server.MAX_INFLIGHT

    response = rvc_server._convert_sync(b"RIFFfake", rvc_server._params())

    assert response.status_code == 503
    assert "queue full" in response.body.decode().lower()


def test_admission_does_not_reject_when_the_pipeline_is_free(monkeypatch):
    """GAP (L3 two-way falsification): the refusal must not fire when there is capacity. A
    regression that always-503s would pass the test above."""
    monkeypatch.setattr(rvc_server, "_vc", object())
    monkeypatch.setattr(
        rvc_server, "_convert_bounded",
        lambda vc, path, p, deadline=None: (_dummy_audio(320), 16000, 1),
    )

    response = rvc_server._convert_sync(b"RIFFfake", rvc_server._params())

    assert response.status_code == 200


# --- D4: conversion timeout (finding 3) -----------------------------------------------------------


def test_a_conversion_that_exceeds_the_deadline_returns_408(monkeypatch):
    """GAP: a 600 s input on the CPU lane holds `_lock` for ~55 minutes. The deadline check fires
    at chunk boundaries; `_convert_bounded` raises `_DeadlineExceeded` and `_convert_sync` must
    surface it as 408, not 500 (which would look like a bug) and not 200 (silent corruption).
    """
    monkeypatch.setattr(rvc_server, "_vc", object())

    def slow(vc, path, p, deadline=None):
        raise rvc_server._DeadlineExceeded("simulated deadline")

    monkeypatch.setattr(rvc_server, "_convert_bounded", slow)

    response = rvc_server._convert_sync(b"RIFFfake", rvc_server._params())

    assert response.status_code == 408
    assert "budget" in response.body.decode().lower()


def test_a_conversion_under_the_deadline_is_not_marked_a_timeout(monkeypatch):
    """GAP (L3 two-way falsification): a regression that always-408s would pass the test above."""
    monkeypatch.setattr(rvc_server, "_vc", object())
    monkeypatch.setattr(
        rvc_server, "_convert_bounded",
        lambda vc, path, p, deadline=None: (_dummy_audio(320), 16000, 1),
    )

    response = rvc_server._convert_sync(b"RIFFfake", rvc_server._params())

    assert response.status_code == 200


# --- D5: CPU overflow pre-built at startup (finding 4) --------------------------------------------


def test_cpu_overflow_is_pre_built_when_primary_lands_on_gpu(monkeypatch):
    """GAP: the prior lazy `_cpu_overflow()` raced two threads against the `Config.device`
    singleton (snapshot/restore interleaving corrupts cfg.device for the NEXT load). The fix is to
    pre-build the overflow at startup so the build runs exactly ONCE, before any conversion can
    hold `_lock`.

    This test pins the decision: load() runs twice at startup iff primary lands on GPU — once for
    the primary, once for the overflow. A regression that reverts to lazy-build would show zero
    overflow loads at startup.
    """
    calls: list[dict] = []

    class FakeConverter:
        def __init__(self, *args, **kwargs):
            pass

        def get_vc(self, path, sid):
            pass

        def load_hubert(self, name, custom):
            pass

        # Used by `_install_warm_caches`'s shim if anything reaches into the converter.
        last_embedder_model = "contentvec"

    def fake_load(device, restore=False):
        calls.append({"device": device, "restore": restore})
        return FakeConverter()

    monkeypatch.setattr(rvc_server, "load", fake_load)
    monkeypatch.setattr(rvc_server, "_install_warm_caches", lambda: None)
    monkeypatch.setattr(rvc_server, "pick_device", lambda: "cuda")
    monkeypatch.setattr(rvc_server, "warmup", lambda: None)

    rvc_server.load_with_fallback()

    # Two loads: primary (cuda, restore=False) and overflow (cpu, restore=True).
    assert calls == [{"device": "cuda", "restore": False}, {"device": "cpu", "restore": True}]


def test_cpu_overflow_is_not_pre_built_when_primary_lands_on_cpu(monkeypatch):
    """GAP (L3 two-way falsification): when the primary is already on CPU, the overflow path is
    unreachable (no CUDA to OOM), so pre-building it would burn 0.6 GiB of RAM for nothing.

    A regression that always pre-builds would burn the RAM but still pass the test above; this
    one pins the asymmetry.
    """
    calls: list[dict] = []

    class FakeConverter:
        def __init__(self, *args, **kwargs):
            pass

        def get_vc(self, path, sid):
            pass

        def load_hubert(self, name, custom):
            pass

        last_embedder_model = "contentvec"

    monkeypatch.setattr(
        rvc_server, "load",
        lambda device, restore=False: (calls.append({"device": device, "restore": restore}) or FakeConverter()),
    )
    monkeypatch.setattr(rvc_server, "_install_warm_caches", lambda: None)
    monkeypatch.setattr(rvc_server, "pick_device", lambda: "cpu")
    monkeypatch.setattr(rvc_server, "warmup", lambda: None)

    rvc_server.load_with_fallback()

    # One load only — the primary on CPU. No overflow.
    assert calls == [{"device": "cpu", "restore": False}]
