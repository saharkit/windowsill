"""voice-loop speech server: POST /stt (faster-whisper) + POST /tts and /tts/stream (Silero or XTTS).

A single small FastAPI app you can run on a laptop (CPU) or on a box with a GPU and reach over your
LAN or an ssh tunnel. Everything is configured through the environment — see README.md:

    VOICE_LOOP_HOST          bind address                (default 127.0.0.1 — loopback only)
    VOICE_LOOP_PORT          port                        (default 8355)
    VOICE_LOOP_DEVICE        auto | cuda | cpu           (default auto)
    VOICE_LOOP_LANGUAGE      default language code       (default ru; see SILERO_VOICES below)
    VOICE_LOOP_STT_MODEL     faster-whisper model size   (default small)
    VOICE_LOOP_COMPUTE_TYPE  auto | float16 | int8 | ... (default auto: float16 on cuda, int8 on cpu)
    VOICE_LOOP_STT_HINT      optional lexicon hint biasing the recognizer toward your jargon
    VOICE_LOOP_TTS_ENGINE    silero | xtts               (default silero; xtts = XTTS-v2 voice cloning)
    VOICE_LOOP_TTS_MODEL     override the Silero model for the default language
    VOICE_LOOP_TTS_SPEAKER   override the default speaker
    VOICE_LOOP_XTTS_REFERENCE  wav of the voice to clone (the xtts engine refuses requests without it)
    VOICE_LOOP_XTTS_MODEL_DIR  local XTTS-v2 model dir   (optional; default: coqui's own download cache)
    VOICE_LOOP_STRESS_FILE   stress overrides            (default ~/.config/voice-loop/stress.json)
    VOICE_LOOP_ACCENT        1 | 0 — enable automatic accentuation (default 1; ru and uk)

Language is a request-level field: /stt takes ?language=, /tts takes {"language": ...}. Both fall back
to VOICE_LOOP_LANGUAGE. STT (whisper) is multilingual out of the box; TTS is limited to the languages
Silero ships a model for — an unsupported code returns 400 with the supported list.

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
from pathlib import Path

import torch
import uvicorn
from fastapi import FastAPI, File, UploadFile
from fastapi.responses import JSONResponse, Response, StreamingResponse

MIN_PYTHON = (3, 10)


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

HOST = os.environ.get("VOICE_LOOP_HOST", "127.0.0.1")
PORT = int(os.environ.get("VOICE_LOOP_PORT", "8355"))
DEVICE = os.environ.get("VOICE_LOOP_DEVICE", "auto")
LANGUAGE = os.environ.get("VOICE_LOOP_LANGUAGE", "ru").lower()
STT_MODEL = os.environ.get("VOICE_LOOP_STT_MODEL", "small")
COMPUTE_TYPE = os.environ.get("VOICE_LOOP_COMPUTE_TYPE", "auto")
STT_HINT = os.environ.get("VOICE_LOOP_STT_HINT", "") or None
TTS_ENGINE = os.environ.get("VOICE_LOOP_TTS_ENGINE", "silero").lower()
TTS_MODEL_OVERRIDE = os.environ.get("VOICE_LOOP_TTS_MODEL", "")
TTS_SPEAKER_OVERRIDE = os.environ.get("VOICE_LOOP_TTS_SPEAKER", "")
XTTS_REFERENCE = os.environ.get("VOICE_LOOP_XTTS_REFERENCE", "")
XTTS_MODEL_DIR = os.environ.get("VOICE_LOOP_XTTS_MODEL_DIR", "")
USE_ACCENT = os.environ.get("VOICE_LOOP_ACCENT", "1") not in ("0", "false", "no")
STRESS_FILE = Path(
    os.environ.get("VOICE_LOOP_STRESS_FILE", "")
    or Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "voice-loop" / "stress.json"
)

TTS_SR = 48000
XTTS_SR = 24000  # XTTS-v2 always synthesizes at 24 kHz
MAX_TTS_CHARS = 800  # Silero degrades past ~1000 characters per call
PAUSE_SECONDS = 0.4

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
_accent: dict[str, object] = {}
_stress: list[tuple[re.Pattern[str], str]] | None = None
_hallucinations: list[str] | None = None
_hallucinations_dropped = 0


def reset_caches() -> None:
    """Drop every lazily loaded model, accentuator, stress rule set and hallucination blocklist.

    Used by the tests between cases, and by anyone who edits stress.json or the hallucination
    blocklist and wants it re-read without restarting the process.
    """
    global _whisper, _xtts, _stress, _hallucinations, _hallucinations_dropped
    _whisper = None
    _xtts = None
    _stress = None
    _hallucinations = None
    _hallucinations_dropped = 0
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
    """ukrainian-word-stress: dictionary-based accentuation; it emits combining acute, so normalize."""
    from ukrainian_word_stress import Stressifier

    stressify = Stressifier()

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


def xtts():
    """Lazily loaded XTTS-v2 voice cloner, cached; reset_caches() drops it like every other model.

    The weights are downloaded by coqui-tts on the USER's first request (they are CPML-licensed —
    set COQUI_TOS_AGREED=1 to accept), never bundled with this repo. A GPU too small for the model
    (~2-2.5 GB of VRAM) degrades to CPU instead of failing the request.
    """
    global _xtts
    if _xtts is None:
        device = resolve_device()
        try:
            _xtts = _load_xtts(device)
        except torch.cuda.OutOfMemoryError:
            log.warning("XTTS-v2 does not fit on the GPU — retrying on CPU")
            device = "cpu"
            _xtts = _load_xtts(device)
        log.info("loaded XTTS-v2 on %s", device)
    return _xtts


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


def chunk(text: str, limit: int = MAX_TTS_CHARS) -> list[str]:
    """Split on sentence boundaries so no single synthesis call exceeds the model's comfortable length."""
    chunks: list[str] = []
    buf = ""
    for sentence in re.split(r"(?<=[.!?…])\s+", text):
        if len(buf) + len(sentence) + 1 > limit and buf:
            chunks.append(buf)
            buf = sentence
        else:
            buf = f"{buf} {sentence}".strip()
    if buf:
        chunks.append(buf)
    return chunks


def xtts_request_error(language: str) -> JSONResponse | None:
    """Why an xtts request cannot be served right now, or None.

    Checked per request ON PURPOSE: the server must still boot and serve /stt with coqui-tts absent
    or the reference unset — a broken TTS engine is a request-level 500, never a startup failure.
    """
    try:
        from TTS.api import TTS  # noqa: F401 — availability probe only; xtts() does the real import
    except ImportError:
        return JSONResponse(
            {
                "error": "the xtts engine needs the optional coqui-tts package, which is not installed",
                "hint": "pip install coqui-tts — its XTTS-v2 weights are CPML-licensed (non-commercial) "
                "and download on first use; set COQUI_TOS_AGREED=1 to accept",
            },
            status_code=500,
        )
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
        return JSONResponse(
            {"error": f"XTTS-v2 does not speak language {language!r}", "supported": sorted(XTTS_LANGUAGES)},
            status_code=400,
        )
    return None


def tts_request_error(text: str, language: str) -> JSONResponse | None:
    """The shared /tts + /tts/stream refusal, or None when the request can be synthesized."""
    if not text:
        return JSONResponse({"error": "empty text"}, status_code=400)
    if TTS_ENGINE == "xtts":
        return xtts_request_error(language)
    if TTS_ENGINE != "silero":
        return JSONResponse(
            {"error": f"unknown TTS engine {TTS_ENGINE!r} (VOICE_LOOP_TTS_ENGINE)", "supported": ["silero", "xtts"]},
            status_code=500,
        )
    if language not in SILERO_VOICES:
        return JSONResponse(
            {
                "error": f"no local TTS model for language {language!r}",
                "supported": sorted(SILERO_VOICES),
                "hint": "use a cloud TTS backend for this language, or add its Silero model to SILERO_VOICES",
            },
            status_code=400,
        )
    return None


def engine_sample_rate() -> int:
    if TTS_ENGINE == "xtts":
        return XTTS_SR
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


def synthesis_pieces(text: str, language: str, speaker: str = ""):
    """One synthesized tensor per sentence chunk from the configured engine — the shared core of
    /tts (which concatenates them) and /tts/stream (which ships each one as it appears)."""
    if TTS_ENGINE == "xtts":
        return xtts_pieces(text, language)
    return silero_pieces(text, language, speaker)


@app.get("/health")
def health() -> dict[str, object]:
    return {
        "ok": True,
        "device": resolve_device(),
        "cuda": torch.cuda.is_available(),
        "language": LANGUAGE,
        "stt_model": STT_MODEL,
        "tts_engine": TTS_ENGINE,
        "tts_languages": sorted(SILERO_VOICES),
        "accentuated_languages": sorted(ACCENTUATORS),
        "streaming": True,
        "stt_loaded": _whisper is not None,
        "tts_loaded": sorted(_tts),
        "xtts_loaded": _xtts is not None,
        "stt_hallucinations_dropped": _hallucinations_dropped,
    }


@app.post("/stt")
async def stt(audio: UploadFile = File(...), language: str = "") -> JSONResponse:
    language = (language or LANGUAGE).lower()
    data = await audio.read()
    with tempfile.NamedTemporaryFile(suffix=".wav") as handle:
        handle.write(data)
        handle.flush()
        segments, info = whisper().transcribe(
            handle.name, language=language, vad_filter=True, initial_prompt=STT_HINT
        )
        text = " ".join(segment.text.strip() for segment in segments).strip()
    pattern = matched_hallucination(text)
    if pattern is not None:
        global _hallucinations_dropped
        _hallucinations_dropped += 1
        log.info("dropped a hallucinated transcript %r (matched %r)", text, pattern)
        text = ""
    return JSONResponse({"text": text, "language": info.language, "duration": info.duration})


@app.post("/tts")
async def tts_endpoint(payload: dict) -> Response:
    text = (payload.get("text") or "").strip()
    language = (payload.get("language") or LANGUAGE).lower()
    error = tts_request_error(text, language)
    if error is not None:
        return error

    sample_rate = engine_sample_rate()
    pause = torch.zeros(int(sample_rate * PAUSE_SECONDS))
    pieces = []
    for piece in synthesis_pieces(text, language, payload.get("speaker") or ""):
        pieces.append(piece)
        pieces.append(pause)
    wav = torch.cat(pieces) if pieces else torch.zeros(1)

    import soundfile as sf

    out = io.BytesIO()
    sf.write(out, wav.numpy(), sample_rate, format="WAV")
    return Response(out.getvalue(), media_type="audio/wav")


@app.post("/tts/stream")
async def tts_stream_endpoint(payload: dict) -> Response:
    """Same JSON body as /tts, but the audio leaves as it is synthesized.

    Server-sent events (see README for the exact contract): one `chunk` event per sentence chunk
    carrying a complete standalone WAV segment in base64, then a terminal `end` — or a terminal
    `error` if synthesis breaks mid-stream (the 200 status left with the first bytes, so a late
    failure cannot become a 500; it becomes the last event instead). Requests refused before
    synthesis starts return plain JSON errors, exactly like /tts.
    """
    text = (payload.get("text") or "").strip()
    language = (payload.get("language") or LANGUAGE).lower()
    error = tts_request_error(text, language)
    if error is not None:
        return error

    sample_rate = engine_sample_rate()
    pieces = synthesis_pieces(text, language, payload.get("speaker") or "")

    def event(name: str, data: dict[str, object]) -> str:
        return f"event: {name}\ndata: {json.dumps(data)}\n\n"

    def stream():
        import soundfile as sf

        sent = 0
        try:
            for piece in pieces:
                out = io.BytesIO()
                sf.write(out, piece.numpy(), sample_rate, format="WAV")
                audio = base64.b64encode(out.getvalue()).decode("ascii")
                yield event("chunk", {"index": sent, "audio": audio})
                sent += 1
        except Exception as exc:
            # The full exception stays in the server log; the client gets the class name at most —
            # str(exc) can carry paths, config values and other internals (CodeQL: information
            # exposure through an exception).
            log.exception("streaming synthesis failed after %d chunk(s)", sent)
            yield event("error", {"error": f"synthesis failed ({type(exc).__name__})", "chunks": sent})
            return
        yield event("end", {"chunks": sent})

    return StreamingResponse(stream(), media_type="text/event-stream")


def main() -> None:
    logging.basicConfig(level=os.environ.get("VOICE_LOOP_LOG_LEVEL", "INFO"))
    log.info("voice-loop server on %s:%s (language=%s, device=%s)", HOST, PORT, LANGUAGE, resolve_device())
    uvicorn.run(app, host=HOST, port=PORT)


if __name__ == "__main__":  # pragma: no cover - the one-line shell around the tested main()
    main()
