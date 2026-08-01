"""voice-loop speech server: POST /stt (faster-whisper) + POST /tts (Silero, RUAccent for Russian).

A single small FastAPI app you can run on a laptop (CPU) or on a box with a GPU and reach over your
LAN or an ssh tunnel. Everything is configured through the environment — see README.md:

    VOICE_LOOP_HOST          bind address                (default 127.0.0.1 — loopback only)
    VOICE_LOOP_PORT          port                        (default 8355)
    VOICE_LOOP_DEVICE        auto | cuda | cpu           (default auto)
    VOICE_LOOP_LANGUAGE      default language code       (default ru; see SILERO_VOICES below)
    VOICE_LOOP_STT_MODEL     faster-whisper model size   (default small)
    VOICE_LOOP_COMPUTE_TYPE  auto | float16 | int8 | ... (default auto: float16 on cuda, int8 on cpu)
    VOICE_LOOP_STT_HINT      optional lexicon hint biasing the recognizer toward your jargon
    VOICE_LOOP_TTS_MODEL     override the Silero model for the default language
    VOICE_LOOP_TTS_SPEAKER   override the default speaker
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
from fastapi.responses import JSONResponse, Response

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

HOST = os.environ.get("VOICE_LOOP_HOST", "127.0.0.1")
PORT = int(os.environ.get("VOICE_LOOP_PORT", "8355"))
DEVICE = os.environ.get("VOICE_LOOP_DEVICE", "auto")
LANGUAGE = os.environ.get("VOICE_LOOP_LANGUAGE", "ru").lower()
STT_MODEL = os.environ.get("VOICE_LOOP_STT_MODEL", "small")
COMPUTE_TYPE = os.environ.get("VOICE_LOOP_COMPUTE_TYPE", "auto")
STT_HINT = os.environ.get("VOICE_LOOP_STT_HINT", "") or None
TTS_MODEL_OVERRIDE = os.environ.get("VOICE_LOOP_TTS_MODEL", "")
TTS_SPEAKER_OVERRIDE = os.environ.get("VOICE_LOOP_TTS_SPEAKER", "")
USE_ACCENT = os.environ.get("VOICE_LOOP_ACCENT", "1") not in ("0", "false", "no")
STRESS_FILE = Path(
    os.environ.get("VOICE_LOOP_STRESS_FILE", "")
    or Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "voice-loop" / "stress.json"
)

TTS_SR = 48000
MAX_TTS_CHARS = 800  # Silero degrades past ~1000 characters per call
PAUSE_SECONDS = 0.4

# Lazily filled caches. Everything expensive is loaded on first use through a seam a test can
# replace — the loaders below take their dependencies from importable modules and torch.hub, both
# patchable, so the whole file is exercisable without a single model on disk.
_whisper = None
_tts: dict[str, object] = {}
_accent: dict[str, object] = {}
_stress: list[tuple[re.Pattern[str], str]] | None = None


def reset_caches() -> None:
    """Drop every lazily loaded model, accentuator and stress rule set.

    Used by the tests between cases, and by anyone who edits stress.json and wants it re-read
    without restarting the process.
    """
    global _whisper, _stress
    _whisper = None
    _stress = None
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


def acute_to_plus(text: str) -> str:
    """Combining acute typed by a human ("Сaха́р", "робо́та") -> Silero's '+' notation ("Сах+ар")."""
    return re.sub(r"([^\W\d_])́", r"+\1", text, flags=re.UNICODE)


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


def mark_stress(text: str, language: str) -> str:
    """Normalize stress marking to Silero's '+' notation. ORDER IS LOAD-BEARING (debugged live):

    1. combining acute typed by a human ("Сaха́р") -> '+' notation ("Сах+ар");
    2. the user's override dictionary (adds '+' to known proper names);
    3. the automatic accentuator LAST (RUAccent for ru, ukrainian-word-stress for uk) — and it must
       never see the already-'+'-marked tokens: it re-stresses them from ITS own dictionary and undoes
       the override. So the text is split on marked tokens and only the free segments are accentuated.

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
        parts = re.split(r"(\S*\+\S*)", text)
        for i, part in enumerate(parts):
            if "+" in part or not part.strip():
                continue
            try:
                parts[i] = engine(part)
            except Exception as exc:
                log.debug("accentuation failed on a segment (%s) — kept as is", exc)
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


@app.get("/health")
def health() -> dict[str, object]:
    return {
        "ok": True,
        "device": resolve_device(),
        "cuda": torch.cuda.is_available(),
        "language": LANGUAGE,
        "stt_model": STT_MODEL,
        "tts_languages": sorted(SILERO_VOICES),
        "accentuated_languages": sorted(ACCENTUATORS),
        "stt_loaded": _whisper is not None,
        "tts_loaded": sorted(_tts),
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
    return JSONResponse({"text": text, "language": info.language, "duration": info.duration})


@app.post("/tts")
async def tts_endpoint(payload: dict) -> Response:
    text = (payload.get("text") or "").strip()
    language = (payload.get("language") or LANGUAGE).lower()
    if not text:
        return JSONResponse({"error": "empty text"}, status_code=400)
    if language not in SILERO_VOICES:
        return JSONResponse(
            {
                "error": f"no local TTS model for language {language!r}",
                "supported": sorted(SILERO_VOICES),
                "hint": "use a cloud TTS backend for this language, or add its Silero model to SILERO_VOICES",
            },
            status_code=400,
        )
    speaker = payload.get("speaker") or default_speaker(language)
    text = mark_stress(text, language)

    pause = torch.zeros(int(TTS_SR * PAUSE_SECONDS))
    pieces = []
    for part in chunk(text):
        pieces.append(tts(language).apply_tts(text=part, speaker=speaker, sample_rate=TTS_SR))
        pieces.append(pause)
    wav = torch.cat(pieces) if pieces else torch.zeros(1)

    import soundfile as sf

    out = io.BytesIO()
    sf.write(out, wav.numpy(), TTS_SR, format="WAV")
    return Response(out.getvalue(), media_type="audio/wav")


def main() -> None:
    logging.basicConfig(level=os.environ.get("VOICE_LOOP_LOG_LEVEL", "INFO"))
    log.info("voice-loop server on %s:%s (language=%s, device=%s)", HOST, PORT, LANGUAGE, resolve_device())
    uvicorn.run(app, host=HOST, port=PORT)


if __name__ == "__main__":  # pragma: no cover - the one-line shell around the tested main()
    main()
