"""voice-loop speech server: POST /stt (faster-whisper) + POST /tts and /tts/stream (Silero or XTTS).

A single small FastAPI app you can run on a laptop (CPU) or on a box with a GPU and reach over your
LAN or an ssh tunnel. Everything is configured through the environment — see README.md:

    VOICE_LOOP_HOST          bind address                (default 127.0.0.1 — loopback only)
    VOICE_LOOP_PORT          port                        (default 8355)
    VOICE_LOOP_DEVICE        auto | cuda | cpu           (default auto)
    VOICE_LOOP_LANGUAGE      default language code       (default en; see SILERO_VOICES below)
    VOICE_LOOP_STT_MODEL     faster-whisper model size   (default small)
    VOICE_LOOP_COMPUTE_TYPE  auto | float16 | int8 | ... (default auto: float16 on cuda, int8 on cpu)
    VOICE_LOOP_STT_HINT      optional lexicon hint biasing the recognizer toward your jargon
    VOICE_LOOP_TTS_ENGINE    silero | xtts | ukrainian   (default silero; xtts = XTTS-v2 voice cloning,
                             ukrainian = the dedicated robinhad/ukrainian-tts voices — speaks uk only)
    VOICE_LOOP_TTS_ENGINE_<LANG>  per-language engine override, e.g. VOICE_LOOP_TTS_ENGINE_UK=ukrainian
                             routes ONLY Ukrainian requests at the ukrainian engine ('-' -> '_', so
                             zh-cn is VOICE_LOOP_TTS_ENGINE_ZH_CN)
    VOICE_LOOP_TTS_FALLBACK_ENGINE  silero | xtts | ukrainian | none — engine a failed synthesis retries on
                             (default: silero when any primary — the global engine or a per-language
                             override — is not silero, none otherwise)
    VOICE_LOOP_TTS_MODEL     override the Silero model for the default language
    VOICE_LOOP_TTS_SPEAKER   override the default speaker
    VOICE_LOOP_XTTS_REFERENCE  wav of the voice to clone (the xtts engine refuses requests without it)
    VOICE_LOOP_XTTS_MODEL_DIR  local XTTS-v2 model dir   (optional; default: coqui's own download cache)
    VOICE_LOOP_XTTS_MIN_FREE_VRAM_BYTES  minimum free VRAM before XTTS uses CPU (default: 3 GiB)
    VOICE_LOOP_STRESS_FILE   stress overrides            (default ~/.config/voice-loop/stress.json)
    VOICE_LOOP_HOOK_STAMP_FILE  the hook's heartbeat stamp (default
                             $XDG_STATE_HOME/voice-loop/hook-last-fired) — /health reports its age
    VOICE_LOOP_ACCENT        1 | 0 — enable automatic accentuation (default 1; ru and uk)
    VOICE_LOOP_MAX_UPLOAD_BYTES  /stt upload size cap    (default 26214400 — 25 MB)
    VOICE_LOOP_MAX_STT_SECONDS   /stt duration cap for parseable WAV uploads (default 600)
    VOICE_LOOP_STT_TIMEOUT   /stt wall-clock transcription budget, ANY codec (default 900 seconds;
                             0 disables it — see bounded_segments below)
    VOICE_LOOP_MAX_TTS_TEXT  /tts/stream text length cap (default 20000 characters)
    VOICE_LOOP_MAX_TTS_TEXT_BLOB /tts (single blob) text length cap (default 3000 characters)
    VOICE_LOOP_MODEL_CONCURRENCY how many model calls may run at once on the PRIMARY device
                             (default: 1 on a GPU, else up to 2 depending on core count; the other
                             device keeps its own default — see model_slot below)

Language is a request-level field: /stt takes ?language=, /tts takes {"language": ...}. Both fall back
to VOICE_LOOP_LANGUAGE. STT (whisper) is multilingual out of the box; TTS speaks what the selected
engine speaks — an unsupported code returns 400 with that engine's supported list.

No authentication: bind to loopback (the default) and reach it over ssh, or put it behind a reverse
proxy if you expose it. Do not put it on an untrusted network as-is.

Requires Python >= 3.10.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import sys
import tempfile
import threading
import time
import wave
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import torch
import uvicorn
from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.concurrency import run_in_threadpool

MIN_PYTHON = (3, 10)
SERVER_VERSION = "0.5.0"


def require_python(version: tuple[int, int] | None = None) -> None:
    """Fail loudly and legibly on an interpreter too old, instead of a confusing SyntaxError later."""
    version = version or sys.version_info[:2]
    if version < MIN_PYTHON:
        raise SystemExit(
            "voice-loop server needs Python {}.{} or newer, found {}.{}".format(*MIN_PYTHON, *version)
        )


require_python()

log = logging.getLogger("voice-loop")
app = FastAPI(title="voice-loop", docs_url=None, redoc_url=None)

# language code -> (Silero language, Silero model id, default speaker)
# Silero's own language keys differ from the ISO codes users type ("uk" is "ua" upstream), so this
# table is the single place that translation lives.
SILERO_VOICES: dict[str, tuple[str, str, str]] = {
    "ru": ("ru", "v4_ru", "baya"),
    "uk": ("ua", "v4_ua", "mykyta"),
    "en": ("en", "v3_en", "en_0"),
    "de": ("de", "v3_de", "eva_k"),
    "es": ("es", "v3_es", "es_0"),
    "fr": ("fr", "v3_fr", "fr_0"),
}

# XTTS-v2 (coqui-tts, an OPTIONAL dependency) is multilingual on its own — this is its fixed
# language set, not SILERO_VOICES. The voice comes from a reference wav, not a speaker name.
XTTS_LANGUAGES = {
    "ar", "cs", "de", "en", "es", "fr", "hi", "hu", "it", "ja", "ko", "nl", "pl", "pt", "ru", "tr", "zh-cn",
}
XTTS_MODEL_ID = "tts_models/multilingual/multi-dataset/xtts_v2"

# robinhad/ukrainian-tts (the OPTIONAL pip package `ukrainian-tts`) is a dedicated Ukrainian engine:
# its own trained voices, not a speaker name on a shared multilingual model — the answer to Silero's
# v4_ua/mykyta reading Ukrainian with a Russian accent. It speaks Ukrainian and nothing else.
UKRAINIAN_VOICES = ("tetiana", "mykyta", "lada", "oleksa")
UKRAINIAN_DEFAULT_VOICE = "tetiana"
# The package's own Stress.Dictionary.value — ukrainian-word-stress in dictionary mode, the same
# dictionary the Silero path's uk accentuator uses. Named as a plain string so the synthesis seam a
# test fakes does not have to carry the real enum.
UKRAINIAN_STRESS = "dictionary"

ENGINES = ("silero", "xtts", "ukrainian")
ENGINE_HEADER = "X-Voice-Loop-Engine"  # every /tts response names the engine that actually spoke

# What each engine can actually speak, addressed by engine name — the tables above and nothing
# else, so a refusal that points at another engine cannot drift from what that engine speaks.
ENGINE_LANGUAGES: dict[str, set[str]] = {
    "silero": set(SILERO_VOICES),
    "xtts": XTTS_LANGUAGES,
    "ukrainian": {"uk"},
}

HOST = os.environ.get("VOICE_LOOP_HOST", "127.0.0.1")
PORT = int(os.environ.get("VOICE_LOOP_PORT", "8355"))
DEVICE = os.environ.get("VOICE_LOOP_DEVICE", "auto")
LANGUAGE = os.environ.get("VOICE_LOOP_LANGUAGE", "en").lower()
STT_MODEL = os.environ.get("VOICE_LOOP_STT_MODEL", "small")
COMPUTE_TYPE = os.environ.get("VOICE_LOOP_COMPUTE_TYPE", "auto")
STT_HINT = os.environ.get("VOICE_LOOP_STT_HINT", "") or None
TTS_ENGINE = os.environ.get("VOICE_LOOP_TTS_ENGINE", "silero").lower()
# Per-language engine overrides: VOICE_LOOP_TTS_ENGINE_UK=ukrainian sends only uk requests to the
# dedicated engine while every other language stays on the global primary — a voice upgrade for one
# language, not a server-wide switch. The variable name is the language code uppercased with '-'
# turned into '_' (zh-cn is VOICE_LOOP_TTS_ENGINE_ZH_CN).
# XTTS is the native local Turkish path: keep the routing in the registry rather than teaching each
# endpoint a Turkish branch. An environment override can replace this or add another language.
TTS_ENGINE_BY_LANGUAGE: dict[str, str] = {"tr": "xtts"}
TTS_ENGINE_BY_LANGUAGE.update(
    {
        name.removeprefix("VOICE_LOOP_TTS_ENGINE_").replace("_", "-").lower(): engine.lower()
        for name, engine in os.environ.items()
        if name.startswith("VOICE_LOOP_TTS_ENGINE_") and engine
    }
)
# A down engine should degrade the voice, not silence it. The default pairs ANY non-Silero primary —
# the global engine or a per-language override — with the one engine that is always here; a
# silero-everywhere setup has nothing lighter to fall to.
TTS_FALLBACK_ENGINE = (
    os.environ.get("VOICE_LOOP_TTS_FALLBACK_ENGINE", "")
    or ("silero" if TTS_ENGINE != "silero" or set(TTS_ENGINE_BY_LANGUAGE.values()) - {"silero"} else "none")
).lower()
TTS_MODEL_OVERRIDE = os.environ.get("VOICE_LOOP_TTS_MODEL", "")
TTS_SPEAKER_OVERRIDE = os.environ.get("VOICE_LOOP_TTS_SPEAKER", "")
XTTS_REFERENCE = os.environ.get("VOICE_LOOP_XTTS_REFERENCE", "")
XTTS_MODEL_DIR = os.environ.get("VOICE_LOOP_XTTS_MODEL_DIR", "")
# XTTS shares the card with whisper and any contour service such as RVC. Leave enough headroom for
# its peak allocation; when the card is already occupied, CPU is slower but keeps speech alive.
XTTS_MIN_FREE_VRAM_BYTES = int(
    os.environ.get("VOICE_LOOP_XTTS_MIN_FREE_VRAM_BYTES", str(3 * 1024**3))
)
USE_ACCENT = os.environ.get("VOICE_LOOP_ACCENT", "1") not in ("0", "false", "no")
STRESS_FILE = Path(
    os.environ.get("VOICE_LOOP_STRESS_FILE", "")
    or Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "voice-loop" / "stress.json"
)
# The hook's heartbeat: speak.py rewrites this stamp on EVERY firing, so its age answers "is the
# harness still calling the hook?" — the question a mid-session silence needs answered first (the
# harness itself has been observed to stop invoking the Stop hook in a long session while the whole
# plugin chain stayed healthy). A server on ANOTHER machine (the ssh-tunnel deployment) cannot see
# the client's state dir, and reports null rather than a stale read of some other machine's stamp.
HOOK_STAMP_FILE = Path(
    os.environ.get("VOICE_LOOP_HOOK_STAMP_FILE", "")
    or Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "voice-loop" / "hook-last-fired"
)

TTS_SR = 48000
XTTS_SR = 24000  # XTTS-v2 always synthesizes at 24 kHz
UKRAINIAN_SR = 22050  # robinhad/ukrainian-tts always synthesizes at 22050 Hz
MAX_TTS_CHARS = 800  # Silero degrades past ~1000 characters per call
PAUSE_SECONDS = 0.4
MAX_UPLOAD_BYTES = int(os.environ.get("VOICE_LOOP_MAX_UPLOAD_BYTES", str(25 * 1024 * 1024)))
MAX_STT_SECONDS = float(os.environ.get("VOICE_LOOP_MAX_STT_SECONDS", "600"))
# The codec-independent half of the same bound. MAX_STT_SECONDS can only measure a WAV header, so a
# byte-capped upload of a compressed codec is admitted unmeasured; this is the wall clock that runs
# while it is being transcribed. Generous on purpose — 600 s of admitted audio on a small model on
# CPU is well inside it — because it exists to stop the pathological case, not to hurry an honest one.
STT_TIMEOUT = float(os.environ.get("VOICE_LOOP_STT_TIMEOUT", "900"))
MAX_TTS_TEXT = int(os.environ.get("VOICE_LOOP_MAX_TTS_TEXT", "20000"))
MAX_TTS_TEXT_BLOB = int(os.environ.get("VOICE_LOOP_MAX_TTS_TEXT_BLOB", "3000"))

# Whisper on near-silent clips hallucinates well-known junk ("Спасибо за просмотр", TV credits,
# "Thank you for watching") instead of returning nothing. The blocklist lives next to the server,
# one pattern per line, user-extendable — see the file's own header for the format.
HALLUCINATIONS_FILE = Path(__file__).with_name("stt_hallucinations.txt")

# Lazily filled caches. Everything expensive is loaded on first use through a seam a test can
# replace — the loaders below take their dependencies from importable modules and torch.hub, both
# patchable, so the whole file is exercisable without a single model on disk.
_whisper = None
_tts: dict[str, object] = {}
_xtts = None
# Where XTTS actually landed — "" until it loads. It normally loads on the resolved device, but a
# card too small for it degrades to CPU (see xtts()), and the queue it takes a slot from has to
# follow the hardware it really runs on rather than the one it was asked for.
_xtts_device = ""
_ukrainian = None
_accent: dict[str, object] = {}
_stress: list[tuple[re.Pattern[str], str]] | None = None
_hallucinations: list[str] | None = None
_hallucinations_dropped = 0
_hallucination_tails_stripped = 0
# What this microphone actually hallucinates, pattern -> hits: the blocklist grows from evidence
# rather than anecdote, so /health has to say WHICH entry fired, not just how often one did.
_hallucinations_by_pattern: dict[str, int] = {}
_tts_fallbacks = 0
# Its own lock rather than the model gate's `_in_flight_lock` below: this counter is bumped on the
# request path from several threads, and it has nothing to do with how many model slots are held.
_fallbacks_lock = threading.Lock()


def reset_caches() -> None:
    """Drop every lazily loaded model, accentuator, stress rule set and hallucination blocklist.

    Used by the tests between cases. Nothing in the running server calls it, so editing
    stress.json or the hallucination blocklist requires a restart in the current server.
    """
    global _whisper, _xtts, _xtts_device, _ukrainian, _stress, _hallucinations, _hallucinations_dropped
    global _hallucination_tails_stripped, _tts_fallbacks
    _whisper = None
    _xtts = None
    _xtts_device = ""
    _ukrainian = None
    _stress = None
    _hallucinations = None
    _hallucinations_dropped = 0
    _hallucination_tails_stripped = 0
    _tts_fallbacks = 0
    _hallucinations_by_pattern.clear()
    _tts.clear()
    _accent.clear()


def resolve_device() -> str:
    if DEVICE != "auto":
        return DEVICE
    return "cuda" if torch.cuda.is_available() else "cpu"


def resolve_compute_type(device: str) -> str:
    if COMPUTE_TYPE != "auto":
        return COMPUTE_TYPE
    return "float16" if device == "cuda" else "int8"


# The two queues in front of every model call. NOT one: the GPU and the CPU are disjoint hardware
# and Silero is pinned to the CPU (see tts()) precisely so synthesis does not compete with GPU
# recognition — a single global gate hands that separation straight back, letting one user's
# dictation block another user's playback for no physical reason. So a call queues on the device it
# actually runs on, and the two queues never touch.
#
# "gpu" covers every non-CPU torch device ("cuda", "cuda:1", "mps"): there is one accelerator budget
# to protect, and its name is not what makes it scarce.
MODEL_DEVICES = ("gpu", "cpu")


def model_device(device: str) -> str:
    """The queue a torch device name belongs to — one of MODEL_DEVICES."""
    return "cpu" if device == "cpu" else "gpu"


def default_model_concurrency(device: str = "") -> int:
    """How many model calls may run at once on a device when VOICE_LOOP_MODEL_CONCURRENCY does not say.

    The device split: the models release the GIL inside native code, so on CPU a second slot buys
    real multicore speedup — but on a GPU every concurrent call stacks its per-call activation
    memory on the ONE shared VRAM budget (whisper + the voices are already resident), and the only
    thing a second slot buys is an OOM. The floor of 1 keeps a single-core box from deadlocking.
    """
    if (device or model_device(resolve_device())) == "gpu":
        return 1
    return max(1, min(2, (os.cpu_count() or 2) // 2))


MODEL_CONCURRENCY = int(os.environ.get("VOICE_LOOP_MODEL_CONCURRENCY", "0")) or default_model_concurrency()

# One knob, and it sets the queue the operator was thinking about: the device this server runs its
# models on. The other queue is the incidental one (Silero's CPU slot on a GPU box, the idle GPU
# queue on a CPU box) and keeps its own default, so raising the primary never quietly widens it.
MODEL_CONCURRENCY_BY_DEVICE = {
    device: MODEL_CONCURRENCY if device == model_device(resolve_device()) else default_model_concurrency(device)
    for device in MODEL_DEVICES
}

# Plain threading semaphores ON PURPOSE, and one mechanism for every call site — /stt transcription,
# /tts blob rendering, each /tts/stream chunk synthesis: every model call already runs on a worker
# thread (run_in_threadpool for the endpoints, Starlette's iterate_in_threadpool for the stream
# generator), so a sync gate reaches all of them — an async CapacityLimiter could not guard the
# inside of the sync generator. anyio's ~40-thread default pool is what queues behind them; the
# gates bound MODEL work, not thread occupancy.
_model_gates = {device: threading.BoundedSemaphore(MODEL_CONCURRENCY_BY_DEVICE[device]) for device in MODEL_DEVICES}
# In flight is what is RUNNING; waiting is what is queued behind it. Both, because one of them alone
# cannot tell "busy" from "saturated": at the limit the first number reads the same either way, and
# the second is the one that says whether anybody is paying for it.
_model_in_flight = {device: 0 for device in MODEL_DEVICES}
_model_waiting = {device: 0 for device in MODEL_DEVICES}
_in_flight_lock = threading.Lock()


@contextmanager
def model_slot(device: str = ""):
    """Hold one slot on `device`'s queue (default: the primary device's) for one model call."""
    device = model_device(device or resolve_device())
    with _in_flight_lock:
        _model_waiting[device] += 1
    try:
        _model_gates[device].acquire()
    finally:  # queued or refused, the caller is no longer waiting
        with _in_flight_lock:
            _model_waiting[device] -= 1
    with _in_flight_lock:
        _model_in_flight[device] += 1
    try:
        yield
    finally:
        with _in_flight_lock:
            _model_in_flight[device] -= 1
        _model_gates[device].release()


def model_in_flight(device: str = "") -> int:
    """Model calls running right now — on one device's queue, or across both when unnamed."""
    with _in_flight_lock:
        if device:
            return _model_in_flight[model_device(device)]
        return sum(_model_in_flight.values())


def queue_depths() -> dict[str, dict[str, int]]:
    """One coherent snapshot of both queues — limit, running and queued, per device."""
    with _in_flight_lock:
        return {
            device: {
                "limit": MODEL_CONCURRENCY_BY_DEVICE[device],
                "in_flight": _model_in_flight[device],
                "waiting": _model_waiting[device],
            }
            for device in MODEL_DEVICES
        }


def synthesis_device(engine: str = "") -> str:
    """The device an engine's synthesis really runs on — which queue it must take its slot from.

    Silero and the ukrainian engine are pinned to the CPU (both are real-time there; the GPU stays
    free for STT). XTTS loads on the resolved device but degrades to the CPU when the card cannot
    hold it, so this follows where it ACTUALLY landed; before the first load there is nothing to
    read yet and the resolved device is the honest guess.
    """
    if (engine or TTS_ENGINE) == "xtts":
        return _xtts_device or resolve_device()
    return "cpu"


def whisper():
    """Lazily loaded recognizer — the model downloads on first request, not at import."""
    global _whisper
    if _whisper is None:
        from faster_whisper import WhisperModel

        device = resolve_device()
        _whisper = WhisperModel(STT_MODEL, device=device, compute_type=resolve_compute_type(device))
        log.info("loaded faster-whisper %s on %s", STT_MODEL, device)
    return _whisper


def stress_rules() -> list[tuple[re.Pattern[str], str]]:
    """User stress overrides, loaded from JSON — proper names and words the voice trips on.

    Format (either shape):
        {"\\bAcme\\b": "+Acme"}                 — object of regex -> replacement
        [["\\bAcme\\b", "+Acme"], ...]          — list of pairs (ordered)
    Silero's '+' goes immediately BEFORE the stressed vowel. Bad entries are skipped, never fatal.
    The rules are language-agnostic (they are your regexes) and apply to every accentuated language.
    """
    global _stress
    if _stress is None:
        rules: list[tuple[re.Pattern[str], str]] = []
        try:
            if STRESS_FILE.is_file():
                raw = json.loads(STRESS_FILE.read_text(encoding="utf-8"))
                items = raw.items() if isinstance(raw, dict) else (tuple(pair) for pair in raw)
                for pattern, replacement in items:
                    try:
                        rules.append((re.compile(pattern), replacement))
                    except re.error:
                        log.warning("stress.json: skipping invalid regex %r", pattern)
        except Exception as exc:
            log.warning("stress.json: ignored (%s)", exc)
        _stress = rules
        log.info("loaded %d stress override(s) from %s", len(rules), STRESS_FILE)
    return _stress


def normalize_transcript(text: str) -> str:
    """Fold a transcript to the shape hallucinations are matched in.

    Whitespace collapsed, lowercased, trailing punctuation stripped — the axes along which the same
    hallucination varies between runs ("Спасибо за просмотр!", "спасибо за просмотр.").
    """
    return re.sub(r"\s+", " ", text).strip().lower().rstrip(" .!?…,:;")


def hallucination_blocklist() -> list[str]:
    """Known silence-hallucination transcripts, loaded from HALLUCINATIONS_FILE and cached.

    One pattern per line, '#' comments and blank lines skipped; each pattern is stored normalized
    (see normalize_transcript). A missing or unreadable file degrades to an empty list, never fatal.
    """
    global _hallucinations
    if _hallucinations is None:
        patterns: list[str] = []
        try:
            if HALLUCINATIONS_FILE.is_file():
                for line in HALLUCINATIONS_FILE.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    normalized = normalize_transcript(line)
                    if normalized:
                        patterns.append(normalized)
        except Exception as exc:
            log.warning("stt_hallucinations.txt: ignored (%s)", exc)
        _hallucinations = patterns
        log.info("loaded %d hallucination pattern(s) from %s", len(patterns), HALLUCINATIONS_FILE)
    return _hallucinations


def matched_hallucination(text: str) -> str | None:
    """The blocklist pattern a transcript IS, or None.

    A transcript matches when its FULL normalized form equals a pattern, or starts with one at a
    word boundary — silence hallucinations often repeat or extend themselves. Genuine speech that
    merely CONTAINS a phrase mid-sentence is never touched.
    """
    normalized = normalize_transcript(text)
    if not normalized:
        return None
    for pattern in hallucination_blocklist():
        if normalized.startswith(pattern):
            if len(normalized) == len(pattern) or not normalized[len(pattern)].isalnum():
                return pattern
    return None


# A sentence break: end punctuation (an ellipsis included, "продолжение следует…" carries one)
# followed by whitespace. Deliberately not a general segmenter — it only has to find where the
# model started a new sentence, which is where the appended caption begins.
SENTENCE_BREAK = re.compile(r"(?<=[.!?…])\s+")

# Character ranges for the speakable-characters check in tts_request_error. A Silero voice for a
# Cyrillic language (ru, uk) needs at least one Cyrillic character; a Latin-language voice
# (en, de, es, fr) needs at least one Latin letter. Without one the model returns garbage or a 500.
_CYRILLIC_CHAR = re.compile(r"[Ѐ-ӿ]")
_LATIN_CHAR = re.compile(r"[a-zA-Z]")


def _has_speakable_chars(text: str, language: str) -> bool:
    """True when `text` contains at least one character a Silero voice for `language` can pronounce.

    A Russian or Ukrainian Silero voice fed pure Latin text returns garbage or raises inside the
    model (an unhandled 500). The same happens in reverse: a Latin voice fed pure Cyrillic. This is
    the check that turns that failure into a named 400 before synthesis is attempted.
    """
    if language in ("ru", "uk"):
        return bool(_CYRILLIC_CHAR.search(text))
    return bool(_LATIN_CHAR.search(text))


def strip_hallucinated_tail(text: str) -> tuple[str, list[tuple[str, str]]]:
    """Peel blocklisted closing captions off the END of a transcript, keeping the speech before them.

    A whisper trained on subtitles finishes a silent tail with a learned closing caption
    ("Продолжение следует…", "Thanks for watching"), APPENDED to an otherwise complete transcript.
    So this is not the whole-drop of matched_hallucination: the transcript is right, the last
    sentence is an addition, and only that sentence goes.

    A sentence break before the tail is REQUIRED. Without one, "скажи ему спасибо за просмотр" is
    ordinary speech that happens to end in a blocklisted phrase, and eating its last words would be
    a worse bug than the one this fixes — a quiet loss instead of a quiet addition. Only the LAST
    sentence is ever examined, and peeling stops at the first one that does not match.

    Returns the kept text and the peeled tails (last sentence first), each with the pattern it hit.
    """
    kept = text.strip()
    peeled: list[tuple[str, str]] = []
    while True:
        sentences = SENTENCE_BREAK.split(kept)
        if len(sentences) < 2:
            return kept, peeled
        pattern = matched_hallucination(sentences[-1])
        if pattern is None:
            return kept, peeled
        peeled.append((sentences[-1], pattern))
        kept = " ".join(sentences[:-1]).strip()


def count_hallucination(pattern: str) -> None:
    """Record one blocklist hit against the pattern that caught it, for the /health corpus."""
    _hallucinations_by_pattern[pattern] = _hallucinations_by_pattern.get(pattern, 0) + 1


def filter_transcript(text: str) -> str:
    """The transcript as it should reach the user — every blocklist hit removed, logged and counted.

    Two shapes, two contracts, in this order:

    * a trailing closing caption is STRIPPED and the speech before it kept (see
      strip_hallucinated_tail) — the transcript is complete, the caption is the addition;
    * what remains, if it IS a hallucination end to end, is dropped whole, as since the blocklist
      shipped.

    Nothing is removed invisibly: each hit is logged at INFO with the exact text that went, so an
    operator chasing a word the user swears they said can see the filter fire — the pasted text is
    the cleaned one, the log is where the difference lives.

    Counters bump on the event loop (the endpoint awaits the model in the threadpool and filters
    after), so they need no lock of their own.
    """
    global _hallucinations_dropped, _hallucination_tails_stripped
    kept, peeled = strip_hallucinated_tail(text)
    for tail, pattern in peeled:
        _hallucination_tails_stripped += 1
        count_hallucination(pattern)
        log.info("stripped a hallucinated tail %r (matched %r) off %r", tail, pattern, text)
    pattern = matched_hallucination(kept)
    if pattern is not None:
        _hallucinations_dropped += 1
        count_hallucination(pattern)
        log.info("dropped a hallucinated transcript %r (matched %r)", kept, pattern)
        return ""
    return kept


def acute_to_plus(text: str) -> str:
    """Combining acute typed by a human ("Сaха́р", "робо́та") -> Silero's '+' notation ("Сах+ар")."""
    return re.sub(r"([^\W\d_])́", r"+\1", text, flags=re.UNICODE)


STRESSED_VOWELS = "аеёиоуыэюяіїєaeiouy"  # everything Silero's '+' can precede, both alphabets


def strip_stress_markers(text: str) -> str:
    """Remove Silero-oriented stress marks for an engine with its own prosody (XTTS).

    Anchored on purpose, mirroring where the notation actually puts the mark: a '+' is removed only
    immediately before a vowel, so "C++" and "2+2" survive; a combining acute is removed only after
    a letter — the same anchor acute_to_plus() matches.
    """
    text = re.sub(f"\\+(?=[{STRESSED_VOWELS}])", "", text, flags=re.IGNORECASE)
    return re.sub(r"([^\W\d_])́", r"\1", text, flags=re.UNICODE)


def _load_ru_accentuator():
    """RUAccent (COLING-2025): context-aware stress + yo-fication, already in '+' notation."""
    from ruaccent import RUAccent

    engine = RUAccent()
    engine.load(omograph_model_size="turbo", use_dictionary=True)
    return engine.process_all


def _load_uk_accentuator():
    """ukrainian-word-stress: dictionary-based accentuation, normalized to Silero's '+' notation.

    Neither argument is decorative. Left to its default the package marks stress with a SPACING
    acute ("приві´т", U+00B4) that acute_to_plus() does not match and Silero cannot read; the
    combining acute (U+0301) is the format this pipeline normalizes FROM. And its default
    disambiguation backend builds a Stanza pipeline that downloads ~500 MB of models on first use —
    dictionary mode is what this server advertises, needs no network, and resolves ~98.7% of
    entries identically (the ambiguous rest are simply left unstressed).
    """
    from ukrainian_word_stress import Disambiguation, Stressifier, StressSymbol

    stressify = Stressifier(
        stress_symbol=StressSymbol.CombiningAcuteAccent,
        disambiguation=Disambiguation.Dictionary,
    )

    def process(segment: str) -> str:
        return acute_to_plus(stressify(segment))

    return process


ACCENTUATORS = {"ru": _load_ru_accentuator, "uk": _load_uk_accentuator}


def accentuator(language: str):
    """Per-language accentuation callable, loaded once and cached.

    Best-effort by design: a missing package or a surprising API degrades to the user's stress
    dictionary alone (logged once) and NEVER blocks synthesis.
    """
    if language not in ACCENTUATORS:
        return None
    if language not in _accent:
        if not USE_ACCENT:
            _accent[language] = False
        else:
            try:
                _accent[language] = ACCENTUATORS[language]()
                log.info("accentuation enabled for %s", language)
            except Exception as exc:
                log.warning(
                    "accentuation unavailable for %s (%s) — using the stress dictionary alone", language, exc
                )
                _accent[language] = False
    return _accent[language] or None


def tts(language: str):
    """Silero model for a language, cached per language (models are a few hundred MB each)."""
    if language not in _tts:
        silero_language, model_id, _ = SILERO_VOICES[language]
        if TTS_MODEL_OVERRIDE and language == LANGUAGE:
            model_id = TTS_MODEL_OVERRIDE
        model, _example = torch.hub.load(
            "snakers4/silero-models", "silero_tts", language=silero_language, speaker=model_id, trust_repo=True
        )
        model.to(torch.device("cpu"))  # Silero on CPU is real-time enough; the GPU stays free for STT
        _tts[language] = model
        log.info("loaded Silero %s (%s) for %s", model_id, silero_language, language)
    return _tts[language]


def default_speaker(language: str) -> str:
    if TTS_SPEAKER_OVERRIDE and language == LANGUAGE:
        return TTS_SPEAKER_OVERRIDE
    return SILERO_VOICES[language][2]


def _load_xtts(device: str):
    """Build the coqui TTS wrapper on a device — split out so the OOM fallback can retry it."""
    from TTS.api import TTS

    if XTTS_MODEL_DIR:
        return TTS(model_path=XTTS_MODEL_DIR, config_path=str(Path(XTTS_MODEL_DIR) / "config.json")).to(device)
    return TTS(XTTS_MODEL_ID).to(device)


def xtts_device(device: str) -> str:
    """Choose a safe XTTS tenant before loading, preserving the GPU for resident contour models.

    `mem_get_info` is advisory: if a driver cannot report free VRAM, the loader keeps its existing
    OOM fallback. A known-small card takes the CPU path proactively, so XTTS does not evict or
    starve whisper/RVC while it grows its activation buffers.

    The probe must never be the thing that fails the request, and torch has three ways of refusing
    to answer: no such function at all (older torch), a driver that errors (RuntimeError), and — the
    one a VOICE_LOOP_DEVICE=cuda override on a CPU-only wheel hits — torch's own lazy-init
    `AssertionError: Torch not compiled with CUDA enabled`. All three mean "unknown", which is not
    the same as "too small": an unreadable card keeps the device it was given, and a real shortage
    is still caught by the loader's OOM fallback.
    """
    if device == "cpu":
        return device
    try:
        free, _total = torch.cuda.mem_get_info(device)
    except (AttributeError, AssertionError, RuntimeError):
        free = XTTS_MIN_FREE_VRAM_BYTES  # unknown reads as exactly enough, never as a reason to move
    if free < XTTS_MIN_FREE_VRAM_BYTES:
        log.warning(
            "XTTS-v2 has only %d bytes of free VRAM (need %d) — using CPU to preserve GPU tenants",
            free,
            XTTS_MIN_FREE_VRAM_BYTES,
        )
        return "cpu"
    return device


def xtts():
    """Lazily loaded XTTS-v2 voice cloner, cached; reset_caches() drops it like every other model.

    The weights are downloaded by coqui-tts on the USER's first request (they are CPML-licensed —
    set COQUI_TOS_AGREED=1 to accept), never bundled with this repo. A GPU too small for the model
    (~2-2.5 GB of VRAM) degrades to CPU instead of failing the request. The free-VRAM guard keeps
    the GPU tenant-safe when whisper or an adjacent contour service is already resident.
    """
    global _xtts, _xtts_device
    if _xtts is None:
        device = xtts_device(resolve_device())
        try:
            _xtts = _load_xtts(device)
        except torch.cuda.OutOfMemoryError:
            log.warning("XTTS-v2 does not fit on the GPU — retrying on CPU")
            device = "cpu"
            _xtts = _load_xtts(device)
        _xtts_device = device  # the queue synthesis_device() sends the next request to
        log.info("loaded XTTS-v2 on %s", device)
    return _xtts


def ukrainian_tts():
    """Lazily loaded robinhad/ukrainian-tts engine, cached; reset_caches() drops it like every model.

    The voices are downloaded by the package on the USER's first request (MIT-licensed, from its
    Hugging Face repo), never bundled with this repo. CPU-pinned on purpose, like Silero: the model
    is small and real-time there, and the GPU stays free for recognition.
    """
    global _ukrainian
    if _ukrainian is None:
        from ukrainian_tts.tts import TTS

        _ukrainian = TTS(device="cpu")
        log.info("loaded ukrainian-tts on cpu")
    return _ukrainian


def protected_segments(text: str) -> list[tuple[str, bool]]:
    """Cut text into `(segment, is_marked)` runs — the seam that hides '+'-marked tokens.

    A whitespace-delimited token carrying a '+' is already stressed, so it becomes a segment of its
    own and never reaches the accentuator. Everything between two such tokens — the words AND the
    whitespace around them — accumulates into ONE run, so the engine still sees multi-word context
    instead of isolated words it cannot disambiguate.

    Tokenizing on whitespace and grouping afterwards is deliberate. Matching the marked tokens
    directly with a pattern like `(\\S*\\+\\S*)` selects the same segments, but its two unbounded
    quantifiers make the matcher retry every start position against the rest of the input: quadratic
    on a long unbroken string, which /tts accepts straight from the network. `(\\s+)` has no such
    ambiguity, and the grouping below is a single linear pass.
    """
    segments: list[list[str]] = []
    marked: list[bool] = []
    for token in re.split(r"(\s+)", text):
        is_marked = "+" in token  # whitespace tokens never carry one, so this only picks words
        if is_marked or not segments or marked[-1]:
            segments.append([token])
            marked.append(is_marked)
        else:
            segments[-1].append(token)
    return [("".join(parts), is_marked) for parts, is_marked in zip(segments, marked)]


def mark_stress(text: str, language: str) -> str:
    """Normalize stress marking to Silero's '+' notation. ORDER IS LOAD-BEARING (debugged live):

    1. combining acute typed by a human ("Сaха́р") -> '+' notation ("Сах+ар");
    2. the user's override dictionary (adds '+' to known proper names);
    3. the automatic accentuator LAST (RUAccent for ru, ukrainian-word-stress for uk) — and it must
       never see the already-'+'-marked tokens: it re-stresses them from ITS own dictionary and undoes
       the override. So the text is cut into runs by protected_segments() and only the free ones are
       accentuated.

    One mechanism, one place: languages differ only by which callable fills slot 3. Languages with no
    accentuator and no user rules pass through untouched.
    """
    if language not in ACCENTUATORS:
        return text
    text = acute_to_plus(text)
    for pattern, replacement in stress_rules():
        text = pattern.sub(replacement, text)
    engine = accentuator(language)
    if engine is not None:
        parts: list[str] = []
        for segment, is_marked in protected_segments(text):
            if is_marked or not segment.strip():
                parts.append(segment)
                continue
            try:
                parts.append(engine(segment))
            except Exception as exc:
                log.debug("accentuation failed on a segment (%s) — kept as is", exc)
                parts.append(segment)
        text = "".join(parts)
    return text


def hard_split(run: str, limit: int) -> list[str]:
    """Length-slice one unbroken run — the last resort when there is no boundary to prefer."""
    return [run[start : start + limit] for start in range(0, len(run), limit)]


def word_split(sentence: str, limit: int) -> list[str]:
    """Break ONE over-limit sentence into pieces no longer than the limit.

    Word boundaries are preferred; a single word longer than the limit itself (an URL, a run of
    CJK, pasted code) is sliced hard. The trailing slice of a hard-split word stays in the buffer
    so following words can still pack onto it.
    """
    pieces: list[str] = []
    buf = ""
    for word in sentence.split():
        if len(word) > limit:
            if buf:
                pieces.append(buf)
            *whole, buf = hard_split(word, limit)
            pieces.extend(whole)
        elif len(buf) + len(word) + 1 > limit and buf:
            pieces.append(buf)
            buf = word
        else:
            buf = f"{buf} {word}".strip()
    if buf:
        pieces.append(buf)
    return pieces


def chunk(text: str, limit: int = MAX_TTS_CHARS) -> list[str]:
    """Split on sentence boundaries so no single synthesis call exceeds the model's comfortable length.

    A sentence with no boundary to cut at — no sentence punctuation at all is common for CJK text,
    URLs and pasted code — is length-limited too (word_split above): every returned chunk is at
    most `limit` characters, unconditionally. MAX_TTS_CHARS exists because the model degrades past
    it, and that holds regardless of how the text is punctuated.
    """
    chunks: list[str] = []
    buf = ""
    for sentence in re.split(r"(?<=[.!?…])\s+", text):
        for piece in word_split(sentence, limit) if len(sentence) > limit else (sentence,):
            if len(buf) + len(piece) + 1 > limit and buf:
                chunks.append(buf)
                buf = piece
            else:
                buf = f"{buf} {piece}".strip()
    if buf:
        chunks.append(buf)
    return chunks


def is_local_origin(origin: str) -> bool:
    """True for origins a same-machine page legitimately sends: loopback hosts and the opaque "null"."""
    if origin == "null":
        return True
    try:
        host = urlsplit(origin).hostname or ""
    except ValueError:
        return False
    return host == "localhost" or host == "::1" or host.startswith("127.")


def cross_site_error(request: Request) -> JSONResponse | None:
    """Refuse browser-fired cross-site requests, or None when the request may proceed.

    Multipart (and text/plain) bodies are CORS-"simple", so any web page can make a visitor's
    browser POST them at this loopback server without a preflight — a drive-by way to burn CPU/GPU
    on someone else's synthesis. Browsers label such requests (Sec-Fetch-Site and/or Origin);
    non-browser clients — curl, the plugin scripts — send neither header and pass untouched.
    """
    if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
        return JSONResponse({"error": "cross-site browser requests are not accepted"}, status_code=403)
    origin = request.headers.get("origin")
    if origin is not None and not is_local_origin(origin):
        return JSONResponse({"error": "cross-origin browser requests are not accepted"}, status_code=403)
    return None


def switch_engine_hint(engine: str, language: str) -> str:
    """The `switch VOICE_LOOP_TTS_ENGINE=...` hint when the OTHER engine speaks a refused language.

    A refusal listing only the refusing engine's own languages is a dead end, and often a wrong one:
    the box frequently HAS a voice for the request, one setting away (Silero speaks 'uk' and XTTS-v2
    does not; XTTS-v2 speaks 'ja' and 'zh-cn' and Silero does not). Read off ENGINE_LANGUAGES, so the
    hint states what the engines really speak — and stays empty when nothing here could serve it.

    A hint, never a route: the request itself is still refused (see fallback_for), because switching
    engines is the operator's decision about which voice this server has, not a per-request one.

    The engine already CONFIGURED is never suggested. `engine` is the primary in the ordinary case,
    but on the fallback path it is the fallback refusing a language the primary speaks — and telling
    an operator to switch to the engine that just broke is worse than saying nothing.
    """
    for other in ENGINES:
        if other != engine and other != TTS_ENGINE and language in ENGINE_LANGUAGES[other]:
            return f"switch VOICE_LOOP_TTS_ENGINE={other} to serve {language!r}"
    return ""


def xtts_import_error(err: ImportError) -> JSONResponse:
    """The refusal for an xtts import that failed — naming WHICH failure it was.

    "coqui-tts is not installed" is the right answer to exactly one of these and a lie about the
    rest: coqui-tts declares neither torch nor torchaudio yet imports torchaudio, and an unpinned
    transformers takes an API out from under it — so the usual real failure is a package that IS
    installed and cannot import. Sending that person to reinstall it wastes their evening.

    So the client is told the SHAPE of the failure, never the exception's text: the class name plus,
    for a ModuleNotFoundError, the module that was not found — a module name is a fact about the
    dependency graph, while `str(err)` carries paths, config values and other internals (CodeQL:
    information exposure through an exception; the streaming error event resolved the same rule the
    same way). The full message stays where it is useful and unexposed — the server log. The install
    hint rides along ONLY when the top-level `TTS` package itself is the thing missing.
    """
    log.warning(
        "the xtts engine could not import coqui-tts — %s",
        " ".join(f"{type(err).__name__}: {err}".split())[:300],
    )
    module = err.name if isinstance(err, ModuleNotFoundError) else None
    if module == "TTS":
        return JSONResponse(
            {
                "error": "xtts import failed: ModuleNotFoundError for 'TTS' — the optional coqui-tts "
                "package is not installed",
                "hint": "pip install coqui-tts — its XTTS-v2 weights are CPML-licensed (non-commercial) "
                "and download on first use; set COQUI_TOS_AGREED=1 to accept",
            },
            status_code=500,
        )
    where = f" in dependency {module!r}" if module else ""
    return JSONResponse(
        {
            "error": f"xtts import failed: {type(err).__name__}{where} — the message is in the server log",
            "hint": "coqui-tts is installed but something it imports is not — reinstalling it alone will "
            "not help. The pinned set that works is torch==2.8.0 torchaudio==2.8.0 'transformers<5'; "
            "see server/README.md → XTTS engine",
        },
        status_code=500,
    )


def xtts_request_error(language: str) -> JSONResponse | None:
    """Why an xtts request cannot be served right now, or None.

    Checked per request ON PURPOSE: the server must still boot and serve /stt with coqui-tts absent
    or the reference unset — a broken TTS engine is a request-level 500, never a startup failure.
    """
    try:
        from TTS.api import TTS  # noqa: F401 — availability probe only; xtts() does the real import
    except ImportError as err:
        return xtts_import_error(err)
    if not XTTS_REFERENCE:
        return JSONResponse(
            {
                "error": "VOICE_LOOP_XTTS_REFERENCE is not set — the xtts engine clones a voice from a reference wav",
                "hint": "point it at a clean 6-30 second wav recording of the voice to clone",
            },
            status_code=500,
        )
    if not Path(XTTS_REFERENCE).is_file():
        return JSONResponse({"error": f"XTTS reference wav not found: {XTTS_REFERENCE}"}, status_code=500)
    if language not in XTTS_LANGUAGES:
        body: dict[str, object] = {
            "error": f"XTTS-v2 does not speak language {language!r}",
            "supported": sorted(XTTS_LANGUAGES),
        }
        hint = switch_engine_hint("xtts", language)
        if hint:
            body["hint"] = hint
        return JSONResponse(body, status_code=400)
    return None


def ukrainian_import_error(err: ImportError) -> JSONResponse:
    """The refusal for a ukrainian-tts import that failed — naming WHICH failure it was.

    Same contract as xtts_import_error, for the same reason: "ukrainian-tts is not installed" is
    the right answer to exactly one of these and a lie about the rest. The client is told the SHAPE
    of the failure — the exception class plus, for a ModuleNotFoundError, the missing module's name
    — never the exception's own text, which can carry paths and other internals (CodeQL:
    information exposure through an exception). The full message stays in the server log.
    """
    log.warning(
        "the ukrainian engine could not import ukrainian-tts — %s",
        " ".join(f"{type(err).__name__}: {err}".split())[:300],
    )
    module = err.name if isinstance(err, ModuleNotFoundError) else None
    if module == "ukrainian_tts":
        return JSONResponse(
            {
                "error": "ukrainian import failed: ModuleNotFoundError for 'ukrainian_tts' — the optional "
                "ukrainian-tts package is not installed",
                "hint": "install the documented recipe (server/README.md → Ukrainian engine): CPU "
                "torch/torchaudio first, then pip install ukrainian-tts, then pip install "
                "'ukrainian-word-stress>=2.0' — its MIT-licensed voices download from Hugging Face "
                "on first use",
            },
            status_code=500,
        )
    where = f" in dependency {module!r}" if module else ""
    return JSONResponse(
        {
            "error": f"ukrainian import failed: {type(err).__name__}{where} — the message is in the server log",
            "hint": "ukrainian-tts is installed but something it imports is not — reinstalling it alone "
            "will not help; see server/README.md → Ukrainian engine",
        },
        status_code=500,
    )


def ukrainian_request_error(language: str) -> JSONResponse | None:
    """Why a ukrainian-engine request cannot be served right now, or None.

    Checked per request ON PURPOSE, exactly like the xtts probe: the server must still boot and
    serve /stt with the package absent — a broken TTS engine is a request-level 500, never a
    startup failure.
    """
    try:
        from ukrainian_tts.tts import TTS  # noqa: F401 — availability probe only; ukrainian_tts() loads
    except ImportError as err:
        return ukrainian_import_error(err)
    if language != "uk":
        body: dict[str, object] = {
            "error": f"the ukrainian engine speaks only 'uk', not {language!r}",
            "supported": ["uk"],
        }
        hint = switch_engine_hint("ukrainian", language)
        if hint:
            body["hint"] = hint
        return JSONResponse(body, status_code=400)
    return None


def tts_request_error(text: str, language: str, engine: str = "") -> JSONResponse | None:
    """The shared /tts + /tts/stream refusal, or None when the request can be synthesized.

    The engine is a parameter (empty = the configured primary) because the fallback path asks the
    very same question of a DIFFERENT engine before it takes the request over — the two engines'
    language sets and prerequisites are not the same.

    The length caps live at the call sites, not here — the two endpoints hold the model executor
    very differently (one blob call vs. a released-and-reacquired slot per chunk), so they carry
    different limits.
    """
    engine = engine or TTS_ENGINE
    if not text:
        return JSONResponse({"error": "empty text"}, status_code=400)
    if engine == "xtts":
        return xtts_request_error(language)
    if engine == "ukrainian":
        return ukrainian_request_error(language)
    if engine != "silero":
        return JSONResponse(
            {"error": f"unknown TTS engine {engine!r} (VOICE_LOOP_TTS_ENGINE)", "supported": list(ENGINES)},
            status_code=500,
        )
    if language not in SILERO_VOICES:
        hint = "use a cloud TTS backend for this language, or add its Silero model to SILERO_VOICES"
        switch = switch_engine_hint("silero", language)
        return JSONResponse(
            {
                "error": f"no local TTS model for language {language!r}",
                "supported": sorted(SILERO_VOICES),
                "hint": f"{switch}; {hint}" if switch else hint,
            },
            status_code=400,
        )
    if not _has_speakable_chars(text, language):
        speaker = default_speaker(language)
        return JSONResponse(
            {
                "error": f"no speakable characters for the {language!r} voice ({speaker})",
                "hint": (
                    "the text contains no characters this voice can pronounce; "
                    "transliterate Latin text to Cyrillic for ru/uk voices, "
                    "or switch to a matching voice"
                ),
            },
            status_code=400,
        )
    return None


def engine_sample_rate(engine: str = "") -> int:
    engine = engine or TTS_ENGINE
    if engine == "xtts":
        return XTTS_SR
    if engine == "ukrainian":
        return UKRAINIAN_SR
    return TTS_SR


def silero_pieces(text: str, language: str, speaker: str = ""):
    """Silero synthesis: the stress pipeline, then one tensor per sentence chunk."""
    speaker = speaker or default_speaker(language)
    text = mark_stress(text, language)
    for part in chunk(text):
        yield tts(language).apply_tts(text=part, speaker=speaker, sample_rate=TTS_SR)


def xtts_pieces(text: str, language: str):
    """XTTS synthesis, one tensor per sentence chunk.

    XTTS brings its own prosody, so the Silero stress pipeline is SKIPPED — markers already in the
    text are stripped instead of applied. The model's true streaming generator (inference_stream)
    lives below coqui's public api and needs the speaker latents plumbed by hand, so both engines
    stream at the same granularity: the sentence chunker.
    """
    model = xtts()
    for part in chunk(strip_stress_markers(text)):
        wav = model.tts(text=part, speaker_wav=XTTS_REFERENCE, language=language)
        yield torch.as_tensor(wav, dtype=torch.float32)


def ukrainian_pieces(text: str, language: str, speaker: str = ""):
    """ukrainian-tts synthesis, one tensor per sentence chunk.

    The package runs its own stress pass (ukrainian-word-stress in dictionary mode — the same
    dictionary the Silero path's accentuator uses), so the Silero stress pipeline is SKIPPED exactly
    as for XTTS: '+' markers and combining acutes already in the text are stripped, and stress.json
    does not reach this engine. The package's API writes a WAV into a file object, so each chunk
    round-trips through a buffer back into a tensor at the engine's own rate — the rate guard is
    what keeps a silent upstream rate change from pitching every utterance.
    """
    import soundfile as sf

    model = ukrainian_tts()
    voice = speaker or UKRAINIAN_DEFAULT_VOICE
    for part in chunk(strip_stress_markers(text)):
        buffer = io.BytesIO()
        model.tts(part, voice, UKRAINIAN_STRESS, buffer)
        buffer.seek(0)
        samples, rate = sf.read(buffer, dtype="float32")
        if rate != UKRAINIAN_SR:
            raise RuntimeError(
                f"ukrainian-tts wrote {rate} Hz but this server encodes it as {UKRAINIAN_SR} — "
                "the package changed its output rate"
            )
        yield torch.as_tensor(samples)


def synthesis_pieces(text: str, language: str, speaker: str = "", engine: str = ""):
    """One synthesized tensor per sentence chunk from ONE engine (empty = the configured primary) —
    the shared core of /tts (which concatenates them) and /tts/stream (which ships each one as it
    appears), and the seam the fallback re-enters with the other engine's name."""
    engine = engine or TTS_ENGINE
    if engine == "xtts":
        return xtts_pieces(text, language)
    if engine == "ukrainian":
        return ukrainian_pieces(text, language, speaker)
    return silero_pieces(text, language, speaker)


# --- engine fallback ------------------------------------------------------------------------------
# A primary engine that cannot speak — coqui-tts missing, weights that will not load, a synthesis
# that raises — degrades to the configured fallback instead of silencing the voice. Everything
# below decides WHO speaks; the endpoints below that carry it out and say so on the way back.


def primary_engine(language: str) -> str:
    """The engine a request in `language` goes to FIRST: its per-language override, else the global.

    VOICE_LOOP_TTS_ENGINE_UK=ukrainian routes only Ukrainian requests at the dedicated engine while
    every other language stays on the configured primary — a per-language voice upgrade, not a
    server-wide switch. An override naming an engine that cannot serve the request is refused (and
    fallen back from, when there is a fallback) exactly like a bad global setting.
    """
    return TTS_ENGINE_BY_LANGUAGE.get(language) or TTS_ENGINE


def fallback_engine(primary: str = "") -> str:
    """The engine a broken primary retries on, or "" when there is nothing to fall to.

    Deliberately quiet about nonsense: "none", an unknown value, and the primary engine itself all
    mean the same thing here. A fallback is a safety net — misconfiguring it must never turn a
    working primary into an error. `primary` is the per-request primary (empty = the global
    engine): with per-language overrides the same setting can be useless behind one language's
    primary (silero behind silero) and exactly right behind another's (silero behind ukrainian).
    """
    primary = primary or TTS_ENGINE
    if TTS_FALLBACK_ENGINE == primary or TTS_FALLBACK_ENGINE not in ENGINES:
        return ""
    return TTS_FALLBACK_ENGINE


def fallback_for(text: str, language: str) -> tuple[str, JSONResponse | None]:
    """(engine, refusal) for a broken primary — at most one of the two is ever set.

    A configured fallback still has to be able to serve THIS request: the two engines' language
    sets differ (XTTS-v2 speaks ja and zh-cn, Silero does not; Silero speaks uk, XTTS-v2 does not).
    When it cannot, the caller gets that engine's ordinary refusal to hand back, so the client
    learns why nothing here could speak instead of receiving a bare 500.
    """
    engine = fallback_engine(primary_engine(language))
    if not engine:
        return "", None
    refusal = tts_request_error(text, language, engine)
    if refusal is not None:
        return "", refusal
    return engine, None


def count_fallback() -> None:
    """One more request a broken primary handed over — surfaced as /health `tts_fallbacks`.

    Locked because the handovers do NOT all happen on one thread: /tts counts from the event loop,
    while the stream's restart counts from inside the generator, which Starlette iterates on a
    worker thread. `+= 1` is a read-modify-write, so concurrent handovers — exactly what a primary
    that is down produces — would undercount the number the operator reads off /health.
    """
    global _tts_fallbacks
    with _fallbacks_lock:
        _tts_fallbacks += 1


def engine_label(engine: str, is_fallback: bool) -> str:
    """How a response names the engine that actually spoke: "silero" or "silero (fallback)"."""
    return f"{engine} (fallback)" if is_fallback else engine


def resolve_engine(text: str, language: str) -> tuple[str, bool, JSONResponse | None]:
    """Who serves this request BEFORE any synthesis runs: (engine, is_fallback, refusal).

    The primary's own check comes first, and its STATUS decides whether a fallback may help. A 400
    is the client's problem — no engine fixes an empty text — and is returned untouched. A 500 says
    the ENGINE is unusable (coqui-tts not installed, no reference wav, a typo in the engine name),
    which is exactly what the fallback exists for, so the fallback is asked the same question and
    takes the request over if it can.
    """
    primary = primary_engine(language)
    refusal = tts_request_error(text, language, primary)
    if refusal is None:
        return primary, False, None
    if refusal.status_code != 500:
        return "", False, refusal
    engine, refused = fallback_for(text, language)
    if not engine:
        return "", False, refused or refusal
    log.warning("the %s engine cannot serve requests — falling back to %s", primary, engine)
    count_fallback()
    return engine, True, None


def _default_wall_clock() -> float:
    """Epoch seconds. Wall time is an INPUT here, injected so a test needs no real clock."""
    return time.time()


def hook_heartbeat(clock: Callable[[], float] = _default_wall_clock) -> tuple[str | None, float | None]:
    """(ISO-8601 UTC stamp, age in seconds) of the hook's last firing — (None, None) when there is
    no readable stamp: the hook never fired on this machine, the stamp is corrupt, or the server
    runs on another machine than the client (the ssh-tunnel deployment) and there is no state dir
    here to read. The age is signed on purpose: a NEGATIVE age means the stamp is ahead of this
    machine's clock, which is itself worth knowing before trusting the number."""
    try:
        fired = float(HOOK_STAMP_FILE.read_text(encoding="utf-8").strip())
        stamp = datetime.fromtimestamp(fired, timezone.utc).isoformat(timespec="milliseconds")
    except (OSError, ValueError, OverflowError):
        return None, None
    return stamp, clock() - fired


@app.get("/health")
def health() -> dict[str, object]:
    queues = queue_depths()
    hook_fired_at, hook_fired_age = hook_heartbeat()
    return {
        "ok": True,
        "version": SERVER_VERSION,
        "model_concurrency": MODEL_CONCURRENCY,
        "model_in_flight": sum(queue["in_flight"] for queue in queues.values()),
        # Busy and saturated read identically without this: at the limit `model_in_flight` is
        # pinned to it either way, and `model_waiting` is what says whether anyone is queued
        # behind. `model_queues` splits both by device, which is where the slots actually are.
        "model_waiting": sum(queue["waiting"] for queue in queues.values()),
        "model_queues": queues,
        "device": resolve_device(),
        "cuda": torch.cuda.is_available(),
        "language": LANGUAGE,
        "stt_model": STT_MODEL,
        "tts_engine": TTS_ENGINE,
        "tts_engine_overrides": dict(TTS_ENGINE_BY_LANGUAGE),  # the per-language routing, if any
        "tts_fallback_engine": fallback_engine() or "none",  # the EFFECTIVE one behind the global engine
        "tts_fallbacks": _tts_fallbacks,
        "tts_languages": sorted(SILERO_VOICES),
        "accentuated_languages": sorted(ACCENTUATORS),
        "streaming": True,
        "stt_loaded": _whisper is not None,
        "tts_loaded": sorted(_tts),
        "xtts_loaded": _xtts is not None,
        "ukrainian_loaded": _ukrainian is not None,
        "stt_hallucinations_dropped": _hallucinations_dropped,
        "stt_hallucination_tails_stripped": _hallucination_tails_stripped,
        "stt_hallucinations_by_pattern": dict(_hallucinations_by_pattern),
        # The hook's heartbeat (see HOOK_STAMP_FILE): nulls when the stamp is not readable HERE,
        # which includes the ssh-tunnel deployment where hook and server are on different machines.
        "hook_last_fired": hook_fired_at,
        "hook_last_fired_age_s": hook_fired_age,
    }


def wav_duration_seconds(data: bytes) -> float | None:
    """Duration of an upload IF it is a parseable PCM RIFF/WAV container, else None.

    Deliberately WAV-only and header-cheap: WAV is what the plugin scripts record, and its header
    states frame count and rate up front. Compressed codecs (whisper accepts many) reveal duration
    only by decoding — exactly the expensive work the duration cap exists to avoid — so those pass
    through on the byte cap alone, honestly unmeasured. Anything the stdlib parser rejects
    (non-PCM subtypes, truncated or crafted headers) is also passed through rather than refused.
    """
    if not data.startswith(b"RIFF"):
        return None
    try:
        with wave.open(io.BytesIO(data)) as handle:
            rate = handle.getframerate()
            return handle.getnframes() / rate if rate else None
    except (wave.Error, EOFError):
        return None


class TranscriptionTimeout(Exception):
    """A transcription outran its wall-clock budget and was abandoned with its slot given back."""


def _default_clock() -> float:
    """Monotonic seconds. Elapsed time is an INPUT here, injected so a test needs no sleep."""
    return time.monotonic()


def bounded_segments(
    segments, budget: float, clock: Callable[[], float] = _default_clock
) -> Iterator[object]:
    """Consume transcript segments until the wall clock passes `budget` seconds, then give up.

    The format-agnostic half of the /stt holding-time bound. wav_duration_seconds() can measure a
    WAV header and nothing else, so 25 MB of a compressed codec is admitted honestly unmeasured —
    and it can decode to hours of transcription holding one model slot. faster-whisper decodes
    LAZILY, as this generator is drawn from, so a deadline checked between segments bounds the hold
    whatever the codec.

    The check sits where a segment ARRIVES, so what gets abandoned is the work still to come. It is
    deliberately not checked after the last one: the exhaustion probe ends the loop before the
    check would run, so a transcription that is simply over is never failed for the moment it spent
    ending. What can still overshoot the budget is one segment's decoding, not one file's.

    A budget of 0 or less means no bound — the behaviour before this cap existed, kept for an
    operator who would rather wait than lose a long dictation.
    """
    if budget <= 0:
        yield from segments
        return
    deadline = clock() + budget
    for segment in segments:
        if clock() > deadline:
            raise TranscriptionTimeout(f"transcription outran its {budget:.0f} second budget")
        yield segment


def transcribe_upload(data: bytes, language: str) -> tuple[str, object]:
    """The blocking body of /stt — runs in the threadpool so the event loop stays responsive."""
    with tempfile.NamedTemporaryFile(suffix=".wav") as handle:
        handle.write(data)
        handle.flush()
        with model_slot(resolve_device()):  # whisper is the one model that follows the GPU
            segments, info = whisper().transcribe(
                handle.name, language=language, vad_filter=True, initial_prompt=STT_HINT
            )
            text = " ".join(
                segment.text.strip() for segment in bounded_segments(segments, STT_TIMEOUT)
            ).strip()
    return text, info


@app.post("/stt")
async def stt(request: Request, audio: UploadFile = File(...), language: str = "") -> JSONResponse:
    refusal = cross_site_error(request)
    if refusal is not None:
        return refusal
    language = (language or LANGUAGE).lower()
    data = await audio.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        return JSONResponse(
            {
                "error": f"audio upload larger than the {MAX_UPLOAD_BYTES} byte limit",
                "hint": "send a shorter clip, or raise VOICE_LOOP_MAX_UPLOAD_BYTES on the server",
            },
            status_code=413,
        )
    duration = wav_duration_seconds(data)
    if duration is not None and duration > MAX_STT_SECONDS:
        return JSONResponse(
            {
                "error": f"audio too long: {duration:.0f} seconds, the limit is {MAX_STT_SECONDS:.0f}",
                "hint": "send a shorter clip, or raise VOICE_LOOP_MAX_STT_SECONDS on the server",
            },
            status_code=413,
        )
    try:
        text, info = await run_in_threadpool(transcribe_upload, data, language)
    except TranscriptionTimeout:
        # The slot went back with the failed call (model_slot releases on the way out), so the
        # queue behind it moves on immediately — which is the whole point of the bound.
        log.warning("abandoned a transcription that outran the %.0f second budget", STT_TIMEOUT)
        return JSONResponse(
            {
                "error": f"transcription outran the {STT_TIMEOUT:.0f} second time budget "
                "and was abandoned",
                "hint": "send a shorter clip — an upload inside the byte cap can still be hours of "
                "compressed audio, which no header cap can see; or raise VOICE_LOOP_STT_TIMEOUT "
                "on the server",
            },
            status_code=503,
        )
    text = filter_transcript(text)
    return JSONResponse({"text": text, "language": info.language, "duration": info.duration})


def render_tts(text: str, language: str, speaker: str, engine: str = "") -> bytes:
    """The blocking body of /tts — synthesis plus WAV encoding, run in the threadpool.

    Holds ONE model slot for the whole blob — which is why /tts carries the small
    MAX_TTS_TEXT_BLOB cap while /tts/stream (a slot per chunk) carries the big one. The slot is
    taken and released INSIDE one call, so a fallback retry (which is a second call) acquires only
    after the failed primary's slot is back: a one-slot gate is never held twice for one request.
    That holds across engines too — the retry may well queue on the OTHER device.
    """
    sample_rate = engine_sample_rate(engine)
    pause = torch.zeros(int(sample_rate * PAUSE_SECONDS))
    pieces = []
    with model_slot(synthesis_device(engine)):
        for piece in synthesis_pieces(text, language, speaker, engine):
            pieces.append(piece)
            pieces.append(pause)
    wav = torch.cat(pieces) if pieces else torch.zeros(1)

    import soundfile as sf

    out = io.BytesIO()
    sf.write(out, wav.numpy(), sample_rate, format="WAV")
    return out.getvalue()


@app.post("/tts")
async def tts_endpoint(request: Request, payload: dict) -> Response:
    """One WAV blob, and a header naming the engine that produced it.

    `X-Voice-Loop-Engine: <engine>` on every success — `<engine> (fallback)` when the primary was
    broken and the configured fallback spoke instead. The failure itself stays in the server log:
    a client that asked for speech gets speech, and can see who gave it.
    """
    refusal = cross_site_error(request)
    if refusal is not None:
        return refusal
    text = (payload.get("text") or "").strip()
    language = (payload.get("language") or LANGUAGE).lower()
    if len(text) > MAX_TTS_TEXT_BLOB:
        return JSONResponse(
            {
                "error": f"text too long for a single blob: {len(text)} characters, "
                f"the limit is {MAX_TTS_TEXT_BLOB}",
                "hint": "long texts belong on /tts/stream, which synthesizes chunk by chunk instead "
                "of holding the synthesis slot for the whole blob; "
                "or raise VOICE_LOOP_MAX_TTS_TEXT_BLOB on the server",
            },
            status_code=400,
        )
    engine, is_fallback, error = resolve_engine(text, language)
    if error is not None:
        return error

    speaker = payload.get("speaker") or ""
    try:
        wav = await run_in_threadpool(render_tts, text, language, speaker, engine)
    except Exception:
        if is_fallback:
            raise  # already the fallback: the ordinary error path, nothing left to try
        alternative, refused = fallback_for(text, language)
        if refused is not None:
            log.exception(
                "the %s engine failed and %s cannot serve this request either", engine, fallback_engine(engine)
            )
            return refused
        if not alternative:
            raise
        log.exception("the %s engine failed — retrying this request on %s", engine, alternative)
        count_fallback()
        wav = await run_in_threadpool(render_tts, text, language, speaker, alternative)
        engine, is_fallback = alternative, True
    return Response(wav, media_type="audio/wav", headers={ENGINE_HEADER: engine_label(engine, is_fallback)})


def gated_pieces(pieces, device: str = ""):
    """Wrap a synthesis generator so each chunk takes — and RELEASES — one model slot on `device`.

    The release between chunks is the point: a long stream queues fairly behind other requests
    instead of holding the executor for its whole duration, which is what lets /tts/stream carry a
    much larger text cap than the /tts blob path.
    """
    iterator = iter(pieces)
    while True:
        with model_slot(device):
            try:
                piece = next(iterator)
            except StopIteration:
                return
        yield piece


def stream_fallback(text: str, language: str, sent: int, is_fallback: bool) -> str:
    """The engine a BROKEN stream may restart on, or "" — and after the first chunk, never.

    A client that already played chunk 0 cannot be handed a different voice mid-sentence, and the
    200 left with those first bytes anyway: from there today's terminal error event stands
    unchanged. Before the first chunk nothing has been committed, so the whole stream can simply be
    synthesized again on the other engine. A fallback that refuses the request is no restart
    either — a stream has no way left to say 400, so the error event says it.
    """
    if sent or is_fallback:
        return ""
    engine, _refused = fallback_for(text, language)
    return engine


@app.post("/tts/stream")
async def tts_stream_endpoint(request: Request, payload: dict) -> Response:
    """Same JSON body as /tts, but the audio leaves as it is synthesized.

    Server-sent events (see README for the exact contract): one `chunk` event per sentence chunk
    carrying a complete standalone WAV segment in base64, then a terminal `end` — or a terminal
    `error` if synthesis breaks mid-stream (the 200 status left with the first bytes, so a late
    failure cannot become a 500; it becomes the last event instead). Requests refused before
    synthesis starts return plain JSON errors, exactly like /tts.

    Who spoke is reported in the terminal `end` event (`"engine": "silero (fallback)"`) rather than
    in a header: the headers leave before the first chunk, i.e. before a mid-flight fallback could
    change the answer, and a new event type would break every existing reader. No engine header is
    set on this endpoint for exactly that reason — the `end` event is the honest place.

    Pacing parity with /tts: /tts inserts PAUSE_SECONDS of silence between sentence chunks, so
    every chunk after the FIRST carries that silence at its head. Leading (not trailing) silence
    keeps the first chunk's latency untouched and needs no lookahead — the audible result between
    two chunks is the same either way.
    """
    refusal = cross_site_error(request)
    if refusal is not None:
        return refusal
    text = (payload.get("text") or "").strip()
    language = (payload.get("language") or LANGUAGE).lower()
    if len(text) > MAX_TTS_TEXT:
        return JSONResponse(
            {
                "error": f"text too long: {len(text)} characters, the limit is {MAX_TTS_TEXT}",
                "hint": "split the text and send several requests, "
                "or raise VOICE_LOOP_MAX_TTS_TEXT on the server",
            },
            status_code=400,
        )
    engine, is_fallback, error = resolve_engine(text, language)
    if error is not None:
        return error

    speaker = payload.get("speaker") or ""

    def event(name: str, data: dict[str, object]) -> str:
        return f"event: {name}\ndata: {json.dumps(data)}\n\n"

    def stream():
        import soundfile as sf

        current, fell_back, sent = engine, is_fallback, 0
        while True:
            sample_rate = engine_sample_rate(current)  # 48 kHz Silero / 24 kHz XTTS — per ENGINE
            device = synthesis_device(current)  # and the queue is per ENGINE too
            pause = torch.zeros(int(sample_rate * PAUSE_SECONDS))
            try:
                for piece in gated_pieces(synthesis_pieces(text, language, speaker, current), device):
                    if sent:
                        piece = torch.cat([pause, piece])
                    out = io.BytesIO()
                    sf.write(out, piece.numpy(), sample_rate, format="WAV")
                    audio = base64.b64encode(out.getvalue()).decode("ascii")
                    yield event("chunk", {"index": sent, "audio": audio})
                    sent += 1
            except Exception as exc:
                # The failed generator is done and its model slot released with it, so restarting
                # here acquires the gate fresh instead of holding it twice for one request.
                alternative = stream_fallback(text, language, sent, fell_back)
                if alternative:
                    log.exception(
                        "the %s engine failed before the first chunk — restarting on %s", current, alternative
                    )
                    count_fallback()
                    current, fell_back = alternative, True
                    continue
                # The full exception stays in the server log; the client gets the class name at
                # most — str(exc) can carry paths, config values and other internals (CodeQL:
                # information exposure through an exception).
                log.exception("streaming synthesis failed after %d chunk(s)", sent)
                yield event("error", {"error": f"synthesis failed ({type(exc).__name__})", "chunks": sent})
                return
            yield event("end", {"chunks": sent, "engine": engine_label(current, fell_back)})
            return

    return StreamingResponse(stream(), media_type="text/event-stream")


def main() -> None:
    logging.basicConfig(level=os.environ.get("VOICE_LOOP_LOG_LEVEL", "INFO"))
    if HOST == "0.0.0.0":  # noqa: S104 — flagging the wide bind is the point
        log.warning(
            "VOICE_LOOP_HOST=0.0.0.0 — listening on EVERY network interface with NO authentication; "
            "anyone who can reach the port can transcribe and synthesize on this hardware "
            "(fine inside a container; on a bare host prefer the default 127.0.0.1 and an ssh tunnel)"
        )
    log.info("voice-loop server on %s:%s (language=%s, device=%s)", HOST, PORT, LANGUAGE, resolve_device())
    uvicorn.run(app, host=HOST, port=PORT)


if __name__ == "__main__":  # pragma: no cover - the one-line shell around the tested main()
    main()
