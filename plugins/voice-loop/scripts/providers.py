#!/usr/bin/env python3
"""voice-loop — the speech PROVIDER REGISTRY: one entry per provider, per direction.

Why it exists. Before this module the provider was a two-armed ``if`` in four places
(``dictate.resolve_settings``, ``dictate._transcribe_cloud``, ``speak.resolve_settings``,
``speak.synthesize``) and a fifth in ``tls-probe.resolve_url``. Adding a third provider meant
editing five branches in three files and hoping none was missed — and the ones that were missed
fail QUIETLY: a cloud STT call that lands on the wrong arm degrades to local whisper under a log
line that says the cloud failed. So: **a provider is an ENTRY, never a branch.** Adding one is a
new row in ``STT_PROVIDERS`` / ``TTS_PROVIDERS`` and nothing else; no dispatch path in this plugin
compares a configured provider name against a literal, and a test in ``tests/test_providers.py``
holds that line by grep.

The two directions have genuinely different shapes and are NOT forced into one table with dead
columns: an STT entry parses a transcript out of a response and reads an error document (the
cloud→local degrade needs a reason); a TTS entry gets audio bytes back and owns an audio-container
knob instead. One module, two entry shapes.

The axes that vary per provider, all seven of them in the entry and nowhere else:

1. **default model** — ``default_model``
2. **request build** — host, URL path, body encoding, and where ``language`` goes (a form field for
   OpenAI, ignored by ElevenLabs today (windowsill#93), a query parameter for Deepgram)
3. **auth** — the header name and its scheme (``Authorization: Bearer``, ``xi-api-key``,
   ``Authorization: Token``)
4. **response parse** — where the transcript lives in the body
5. **credential resolution** — ``key_env_fallbacks``, the "one credentials home" rule
6. **error-document reading** — ``error_summary``, so a degrade names a real reason
7. **the remote default host** — ``default_host``; "" means "this provider has none, use the
   configured endpoint", which is what makes the TLS probe's old ElevenLabs special case go away

This is the one module in ``scripts/`` that the other scripts import, so the "a single file can be
copied out and still run" property now means "that file plus ``providers.py``". The launchers
(``speak.sh``, ``dictate-toggle.sh``) check for it the same way they check for their own .py.

Stdlib only, Python 3.10+. Nothing here does I/O: an entry BUILDS a request and READS a decoded
body; the scripts own the sockets, the logging and the degrade decision.

The user-facing comparison — latency, cost per minute, language coverage (Russian and Ukrainian
both matter, cf. windowsill#47) and privacy posture — is the ``Comparison`` on each entry, rendered
for humans in ``PROVIDERS.md`` next to this file.
"""

from __future__ import annotations

import json
import urllib.parse
from dataclasses import dataclass
from typing import Callable

# --- remote default hosts ------------------------------------------------------------------------

ELEVENLABS_HOST = "https://api.elevenlabs.io"
DEEPGRAM_HOST = "https://api.deepgram.com"

# Where the OpenAI-compatible path lands when nothing is configured: this plugin's own server, on
# loopback and on http. It is deliberately NOT a `default_host` — a provider's default_host is a
# REMOTE host, which is exactly the distinction the TLS probe branches on.
LOCAL_SPEECH_HOST = "http://127.0.0.1:8355"

# The provider a config that names none — or names one nobody has heard of — resolves to. Same
# value both directions, and the same one the two-branch code defaulted to before this module.
DEFAULT_STT = "openai"
DEFAULT_TTS = "openai"


# --- what an entry hands back --------------------------------------------------------------------


@dataclass(frozen=True)
class SttRequest:
    """One transcription POST, fully built — the script only opens the socket."""

    url: str
    headers: dict[str, str]
    body: bytes
    content_type: str


@dataclass(frozen=True)
class TtsRequest:
    """One synthesis POST. Every provider on the shelf takes a JSON body, so the payload stays a
    dict and the script encodes it — the container the AUDIO comes back in is the varying part."""

    url: str
    headers: dict[str, str]
    payload: dict


@dataclass(frozen=True)
class Comparison:
    """What a user picks a provider FROM. Kept beside the code rather than in an issue comment,
    and rendered as a table in PROVIDERS.md — which a test holds in sync with this registry.

    Every field is prose on purpose: a number without its date and tier is a number that goes
    stale silently. PROVIDERS.md carries the date and the "check the vendor's page" caveat."""

    latency: str
    cost: str
    languages: str
    privacy: str


# --- the entry shapes ----------------------------------------------------------------------------


@dataclass(frozen=True)
class SttProvider:
    """One speech-to-text provider. See the module docstring for the seven axes."""

    name: str
    default_model: str
    # "" = no remote default; the configured endpoint is used, which for STT is stt.endpoint
    default_host: str
    # env var names tried AFTER the configured one, in order — the "one credentials home" rule
    key_env_fallbacks: tuple[str, ...]
    build: Callable[["SttProvider", dict, str, bytes, str], SttRequest]
    transcript: Callable[[object], str]
    error_summary: Callable[[object], str]
    comparison: Comparison

    def endpoint(self, s: dict) -> str:
        """The host this call goes to: an explicit stt.cloud.endpoint wins, then the provider's own
        remote default, then whatever stt.endpoint is (the OpenAI-compatible path's behaviour)."""
        return str(s.get("cloud_endpoint") or self.default_host or s.get("endpoint", ""))

    def key_envs(self, configured: str) -> tuple[str, ...]:
        """Every env var name this provider will accept a key from, most-specific first."""
        return (configured, *self.key_env_fallbacks)

    def request(self, s: dict, key: str, wav_bytes: bytes, boundary: str) -> SttRequest:
        return self.build(self, s, key, wav_bytes, boundary)


@dataclass(frozen=True)
class TtsProvider:
    """One text-to-speech provider.

    No degrade axis and no response parse: synthesis either hands back audio bytes or it does not,
    and the "is this an error document?" check is one shared rule (a body that starts with a brace)
    rather than a per-provider one. What it has instead is ``default_output_format`` — the audio
    CONTAINER, which the user's ``speak.player`` has to be able to play, and whose spelling is
    provider-private (ElevenLabs takes ``mp3_44100_128``; Deepgram takes query parameters)."""

    name: str
    default_model: str
    default_host: str
    default_output_format: str
    build: Callable[["TtsProvider", dict, str, str], TtsRequest]
    comparison: Comparison

    def endpoint(self, s: dict) -> str:
        """An explicit tts.endpoint wins, then the provider's remote default, then this plugin's
        own server on loopback."""
        return str(s.get("endpoint") or self.default_host or LOCAL_SPEECH_HOST)

    def request(self, s: dict, key: str, text: str) -> TtsRequest:
        return self.build(self, s, key, text)


# --- shared readers ------------------------------------------------------------------------------


def decode(raw: bytes | None) -> object:
    """A response body -> a python object, or None when there is nothing decodable in it.

    None covers all three "no document" cases identically — no body, an empty body, and a body that
    is not JSON at all (an HTML error page from a proxy) — so every parser below can be written for
    the shape it expects and nothing else."""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def text_field(data: object) -> str:
    """``{"text": "..."}`` — OpenAI's transcription shape, ElevenLabs Scribe's, and this plugin's
    own server's. '' for anything else, which is what makes a malformed body a degrade rather than
    a traceback."""
    return str(data.get("text", "")).strip() if isinstance(data, dict) else ""


def _deepgram_transcript(data: object) -> str:
    """Deepgram nests the transcript under the first alternative of the first channel:
    ``results.channels[0].alternatives[0].transcript``. Every way that walk can fail — a dict that
    is an error document, an empty channel list — reads as "no transcript", i.e. degrade."""
    try:
        return str(data["results"]["channels"][0]["alternatives"][0]["transcript"]).strip()  # type: ignore[index]
    except (TypeError, KeyError, IndexError):
        return ""


def _detail_or_document(data: object) -> str:
    """OpenAI and ElevenLabs both put the human-readable reason in ``detail``; a document without
    one travels as itself, truncated. The isinstance guard is load-bearing: a JSON body that is a
    LIST decodes fine and has no ``.get``."""
    if isinstance(data, dict):
        return str(data.get("detail", data))[:200]
    return str(data)[:200]


def _deepgram_error(data: object) -> str:
    """Deepgram's error documents are ``{"err_code": ..., "err_msg": ...}`` on the API surface and
    ``{"error": ...}`` on some edges; the first populated one wins, then the whole document."""
    if not isinstance(data, dict):
        return str(data)[:200]
    for key in ("err_msg", "error", "reason", "message"):
        if data.get(key):
            return str(data[key])[:200]
    return str(data)[:200]


def _multipart_type(boundary: str) -> str:
    return f"multipart/form-data; boundary={boundary}"


def multipart_form(fields: dict[str, str], file_field: str, filename: str, payload: bytes, boundary: str) -> bytes:
    """A multipart/form-data body the way curl -F built it: text fields, then one WAV part.

    Lives here rather than in dictate.py because it is the request-BUILD axis's shared helper and
    the entries below are what call it; dictate.py keeps a module-level alias for its own callers."""
    lines: list[str] = []
    for name, value in fields.items():
        lines += [f"--{boundary}", f'Content-Disposition: form-data; name="{name}"', "", value]
    lines += [
        f"--{boundary}",
        f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"',
        "Content-Type: audio/wav",
        "",
    ]
    head = "\r\n".join(lines).encode("utf-8") + b"\r\n"
    return head + payload + f"\r\n--{boundary}--\r\n".encode("ascii")


# --- STT request builders ------------------------------------------------------------------------


def _openai_stt(entry: SttProvider, s: dict, key: str, wav_bytes: bytes, boundary: str) -> SttRequest:
    """The OpenAI speech-to-text API (and anything that speaks its shape): multipart, the model and
    the language as form fields, a bearer token."""
    body = multipart_form(
        {"model": s["stt_model"], "language": s["language"]}, "file", "dictate.wav", wav_bytes, boundary
    )
    return SttRequest(
        f"{entry.endpoint(s)}/v1/audio/transcriptions",
        {"Authorization": f"Bearer {key}"},
        body,
        _multipart_type(boundary),
    )


def _elevenlabs_stt(entry: SttProvider, s: dict, key: str, wav_bytes: bytes, boundary: str) -> SttRequest:
    """ElevenLabs Scribe: multipart with ``model_id``, and no language field at all — Scribe
    auto-detects, so a configured ``stt.language`` does not reach it (windowsill#93)."""
    body = multipart_form({"model_id": s["stt_model"]}, "file", "dictate.wav", wav_bytes, boundary)
    return SttRequest(
        f"{entry.endpoint(s)}/v1/speech-to-text",
        {"xi-api-key": key},
        body,
        _multipart_type(boundary),
    )


def _deepgram_stt(entry: SttProvider, s: dict, key: str, wav_bytes: bytes, boundary: str) -> SttRequest:
    """Deepgram Listen: NOT multipart — the WAV is the whole request body, and every knob is a
    query parameter. ``smart_format`` is what puts punctuation and capitalisation in a dictated
    line; without it the transcript arrives as one lowercase run.

    ``boundary`` is unused here on purpose: it is part of the caller's contract, not of every
    provider's encoding, and that asymmetry is exactly what a registry is for."""
    query = urllib.parse.urlencode(
        {"model": s["stt_model"], "language": s["language"], "smart_format": "true"}
    )
    return SttRequest(
        f"{entry.endpoint(s)}/v1/listen?{query}",
        {"Authorization": f"Token {key}"},
        wav_bytes,
        "audio/wav",
    )


# --- TTS request builders ------------------------------------------------------------------------


def _openai_tts(entry: TtsProvider, s: dict, key: str, text: str) -> TtsRequest:
    """The OpenAI speech API: JSON in, WAV out, a bearer token. ``alloy`` is its stock voice and
    the request is invalid without one, so an unset speaker resolves to it here."""
    return TtsRequest(
        f"{entry.endpoint(s)}/v1/audio/speech",
        {"Authorization": f"Bearer {key}"},
        {
            "model": s["cloud_model"],
            "voice": s["voice_id"] or "alloy",
            "input": text,
            "response_format": "wav",
        },
    )


def _elevenlabs_tts(entry: TtsProvider, s: dict, key: str, text: str) -> TtsRequest:
    """ElevenLabs text-to-speech: the voice is in the PATH, the container is one opaque
    ``output_format`` token, and the anti-robovoice knobs ride as ``voice_settings`` when set."""
    payload: dict = {"text": text, "model_id": s["cloud_model"]}
    if s["voice_settings"] is not None:
        payload["voice_settings"] = s["voice_settings"]
    return TtsRequest(
        f"{entry.endpoint(s)}/v1/text-to-speech/{s['voice_id']}?output_format={s['output_format']}",
        {"xi-api-key": key},
        payload,
    )


def _deepgram_tts(entry: TtsProvider, s: dict, key: str, text: str) -> TtsRequest:
    """Deepgram Speak: the voice IS the model (``aura-2-thalia-en``), and the container is a pair of
    query parameters rather than one token — so this provider's ``output_format`` value is a raw
    query fragment (``encoding=linear16&container=wav``), which is why the default lives on the
    entry instead of in speak.py. The default is WAV precisely because the documented Linux player
    (``aplay``) cannot play mp3."""
    parts = [f"model={urllib.parse.quote(s['cloud_model'])}"]
    if s["output_format"]:
        parts.append(s["output_format"])
    return TtsRequest(
        f"{entry.endpoint(s)}/v1/speak?{'&'.join(parts)}",
        {"Authorization": f"Token {key}"},
        {"text": text},
    )


# --- the registry --------------------------------------------------------------------------------
#
# One entry per provider. Adding a provider is a row here plus its builder above — no dispatch
# path in this plugin learns its name. The Comparison prose is the user-facing half; PROVIDERS.md
# renders it and a test holds the two in sync.

STT_PROVIDERS: dict[str, SttProvider] = {
    "openai": SttProvider(
        name="openai",
        default_model="whisper-1",
        default_host="",
        key_env_fallbacks=(),
        build=_openai_stt,
        transcript=text_field,
        error_summary=_detail_or_document,
        comparison=Comparison(
            latency="a second or two for a short clip; the whole clip is uploaded before work starts",
            cost="whisper-1 list price $0.006/min",
            languages="~99 languages including Russian and Ukrainian",
            privacy="audio leaves the machine; retention follows the account's API data policy",
        ),
    ),
    "elevenlabs": SttProvider(
        name="elevenlabs",
        default_model="scribe_v1",
        default_host=ELEVENLABS_HOST,
        # One credentials home: a user who already configured /voice-design for TTS has dictation
        # working without a second key.
        key_env_fallbacks=("VOICE_LOOP_TTS_API_KEY",),
        build=_elevenlabs_stt,
        transcript=text_field,
        error_summary=_detail_or_document,
        comparison=Comparison(
            latency="a second or two for a short clip; accuracy-first rather than latency-first",
            cost="Scribe list price ≈$0.40/hour (≈$0.0067/min) on the paid tiers",
            languages="99 languages including Russian and Ukrainian; auto-detected, stt.language is ignored (#93)",
            privacy="audio leaves the machine; zero-retention is an account/enterprise setting",
        ),
    ),
    "deepgram": SttProvider(
        name="deepgram",
        default_model="nova-3",
        default_host=DEEPGRAM_HOST,
        key_env_fallbacks=(),
        build=_deepgram_stt,
        transcript=_deepgram_transcript,
        error_summary=_deepgram_error,
        comparison=Comparison(
            latency="the fastest of the three on short clips; streaming exists but this plugin posts whole clips",
            cost="nova-3 pre-recorded list price ≈$0.0043/min; new accounts start with a $200 credit",
            languages="Russian via nova-3 multilingual (stt.language: \"multi\"); "
            "Ukrainian needs stt.model: \"nova-2\" — check the vendor's model/language matrix",
            privacy="audio leaves the machine; self-hosted deployment is offered, and this plugin's "
            "stt.cloud.endpoint points at one",
        ),
    ),
}

TTS_PROVIDERS: dict[str, TtsProvider] = {
    "openai": TtsProvider(
        name="openai",
        default_model="tts-1",
        default_host="",
        default_output_format="",
        build=_openai_tts,
        comparison=Comparison(
            latency="under a second to first byte for a short line",
            cost="tts-1 list price $15 per 1M characters",
            languages="multilingual including Russian and Ukrainian, in an English-accented voice",
            privacy="text leaves the machine; retention follows the account's API data policy",
        ),
    ),
    "elevenlabs": TtsProvider(
        name="elevenlabs",
        default_model="eleven_multilingual_v2",
        default_host=ELEVENLABS_HOST,
        # mp3 by default — your speak.player must be able to play it (macOS afplay does; on Linux
        # use mpg123 or ffplay). The value is one opaque ElevenLabs token.
        default_output_format="mp3_44100_128",
        build=_elevenlabs_tts,
        comparison=Comparison(
            latency="around a second to first byte; flash models are quicker at some quality cost",
            cost="credit-based; the character rate depends on the plan tier",
            languages="29+ languages including Russian and Ukrainian; voice cloning via /voice-design",
            privacy="text leaves the machine; designed voices live in the user's ElevenLabs account",
        ),
    ),
    "deepgram": TtsProvider(
        name="deepgram",
        default_model="aura-2-thalia-en",
        default_host=DEEPGRAM_HOST,
        # A raw query fragment, not an opaque token — see _deepgram_tts. WAV because aplay, the
        # documented Linux player, cannot play mp3.
        default_output_format="encoding=linear16&container=wav",
        build=_deepgram_tts,
        comparison=Comparison(
            latency="the lowest first-byte latency of the three",
            cost="Aura list price ≈$0.030 per 1k characters; the $200 new-account credit covers it too",
            languages="ENGLISH ONLY on Aura-2 (plus Spanish) — no Russian, no Ukrainian. "
            "Not a choice for a Russian- or Ukrainian-speaking contour",
            privacy="text leaves the machine; self-hosted deployment is offered",
        ),
    ),
}


def stt_provider(name: str) -> SttProvider | None:
    """The entry for a configured ``stt.cloud.provider``, or None when nobody has heard of it.

    None rather than a default so the CALLER decides what an unknown name means and can say so in
    the log — a silent fall-through to OpenAI is how a typo becomes a mystery."""
    return STT_PROVIDERS.get(name)


def tts_provider(name: str) -> TtsProvider | None:
    """The entry for a configured ``tts.cloud.provider``, or None — see stt_provider."""
    return TTS_PROVIDERS.get(name)


def _validate_registry() -> None:
    """Run at import (ADR-shaped: a vocabulary other artifacts cite by id validates its own rows).

    Everything here is a mistake that would otherwise surface as a runtime 404 or a request built
    against the wrong host, in a path whose failure mode is a quiet degrade."""
    for table in (STT_PROVIDERS, TTS_PROVIDERS):
        for key, entry in table.items():
            if key != entry.name:
                raise ValueError(f"provider registry: row {key!r} carries name {entry.name!r}")
            if not entry.default_model:
                raise ValueError(f"provider registry: {key!r} has no default model")
            if entry.default_host and not entry.default_host.startswith("https://"):
                raise ValueError(f"provider registry: {key!r} has a non-https default host")
            missing = [f for f in ("latency", "cost", "languages", "privacy") if not getattr(entry.comparison, f)]
            if missing:
                raise ValueError(f"provider registry: {key!r} has an empty comparison field {missing[0]}")
    if DEFAULT_STT not in STT_PROVIDERS:
        raise ValueError(f"provider registry: the default STT provider {DEFAULT_STT!r} has no entry")
    if DEFAULT_TTS not in TTS_PROVIDERS:
        raise ValueError(f"provider registry: the default TTS provider {DEFAULT_TTS!r} has no entry")


_validate_registry()
