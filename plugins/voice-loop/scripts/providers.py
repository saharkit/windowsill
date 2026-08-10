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
2. **request build** — host, URL path, body encoding, and where ``language`` goes (a ``language``
   form field for OpenAI, a ``language_code`` form field for ElevenLabs, a query parameter for
   Deepgram)
3. **auth** — the header name and its scheme (``Authorization: Bearer``, ``xi-api-key``,
   ``Authorization: Token``)
4. **response parse** — where the transcript lives in the body
5. **credential resolution** — ``key_env_fallbacks``, the "one credentials home" rule
6. **error-document reading** — ``error_summary``, so a degrade names a real reason
7. **the remote default host** — ``default_host``; "" means "this provider has none, use the
   configured endpoint", which is what makes the TLS probe's old ElevenLabs special case go away

This is the module the other scripts import, so the "a single file can be copied out and still run"
property now means "that file plus ``providers.py``" — and, for the dictation toggle since the
streaming variant landed, plus ``wsclient.py``. The launchers (``speak.sh``, ``dictate-toggle.sh``)
check for what they need the same way they check for their own .py.

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
class StreamResult:
    """One transcript update off a streaming socket.

    ``is_final`` is the whole distinction the caller cares about: an INTERIM result is a guess that
    the next message may rewrite, and a FINAL one is a settled span that will never be sent again.
    Assembling finals in arrival order reconstructs the dictation; assembling interims would
    duplicate half of it."""

    text: str
    is_final: bool


@dataclass(frozen=True)
class StreamAudio:
    """One audio fragment off a streaming TTS socket — the voice-back counterpart of ``StreamResult``.

    ``audio_b64`` is the vendor's base64 PCM exactly as it arrived. The DECODE is the step that can
    fail on a truncated fragment, so it lives where the failure is handled (the holder, which can
    skip a bad fragment rather than abort a line), not in the pure parser — which only reads the
    field. ``is_final`` is True only on the LAST fragment of the utterance a flush closed out: the
    marker the holder waits for before it knows a line is complete. A terminal frame the vendor
    sends with no audio at all is ``StreamAudio("", True)``."""

    audio_b64: str
    is_final: bool


@dataclass(frozen=True)
class SttStreaming:
    """The STREAMING variant of an STT provider — the same provider, a different transport.

    It sits ON the batch entry rather than in a table of its own because it is not a different
    provider: it is the same vendor, the same key, the same model family, reached over a websocket
    while the microphone is still open instead of by POSTing a finished clip. A provider that has
    no streaming variant carries ``streaming=None``, and that — never a name comparison — is what
    the caller branches on.

    Pure like everything else in this module: it BUILDS a URL and READS a decoded message. The
    socket, the audio, the retry and the degrade decision all belong to the script.

    The audio SHAPE travels in the settings the caller passes (``stream_rate``, and the linear16
    mono encoding this plugin's recorder table pins): a streaming URL declares what the client is
    about to send, and only the client knows that. There is deliberately no ``default_model`` here
    — the MODEL is the batch entry's axis and the two transports share it, so a user who set
    ``stt.model`` gets that model both ways and one who set nothing gets one default, not two."""

    url: Callable[["SttStreaming", "SttProvider", dict], str]
    headers: Callable[[str], dict[str, str]]
    # a decoded message -> a transcript update, or None for every message that is not one
    # (metadata, speech-started, keepalive acks — a live socket says a great deal besides words)
    result: Callable[[object], StreamResult | None]
    # what to send to ask the server to flush its finals and hang up, and what to send to keep an
    # idle socket alive; both are provider-private JSON strings, hence text frames
    close_message: str
    keepalive_message: str


@dataclass(frozen=True)
class TtsStreaming:
    """The STREAMING variant of a TTS provider — the same vendor, the same key, the same model
    family, reached over a websocket while the audio plays back instead of by POSTing a whole line.

    The mirror of ``SttStreaming`` (windowsill#113, the voice-back counterpart of #99): it sits ON
    the batch entry rather than in a table of its own because it is not a different provider — only
    a different transport — and a provider with no streaming variant carries ``streaming=None``,
    which is what the speak path branches on. Pure like everything else in this module: it BUILDS a
    URL, a begin-of-stream frame and a text frame, and READS a decoded message. The socket, the
    resident holder, the retry and the degrade decision all belong to the script.

    The audio SHAPE is the entry's own (``default_output_format``): a streaming socket sends raw
    samples, so the container the player must handle is decided HERE — ElevenLabs' ``pcm_22050`` is
    raw s16le straight to ``aplay`` with no decoder, which is why a streaming variant carries an
    output format its batch entry need not share (the batch path returns a WAV/mp3 blob)."""

    url: Callable[["TtsStreaming", "TtsProvider", dict], str]
    headers: Callable[[str], dict[str, str]]
    # the begin-of-stream frame — voice_settings (stability, similarity_boost, speed…) ride here.
    # Sent ONCE on connect; the resident holder keeps the socket for the lines that follow.
    bos: Callable[[dict], str]
    # one line (or chunk) of text as a frame
    text_message: Callable[[str], str]
    # forces the server to emit audio for the text buffered since the last flush — sent per line,
    # because the default chunk schedule generates nothing until a flush or EOS
    flush_message: str
    # a decoded message -> an audio fragment, or None for everything that is not one (metadata, an
    # error document — a live socket says a great deal besides audio)
    result: Callable[[object], StreamAudio | None]
    # whitespace keepalive: an idle text frame that holds the socket open past the vendor's idle
    # close without producing audio (no flush). Provider-private JSON, hence a text frame.
    keepalive_message: str
    # the audio container the player must handle — pcm_22050 means raw s16le, no decoder
    default_output_format: str = "pcm_22050"


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
    # a transcript, '' included (a silent clip IS an empty transcript), or None when the document
    # carries no transcript at all — which is the caller's degrade signal. See ``text_field``.
    transcript: Callable[[object], str | None]
    error_summary: Callable[[object], str]
    comparison: Comparison
    # the live-socket variant of this same provider, or None where the vendor has none / we have
    # not built one. The batch path above is unaffected by its presence and is the fallback for it.
    streaming: "SttStreaming | None" = None

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
    # the live-socket variant of this same provider, or None where the vendor has none / we have
    # not built one. The batch path above is unaffected by its presence and is the fallback for it.
    streaming: "TtsStreaming | None" = None

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


def text_field(data: object) -> str | None:
    """``{"text": "..."}`` — OpenAI's transcription shape, ElevenLabs Scribe's, and this plugin's
    own server's. None for anything without a ``text`` field, which is what makes a malformed body
    (or an error document) a degrade rather than a traceback.

    None and '' are DIFFERENT answers and the difference is load-bearing: a silent clip transcribes
    to an empty string, and that is a success. Reading '' as "no transcript" logged a cloud error
    and posted the clip a second time on every silent toggle (windowsill#93)."""
    if isinstance(data, dict) and "text" in data:
        return str(data["text"]).strip()
    return None


def _deepgram_transcript(data: object) -> str | None:
    """Deepgram nests the transcript under the first alternative of the first channel:
    ``results.channels[0].alternatives[0].transcript``. Every way that walk can fail — a dict that
    is an error document, an empty channel list — reads as None, i.e. degrade. A walk that SUCCEEDS
    on an empty string is a silent clip, and returns that empty string (see ``text_field``)."""
    try:
        return str(data["results"]["channels"][0]["alternatives"][0]["transcript"]).strip()  # type: ignore[index]
    except (TypeError, KeyError, IndexError):
        return None


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
    """ElevenLabs Scribe: multipart with ``model_id``, plus ``language_code`` when a language is
    configured — Scribe's spelling of the hint OpenAI takes as ``language``, so the two providers
    agree rather than one of them silently dropping ``stt.language`` (windowsill#93).

    An EMPTY ``stt.language`` omits the field, which is how a user asks Scribe to auto-detect."""
    fields = {"model_id": s["stt_model"]}
    if s["language"]:
        fields["language_code"] = s["language"]
    body = multipart_form(fields, "file", "dictate.wav", wav_bytes, boundary)
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


# --- STT streaming variants ----------------------------------------------------------------------


def websocket_scheme(host: str) -> str:
    """An http(s) host rewritten to its websocket scheme, and left alone if it already is one.

    A provider's ``default_host`` is an https URL because that is what the batch path POSTs to, and
    a self-hosted ``stt.cloud.endpoint`` is whatever the operator wrote. Both name the same server;
    only the scheme differs, and guessing it wrong is a connection that never opens."""
    for prefix, replacement in (("https://", "wss://"), ("http://", "ws://")):
        if host.startswith(prefix):
            return replacement + host[len(prefix):]
    return host


def _deepgram_stream_url(stream: SttStreaming, entry: SttProvider, s: dict) -> str:
    """Deepgram Listen, live: the same ``/v1/listen`` path as the batch call, over a websocket.

    Every knob is a query parameter, and three of them describe the AUDIO WE ARE ABOUT TO SEND
    rather than what we want back — there is no container to read the shape out of, because raw
    PCM is exactly what a growing recording can be forwarded as. ``interim_results`` costs nothing
    and gives the caller a live signal that speech is being heard at all; ``smart_format`` is what
    puts punctuation and capitalisation in the finals, exactly as on the batch path."""
    host = websocket_scheme(str(s.get("cloud_endpoint") or entry.default_host))
    query = urllib.parse.urlencode(
        {
            "model": s["stt_model"],
            "language": s["language"],
            "smart_format": "true",
            "interim_results": "true",
            "encoding": "linear16",
            "sample_rate": str(s["stream_rate"]),
            "channels": "1",
        }
    )
    return f"{host}/v1/listen?{query}"


def _deepgram_stream_result(data: object) -> StreamResult | None:
    """A live message -> a transcript update, or None for everything that is not one.

    A live socket says far more than words: ``Metadata`` when the stream ends, ``SpeechStarted``,
    ``UtteranceEnd``, and error documents. Only ``Results`` carries a transcript, and its nesting
    is the streaming shape (``channel.alternatives[0]``), one level shallower than the batch
    response's (``results.channels[0].alternatives[0]``) — which is precisely the kind of drift a
    shared parser would hide."""
    if not isinstance(data, dict) or data.get("type") not in (None, "Results"):
        return None
    try:
        text = str(data["channel"]["alternatives"][0]["transcript"]).strip()  # type: ignore[index]
    except (TypeError, KeyError, IndexError):
        return None
    return StreamResult(text, bool(data.get("is_final")))


DEEPGRAM_STREAMING = SttStreaming(
    url=_deepgram_stream_url,
    headers=lambda key: {"Authorization": f"Token {key}"},
    result=_deepgram_stream_result,
    # documented control messages: CloseStream asks the server to transcribe what is left and
    # close (the finals arrive AFTER it, which is why the caller drains), KeepAlive holds an idle
    # socket open past the vendor's ten-second silence timeout.
    close_message=json.dumps({"type": "CloseStream"}),
    keepalive_message=json.dumps({"type": "KeepAlive"}),
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


# --- TTS streaming variants ----------------------------------------------------------------------


def _elevenlabs_stream_url(stream: TtsStreaming, entry: TtsProvider, s: dict) -> str:
    """ElevenLabs stream-input, live: the same text-to-speech voice as the batch call, over a
    websocket that stays open across lines. The model and the OUTPUT FORMAT are query parameters —
    there is no container on the way back, only raw PCM frames, so the format the player must
    handle is declared here (pcm_22050 = raw s16le, no decoder in the critical path)."""
    host = websocket_scheme(str(s.get("endpoint") or entry.default_host))
    voice_id = urllib.parse.quote(str(s.get("voice_id") or ""), safe="")
    query = urllib.parse.urlencode(
        {
            "model_id": s["cloud_model"],
            "output_format": s.get("stream_output_format") or stream.default_output_format,
        }
    )
    return f"{host}/v1/text-to-speech/{voice_id}/stream-input?{query}"


def _elevenlabs_stream_bos(s: dict) -> str:
    """The begin-of-stream frame: a near-empty text frame carrying the voice settings. Sent once on
    connect; the resident holder keeps the socket for the lines that follow. An unset voice_settings
    sends a bare BOS and the vendor defaults apply — speed is folded in by the holder before this
    runs, so it rides whenever the operator set one."""
    payload: dict = {"text": " "}
    settings = s.get("voice_settings")
    if isinstance(settings, dict):
        payload["voice_settings"] = settings
    return json.dumps(payload)


def _elevenlabs_stream_text(text: str) -> str:
    """One line of text as a frame. A trailing space is intentional: ElevenLabs concatenates the
    text frames between flushes, and a line whose first word runs straight into the previous line's
    last word is a word neither the model nor the listener can recover."""
    return json.dumps({"text": f"{text} "})


def _elevenlabs_stream_result(data: object) -> StreamAudio | None:
    """A live message -> an audio fragment, or None for everything that is not one.

    ElevenLabs' stream-input answers each flushed text with base64 PCM under ``audio`` and a final
    marker: ``{"audio": "<b64>", "isFinal": false}`` per fragment, then ``{"isFinal": true}``
    (audio null) to close the utterance the flush ended. The marker is NOT an error: the holder
    waits on it to know a line is complete, so it must read as ``StreamAudio("", True)`` — which is
    why the test is "carries ``audio`` OR ``isFinal``", not "carries ``audio``" alone. A document
    with neither — metadata, an error document — is not part of the audio stream, and reads None."""
    if not isinstance(data, dict):
        return None
    if "audio" not in data and "isFinal" not in data:
        return None
    audio = data.get("audio")
    if audio is not None and not isinstance(audio, str):
        return None
    return StreamAudio(audio or "", bool(data.get("isFinal")))


ELEVENLABS_STREAMING = TtsStreaming(
    url=_elevenlabs_stream_url,
    headers=lambda key: {"xi-api-key": key},
    bos=_elevenlabs_stream_bos,
    text_message=_elevenlabs_stream_text,
    # documented flush: forces the server to emit audio for the buffered text without ending the
    # stream — sent per line, because the default chunk schedule generates nothing until a flush
    # or EOS (live probe: +174-212 ms per flushed fragment on one connection).
    flush_message=json.dumps({"flush": True}),
    result=_elevenlabs_stream_result,
    # whitespace keepalive: a text frame with no flush produces no audio and resets the vendor's
    # ~20 s idle close (WS 1008). Sent by the holder between turns, every <= KEEPALIVE_SECONDS.
    keepalive_message=json.dumps({"text": " "}),
    default_output_format="pcm_22050",
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
            languages="99 languages including Russian and Ukrainian; stt.language rides as "
            "language_code, and an empty one lets Scribe auto-detect",
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
        # the one entry with a live socket today (windowsill#99) — opt in with stt.cloud.streaming
        streaming=DEEPGRAM_STREAMING,
        comparison=Comparison(
            latency="the fastest of the three on short clips, and the only one with a STREAMING "
            "variant here (stt.cloud.streaming: true) — the transcript is assembled while you "
            "speak, so a long dictation stops paying for its own length at the end",
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
        # the one entry with a live socket on the TTS side (windowsill#113) — opt in with
        # tts.cloud.streaming. The resident holder keeps one stream-input socket open across turns,
        # so the TLS+WS dial is paid once per session, not once per line; pcm_22050 out, no decoder.
        streaming=ELEVENLABS_STREAMING,
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


def _validate_streaming(key: str, streaming: "SttStreaming | None") -> None:
    """A streaming variant validates like a row of its own, because that is what it is.

    Same reasoning as the batch checks beside it: everything here would otherwise surface at
    runtime as a socket that will not open or a message nobody parses — in a path whose failure
    mode is a quiet degrade back to the batch call, i.e. exactly the kind of break that ships."""
    if streaming is None:
        return
    for field in ("url", "headers", "result"):
        if not callable(getattr(streaming, field)):
            raise ValueError(f"provider registry: {key!r}'s streaming variant has no {field}")
    for field in ("close_message", "keepalive_message"):
        value = getattr(streaming, field)
        try:
            json.loads(value)
        except ValueError as err:
            raise ValueError(f"provider registry: {key!r}'s streaming {field} is not JSON: {err}") from err


def _validate_tts_streaming(key: str, streaming: "TtsStreaming | None") -> None:
    """A TTS streaming variant validates like a row of its own — same reasoning as the STT one:
    everything here would otherwise surface at runtime as a socket that will not open or a frame
    nobody parses, in a path whose failure mode is a quiet degrade."""
    if streaming is None:
        return
    for field in ("url", "headers", "bos", "text_message", "result"):
        if not callable(getattr(streaming, field)):
            raise ValueError(f"provider registry: {key!r}'s TTS streaming variant has no {field}")
    for field in ("flush_message", "keepalive_message"):
        value = getattr(streaming, field)
        try:
            json.loads(value)
        except ValueError as err:
            raise ValueError(f"provider registry: {key!r}'s TTS streaming {field} is not JSON: {err}") from err
    if not streaming.default_output_format:
        raise ValueError(f"provider registry: {key!r}'s TTS streaming variant has no output format")


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
            # the two tables carry DIFFERENT streaming shapes (SttStreaming vs TtsStreaming), so the
            # validator is chosen by the table — a row never reaches the wrong one.
            if table is STT_PROVIDERS:
                _validate_streaming(key, getattr(entry, "streaming", None))
            else:
                _validate_tts_streaming(key, getattr(entry, "streaming", None))
    if DEFAULT_STT not in STT_PROVIDERS:
        raise ValueError(f"provider registry: the default STT provider {DEFAULT_STT!r} has no entry")
    if DEFAULT_TTS not in TTS_PROVIDERS:
        raise ValueError(f"provider registry: the default TTS provider {DEFAULT_TTS!r} has no entry")


_validate_registry()
