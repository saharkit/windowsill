"""Night Bakery RVC voice conversion (<gpu-host>): "scheherazade" on 127.0.0.1:8358.

ADDITIVE to the live Silero/whisper server on :8355 and the XTTS server on :8356 — separate venv
(the RVC *training* venv, reused), separate port, separate model. It mirrors the :8356 house style
(bytes in, audio/wav out; /health with the same key shape) so a client hook can switch endpoints
without reshaping the request.

What it is FOR: turning the 14.6 s cold `core.py infer` CLI call into a warm HTTP call. Weights,
faiss index, hubert embedder and the rmvpe f0 predictor are loaded ONCE at startup and reused; a
request pays only the conversion itself (measured ~0.36 s for 5.6 s of audio, RTF 0.064).

Loopback-only by deployment, like :8356. Reach it from the brain over an ssh tunnel, not the LAN.

SEAM: Applio is used as a LIBRARY — `rvc.infer.infer.VoiceConverter.convert_audio`, the same callable
`core.py infer` drives. No subprocess per request. Four things make that seam safe here, all done
below and all load-bearing:

  1. the pedalboard AVX2 shim goes on sys.path FIRST (RUNBOOK §8 — without it the import SIGILLs on
     this Sandy-Bridge CPU: "Illegal instruction", rc=132, no traceback);
  2. cwd is Applio/ — `infer.py`, `utils.py` and `configs/config.py` all read `os.getcwd()` or
     relative paths at import time;
  3. two process-local caches are patched into `rvc.infer.pipeline` (_install_warm_caches), because
     upstream re-reads the 318 MB faiss index and re-builds the rmvpe predictor on EVERY pipeline()
     call — fine for a one-shot CLI, absurd for a resident service;
  4. input audio is CHUNKED to CHUNK_SECONDS before conversion. Peak VRAM is linear in input
     duration and this 6 GiB card is shared with two incumbents — see the measured curve at
     CHUNK_SECONDS below. Without the chunker a 17 s clip OOMs the GPU.

Nothing in site-packages or in the Applio tree is modified. Remove this directory to revert.

License: the model carries the CORPUS's license (ElevenLabs-generated audio), not Applio's MIT —
RUNBOOK §12. LAN / operator-personal use only.

Runbook: ~/voice/rvc-serve/RUNBOOK-serve.md
"""

import io
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import asynccontextmanager

# ---------------------------------------------------------------------------
# config block — the only knobs
# ---------------------------------------------------------------------------
HOME = os.path.expanduser("~")
APPLIO_DIR = f"{HOME}/voice/rvc/Applio"
SHIM_DIR = f"{HOME}/voice/rvc/shims"
EXP_DIR = f"{APPLIO_DIR}/logs/scheherazade"

MODEL_ID = "scheherazade_70e"
PTH_PATH = f"{EXP_DIR}/scheherazade_70e_32270s.pth"
INDEX_PATH = f"{EXP_DIR}/scheherazade.index"

PORT = 8358
HOST = "127.0.0.1"

# THE ADMISSION GATE. Measured requirement for the GPU fast path, resident models + a 6 s chunk's
# working set: it RAN with 1705 MiB free and OOM'd with 1570 MiB free. 1700 is that measured line,
# not a guess. Below it the process never touches CUDA at all (CUDA_VISIBLE_DEVICES is emptied
# BEFORE torch is imported), so a CPU-mode server holds ZERO VRAM instead of squatting ~1.15 GiB it
# cannot use — which is 1.15 GiB handed back to the two incumbents that own this card.
MIN_FREE_VRAM_MIB = 1700

# Ops override: RVC_FORCE_DEVICE=cpu|cuda skips the gate. `cuda` is a deliberate "I know the card is
# tight, measure it anyway" lever; it can OOM into the CPU overflow lane, never into the incumbents.
FORCE_DEVICE = os.environ.get("RVC_FORCE_DEVICE", "").strip().lower()

# THE SATURATION CONSTANT. Peak VRAM grows ~linearly with input duration; measured on this box with
# both incumbents resident (:8355 1824 MiB, :8356 2206-2750 MiB — xtts's own footprint varies):
#
#     input   process peak   min free on the card   outcome
#      5.6 s      1008 MiB           697 MiB        ok, rtf 0.064
#     11.2 s     ~1487 MiB            83 MiB        ok but the card was effectively full
#     16.8 s          —                9 MiB        CUDA OOM
#     61.6 s          —                —            CUDA OOM
#
# 6 s keeps the peak near the measured-safe point and leaves ~500 MiB free even when xtts is at its
# 2750 MiB high-water mark. Longer inputs are split at their quietest points and re-joined, so the
# service accepts any duration without ever approaching that wall. Raise this ONLY with a fresh
# measurement of the same table — it is a headroom constant, not a quality knob.
CHUNK_SECONDS = 6.0
CHUNK_SEARCH_SECONDS = 1.5  # how far back from a chunk edge to hunt for a quiet cut point
CHUNK_FADE_MS = 5.0  # click-suppression fade at each join
MAX_INPUT_SECONDS = 600.0  # absolute sanity bound; a longer body is a client bug, not a request

# Conversion defaults — RUNBOOK §8 verbatim. Per-request query params override them.
DEFAULT_PITCH = 0
DEFAULT_INDEX_RATE = 0.75
DEFAULT_PROTECT = 0.33
DEFAULT_VOLUME_ENVELOPE = 1.0
DEFAULT_F0_METHOD = "rmvpe"
EMBEDDER = "contentvec"  # MUST match training — do not change (RUNBOOK §8)

MAX_UPLOAD_BYTES = 128 * 1024 * 1024
PITCH_LIMIT = 24

# Release the torch caching allocator's unused blocks after every request. VRAM, not latency, is the
# scarce resource on this card; measured latency cost is within noise (0.354 s vs 0.356 s).
RELEASE_CACHE_AFTER_REQUEST = True

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sahar-rvc")

# ---------------------------------------------------------------------------
# import order is load-bearing — see the module docstring
# ---------------------------------------------------------------------------
if SHIM_DIR not in sys.path:
    sys.path.insert(0, SHIM_DIR)  # (1) pedalboard shim ahead of site-packages
if APPLIO_DIR not in sys.path:
    sys.path.insert(1, APPLIO_DIR)
os.chdir(APPLIO_DIR)  # (2) Applio reads os.getcwd() at import time


def _card_free_mib() -> int | None:
    """Free VRAM from nvidia-smi — read BEFORE importing torch, which would create a CUDA context."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15, check=True,
        )
        return int(out.stdout.strip().splitlines()[0])
    except Exception as exc:
        log.warning("nvidia-smi probe failed (%s) — assuming no usable GPU", exc)
        return None


_CARD_FREE_MIB = _card_free_mib()
_GATE_REASON = ""
if FORCE_DEVICE == "cuda":
    _GATE_REASON = "RVC_FORCE_DEVICE=cuda — admission gate bypassed"
elif FORCE_DEVICE == "cpu":
    _GATE_REASON = "RVC_FORCE_DEVICE=cpu"
    os.environ["CUDA_VISIBLE_DEVICES"] = ""
elif _CARD_FREE_MIB is None or _CARD_FREE_MIB < MIN_FREE_VRAM_MIB:
    _GATE_REASON = f"card free {_CARD_FREE_MIB}MiB < {MIN_FREE_VRAM_MIB}MiB required -> CPU, holding no VRAM"
    os.environ["CUDA_VISIBLE_DEVICES"] = ""
else:
    _GATE_REASON = f"card free {_CARD_FREE_MIB}MiB >= {MIN_FREE_VRAM_MIB}MiB required -> GPU"
log.info("admission gate: %s", _GATE_REASON)

import pedalboard  # noqa: E402  — deliberate fail-fast: SIGILL here, not mid-request

if os.path.realpath(os.path.dirname(pedalboard.__file__ or "")) != os.path.realpath(SHIM_DIR):
    log.warning("pedalboard is NOT the shim (%s) — expect SIGILL on this CPU", pedalboard.__file__)
else:
    log.info("pedalboard shim active: %s", pedalboard.__file__)

import numpy as np  # noqa: E402
import soundfile as sf  # noqa: E402
import torch  # noqa: E402
import uvicorn  # noqa: E402
from fastapi import FastAPI, Query, Request  # noqa: E402
from fastapi.responses import JSONResponse, Response  # noqa: E402

# starlette's UploadFile, NOT fastapi's: fastapi.UploadFile is a SUBCLASS, and the object a parsed
# multipart form yields is the starlette parent — so `isinstance(part, fastapi.UploadFile)` is False
# and every multipart upload 400s. Found by the smoke, invisible when reading the code.
from starlette.datastructures import UploadFile  # noqa: E402

TMP_DIR = "/dev/shm" if os.access("/dev/shm", os.W_OK) else tempfile.gettempdir()

_vc = None  # primary VoiceConverter (cuda when the card allows)
_vc_cpu = None  # lazy CPU overflow converter — built only if the primary ever OOMs
_device = "unloaded"
_lock = threading.Lock()  # the pipeline is not re-entrant: one conversion at a time
_in_flight = 0
_in_flight_lock = threading.Lock()
_started = time.monotonic()
_stats = {"converted": 0, "chunked": 0, "oom_overflows": 0, "errors": 0}


# ---------------------------------------------------------------------------
# warm caches — the difference between "resident" and "merely long-running"
# ---------------------------------------------------------------------------
class _CachedIndex:
    """Wraps a faiss index so reconstruct_n() is paid once, not per request.

    Upstream pipeline() does `index.reconstruct_n(0, index.ntotal)` on every call, which
    materializes the whole (105516, 768) float32 feature matrix — ~309 MiB — each time. Everything
    else (.search, .ntotal) delegates to the real index.
    """

    def __init__(self, real):
        self._real = real
        self._big = None

    def reconstruct_n(self, i0, n):
        if self._big is None:
            self._big = self._real.reconstruct_n(i0, n)
            log.info("faiss big_npy materialized once: %s %s", self._big.shape, self._big.dtype)
        return self._big

    def __getattr__(self, name):
        return getattr(self._real, name)


class _FaissShim:
    """Stands in for the `faiss` module inside rvc.infer.pipeline only — read_index() memoized."""

    def __init__(self, real_faiss):
        self._real = real_faiss
        self._cache: dict[str, _CachedIndex] = {}

    def read_index(self, path):
        key = os.path.realpath(path)
        if key not in self._cache:
            t0 = time.monotonic()
            self._cache[key] = _CachedIndex(self._real.read_index(path))
            log.info("faiss index loaded once in %.2fs: %s", time.monotonic() - t0, path)
        return self._cache[key]

    def __getattr__(self, name):
        return getattr(self._real, name)


def _install_warm_caches():
    """Patch per-request model construction out of rvc.infer.pipeline. Process-local, no file edits."""
    import faiss

    import rvc.infer.pipeline as pipe

    pipe.faiss = _FaissShim(faiss)

    real_rmvpe = pipe.RMVPE
    cache: dict[tuple, object] = {}

    def cached_rmvpe(device, sample_rate=16000, hop_size=160, **kw):
        key = (device, sample_rate, hop_size)
        if key not in cache:
            t0 = time.monotonic()
            cache[key] = real_rmvpe(device=device, sample_rate=sample_rate, hop_size=hop_size, **kw)
            log.info("rmvpe predictor loaded once in %.2fs (device=%s)", time.monotonic() - t0, device)
        return cache[key]

    pipe.RMVPE = cached_rmvpe
    log.info("warm caches installed (faiss.read_index, RMVPE)")


# ---------------------------------------------------------------------------
# device + load
# ---------------------------------------------------------------------------
def pick_device() -> str:
    """cuda only if the admission gate let CUDA through and torch actually sees a card.

    The real decision was already made above, before torch existed — this only reports it.
    """
    if not torch.cuda.is_available():
        log.warning("cuda unavailable (%s) -> cpu", _GATE_REASON)
        return "cpu"
    try:
        free, total = torch.cuda.mem_get_info()
        log.info("vram free=%.0fMiB total=%.0fMiB", free / 2**20, total / 2**20)
    except Exception as exc:
        log.warning("mem_get_info failed (%s) -> cpu", exc)
        return "cpu"
    return "cuda"


def load(device: str, restore: bool = False):
    """Load weights + hubert on `device`. Raises on failure; the caller owns the fallback.

    NOTE: rvc.configs.config.Config is a @singleton, so `.device` is process-GLOBAL. A converter
    captures it at load time only (net_g/hubert `.to()`, and Pipeline.__init__ copies it), so a
    loaded converter is immune to later flips. `restore=True` is used when building the CPU overflow
    copy, so the singleton keeps pointing at the PRIMARY's device afterwards.
    """
    from rvc.configs.config import Config

    from rvc.infer.infer import VoiceConverter

    cfg = Config()
    previous = cfg.device
    cfg.device = "cuda:0" if device == "cuda" else "cpu"
    try:
        t0 = time.monotonic()
        vc = VoiceConverter()
        vc.get_vc(PTH_PATH, 0)
        if vc.vc is None:
            raise RuntimeError(f"weights did not load: {PTH_PATH}")
        vc.load_hubert(EMBEDDER, None)
        vc.last_embedder_model = EMBEDDER
        log.info(
            "model loaded on %s in %.1fs (tgt_sr=%s version=%s vocoder=%s)",
            device, time.monotonic() - t0, vc.tgt_sr, vc.version, getattr(vc, "vocoder", "?"),
        )
        return vc
    finally:
        if restore:
            cfg.device = previous


def _cpu_overflow() -> object:
    """Get (building on first need) the CPU converter used when the GPU cannot take a request.

    A SECOND resident copy, deliberately: the primary cuda converter is never downgraded, so one
    oversized request can no longer leave the whole service 27x slower until someone restarts it.
    Costs ~0.6 GiB of host RAM (31 GiB on this node), and only after the first overflow.
    """
    global _vc_cpu
    if _vc_cpu is None:
        log.warning("building CPU overflow converter (first overflow)")
        _vc_cpu = load("cpu", restore=True)
    return _vc_cpu


def warmup():
    """One real conversion of synthetic audio: fills the faiss/rmvpe caches and the cuda kernels.

    Without it the FIRST client request pays the index + predictor load, which is exactly the cold
    cost this service exists to remove.
    """
    sr = 16000
    t = np.linspace(0, 1.5, int(sr * 1.5), endpoint=False, dtype=np.float32)
    tone = 0.2 * np.sin(2 * np.pi * 190.0 * t) + 0.02 * np.random.default_rng(0).standard_normal(t.shape)
    path = os.path.join(TMP_DIR, "rvc-serve-warmup.wav")
    sf.write(path, tone.astype(np.float32), sr, format="WAV", subtype="PCM_16")
    try:
        t0 = time.monotonic()
        _convert_bounded(_vc, path, _params())
        log.info("warmup conversion done in %.2fs", time.monotonic() - t0)
    finally:
        for p in (path, path.replace(".wav", ".out.wav")):
            try:
                os.remove(p)
            except OSError:
                pass


def load_with_fallback():
    """Try the chosen device, fall back to cpu on any failure — serving slowly beats not serving."""
    global _vc, _device
    _install_warm_caches()
    device = pick_device()
    try:
        _vc = load(device)
        _device = device
    except Exception as exc:
        log.error("model load on %s failed (%s) -> retrying on cpu", device, exc, exc_info=True)
        _release_cuda()
        _vc = load("cpu")
        _device = "cpu"
    try:
        warmup()
    except Exception as exc:
        log.error("warmup failed (%s) — serving anyway, first request pays the cold cost", exc, exc_info=True)


def _release_cuda():
    global _vc
    _vc = None
    try:
        torch.cuda.empty_cache()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# conversion
# ---------------------------------------------------------------------------
def _params(pitch: int = DEFAULT_PITCH, index_rate: float = DEFAULT_INDEX_RATE,
            protect: float = DEFAULT_PROTECT, volume_envelope: float = DEFAULT_VOLUME_ENVELOPE,
            f0_method: str = DEFAULT_F0_METHOD) -> dict:
    return {
        "pitch": pitch,
        "index_rate": index_rate,
        "protect": protect,
        "volume_envelope": volume_envelope,
        "f0_method": f0_method,
    }


def _convert_one(vc, in_path: str, out_path: str, p: dict) -> None:
    """One raw Applio conversion. Caller holds _lock and guarantees the input fits CHUNK_SECONDS."""
    vc.convert_audio(
        audio_input_path=in_path,
        audio_output_path=out_path,
        model_path=PTH_PATH,
        index_path=INDEX_PATH,
        pitch=p["pitch"],
        f0_method=p["f0_method"],
        index_rate=p["index_rate"],
        volume_envelope=p["volume_envelope"],
        protect=p["protect"],
        split_audio=False,
        f0_autotune=False,
        embedder_model=EMBEDDER,
        embedder_model_custom=None,
        clean_audio=False,
        export_format="WAV",
        post_process=False,
        resample_sr=0,
        sid=0,
    )


def _cut_points(x: np.ndarray, sr: int, max_s: float) -> list[tuple[int, int]]:
    """Split indices, each piece <= max_s, cut at the QUIETEST 20 ms frame near each boundary.

    Cutting in a low-energy region means the join lands in a pause, where RVC's own padded windows
    make the seam inaudible — as opposed to a fixed-stride cut through a vowel.
    """
    n = len(x)
    max_n = int(max_s * sr)
    if n <= max_n:
        return [(0, n)]
    win = max(int(0.02 * sr), 1)
    step = max(win // 2, 1)
    pieces, pos = [], 0
    while n - pos > max_n:
        target = pos + max_n
        lo = max(pos + max_n // 4, target - int(CHUNK_SEARCH_SECONDS * sr))
        best, best_e = target, None
        for c in range(lo, max(target - win, lo + 1), step):
            e = float(np.mean(np.square(x[c:c + win])))
            if best_e is None or e < best_e:
                best_e, best = e, c + win // 2
        pieces.append((pos, best))
        pos = best
    pieces.append((pos, n))
    return pieces


def _fade_join(pieces: list[np.ndarray], sr: int) -> np.ndarray:
    """Concatenate converted pieces with a short fade at each seam (click suppression)."""
    if len(pieces) == 1:
        return pieces[0]
    fade_n = max(int(CHUNK_FADE_MS / 1000.0 * sr), 1)
    out = []
    for i, piece in enumerate(pieces):
        piece = np.asarray(piece, dtype=np.float32).copy()
        if len(piece) > 2 * fade_n:
            if i > 0:
                piece[:fade_n] *= np.linspace(0.0, 1.0, fade_n, dtype=np.float32)
            if i < len(pieces) - 1:
                piece[-fade_n:] *= np.linspace(1.0, 0.0, fade_n, dtype=np.float32)
        out.append(piece)
    return np.concatenate(out)


def _convert_bounded(vc, in_path: str, p: dict) -> tuple[np.ndarray, int, int]:
    """Convert a whole file, chunked so no single conversion exceeds CHUNK_SECONDS.

    Returns (audio, sample_rate, n_chunks). Holds _lock for the duration: the pipeline, the cuda
    context and the Config singleton are all shared, so conversions are strictly serialized.
    """
    x, sr = sf.read(in_path, dtype="float32", always_2d=False)
    if x.ndim > 1:
        x = x.mean(axis=1)
    cuts = _cut_points(x, sr, CHUNK_SECONDS)

    with _lock:
        outputs, out_sr = [], None
        tmp_paths = []
        try:
            for idx, (a, b) in enumerate(cuts):
                if len(cuts) == 1:
                    piece_in = in_path
                else:
                    piece_in = os.path.join(TMP_DIR, f"rvc-chunk-{os.getpid()}-{idx}.wav")
                    sf.write(piece_in, x[a:b], sr, format="WAV", subtype="PCM_16")
                    tmp_paths.append(piece_in)
                piece_out = piece_in.replace(".wav", ".out.wav")
                tmp_paths.append(piece_out)
                _convert_one(vc, piece_in, piece_out, p)
                y, out_sr = sf.read(piece_out, dtype="float32", always_2d=False)
                if y.ndim > 1:
                    y = y.mean(axis=1)
                outputs.append(y)
        finally:
            for path in tmp_paths:
                try:
                    os.remove(path)
                except OSError:
                    pass
            if RELEASE_CACHE_AFTER_REQUEST and torch.cuda.is_available():
                try:
                    torch.cuda.empty_cache()  # give the pool back; the incumbents share this card
                except Exception:
                    pass
    return _fade_join(outputs, out_sr or sr), out_sr or sr, len(cuts)


def sample_rate() -> int:
    try:
        return int(_vc.tgt_sr)
    except Exception:
        return 40000  # this model's native rate


@asynccontextmanager
async def lifespan(_app):
    """Load once, at startup — no request may pay the model load."""
    load_with_fallback()
    yield


app = FastAPI(title="sahar-rvc", docs_url=None, redoc_url=None, lifespan=lifespan)


@app.get("/health")
def health():
    free_mib = None
    if torch.cuda.is_available():
        try:
            free_mib = round(torch.cuda.mem_get_info()[0] / 2**20)
        except Exception:
            pass
    return {
        "ok": _vc is not None,
        "device": _device,
        "model_loaded": _vc is not None,
        "model": MODEL_ID,
        "in_flight": _in_flight,
        "sample_rate": sample_rate() if _vc is not None else None,
        "chunk_seconds": CHUNK_SECONDS,
        "cpu_overflow_loaded": _vc_cpu is not None,
        "vram_free_mib": free_mib,
        "admission": _GATE_REASON,
        "uptime_s": round(time.monotonic() - _started, 1),
        **_stats,
    }


@app.post("/convert")
async def convert_endpoint(
    request: Request,
    pitch: int = Query(DEFAULT_PITCH),
    index_rate: float = Query(DEFAULT_INDEX_RATE),
    protect: float = Query(DEFAULT_PROTECT),
    volume_envelope: float = Query(DEFAULT_VOLUME_ENVELOPE),
    f0_method: str = Query(DEFAULT_F0_METHOD),
):
    """audio bytes in -> converted audio/wav out.

    Body may be raw audio (Content-Type: audio/wav — libsndfile sniffs the container, so wav/flac/ogg
    all work) or multipart/form-data with a `file` field and optional `pitch` field.
    """
    ctype = (request.headers.get("content-type") or "").lower()
    if ctype.startswith("multipart/form-data"):
        form = await request.form()
        upload = form.get("file") or form.get("audio")
        if not isinstance(upload, UploadFile):
            return JSONResponse({"error": "multipart body needs a 'file' part"}, status_code=400)
        data = await upload.read()
        if "pitch" in form:
            try:
                pitch = int(str(form["pitch"]))
            except ValueError:
                return JSONResponse({"error": "pitch must be an integer"}, status_code=400)
    else:
        data = await request.body()

    if not data:
        return JSONResponse({"error": "empty body"}, status_code=400)
    if len(data) > MAX_UPLOAD_BYTES:
        return JSONResponse({"error": f"body over {MAX_UPLOAD_BYTES} bytes"}, status_code=413)
    if abs(pitch) > PITCH_LIMIT:
        return JSONResponse({"error": f"pitch out of range +-{PITCH_LIMIT}"}, status_code=400)
    if _vc is None:
        return JSONResponse({"error": "model not loaded"}, status_code=503)

    from starlette.concurrency import run_in_threadpool

    return await run_in_threadpool(
        _convert_sync, data, _params(pitch, index_rate, protect, volume_envelope, f0_method)
    )


def _convert_sync(data: bytes, p: dict):
    _bump(+1)
    t0 = time.monotonic()
    fd, in_path = tempfile.mkstemp(suffix=".wav", dir=TMP_DIR, prefix="rvc-in-")
    device_used = _device
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        try:
            info = sf.info(in_path)
        except Exception as exc:
            _stats["errors"] += 1
            return JSONResponse({"error": f"undecodable audio: {exc}"}, status_code=400)
        if info.duration > MAX_INPUT_SECONDS:
            _stats["errors"] += 1
            return JSONResponse(
                {"error": f"input {info.duration:.1f}s exceeds max {MAX_INPUT_SECONDS:.0f}s"}, status_code=413
            )

        try:
            wav, sr, n_chunks = _convert_bounded(_vc, in_path, p)
        except torch.cuda.OutOfMemoryError as exc:
            # Should be unreachable now that inputs are chunked — kept as the last line of defence.
            # The PRIMARY converter is deliberately NOT downgraded: this request overflows to a CPU
            # copy and the next one is fast again.
            log.error("cuda OOM (%s) -> serving THIS request from the cpu overflow lane", exc)
            _stats["oom_overflows"] += 1
            try:
                torch.cuda.empty_cache()
            except Exception:
                pass
            wav, sr, n_chunks = _convert_bounded(_cpu_overflow(), in_path, p)
            device_used = "cpu-overflow"

        buf = io.BytesIO()
        sf.write(buf, wav, sr, format="WAV", subtype="PCM_16")
        dur = len(wav) / max(sr, 1)
        wall = time.monotonic() - t0
        _stats["converted"] += 1
        if n_chunks > 1:
            _stats["chunked"] += 1
        log.info(
            "convert in=%dB in_dur=%.2fs chunks=%d pitch=%+d device=%s wall=%.2fs audio=%.2fs rtf=%.3f rms=%.4f",
            len(data), info.duration, n_chunks, p["pitch"], device_used, wall, dur,
            wall / max(dur, 1e-6), float(np.sqrt(np.mean(np.square(wav)))),
        )
        return Response(
            buf.getvalue(),
            media_type="audio/wav",
            headers={
                "X-RVC-Model": MODEL_ID,
                "X-RVC-Device": device_used,
                "X-RVC-Chunks": str(n_chunks),
                "X-RVC-Wall-S": f"{wall:.3f}",
                "X-RVC-Audio-S": f"{dur:.3f}",
                "X-RVC-RTF": f"{wall / max(dur, 1e-6):.3f}",
            },
        )
    except Exception as exc:
        _stats["errors"] += 1
        log.error("convert failed: %s", exc, exc_info=True)
        return JSONResponse({"error": str(exc)}, status_code=500)
    finally:
        _bump(-1)
        try:
            os.remove(in_path)
        except OSError:
            pass


def _bump(delta: int):
    global _in_flight
    with _in_flight_lock:
        _in_flight += delta


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
