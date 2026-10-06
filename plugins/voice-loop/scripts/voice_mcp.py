#!/usr/bin/env python3
"""voice-loop plugin MCP server.

One stdlib stdio MCP server that does two unrelated jobs:

* ``design_previews`` and ``design_save`` — the two ElevenLabs text-to-voice calls that used
  to live as inline python snippets in ``skills/voice-design/SKILL.md``. The skill calls
  them by name; the key is read here, never in the skill, never on argv.

* The hotkey STT relay — a Unix-socket listener that the desktop ``dictate-toggle.sh``
  path connects to for cloud STT. The socket lives under ``$XDG_RUNTIME_DIR/voice-loop``
  with mode 0700, the relay owns the stale-socket question, and the client vouches for
  ownership before it dials (the mitigate is in ``scripts/dictate.py``). Two legs share
  the socket — the historical batch path (one WAV in, one transcript out) and the
  streaming dictation path (one stream-line in, the provider socket framed through the
  relay byte-for-byte). The dispatch is at the front of ``_serve_one_client``: a line
  without ``mode`` is the batch line, a line with ``mode == "stream"`` is the stream
  line and is handled by ``_serve_stream_client``. The clear-text refusal, the
  three-step key resolution and the closed ``_REASONS`` set are the same on both legs.

The server holds ONE thing of its own — the two plugin userConfig values, delivered as
``CLAUDE_PLUGIN_OPTION_TTS_API_KEY`` and ``CLAUDE_PLUGIN_OPTION_STT_API_KEY`` in its env
block. The relay resolves the STT key itself in three steps (the STT key; the TTS key
only when the resolved registry entry's ``fallback_vendors`` admits the request's
``tts_vendor``; else ``no-key``) and never reads ``config.json``.

The streaming TTS over the resident websocket (``speak.py`` still uses ``wsclient``)
keeps its key passed directly by the holder process — that path is unchanged.
"""

from __future__ import annotations

import base64
import http.client
import json
import os
import socket as _socket
import sys
import threading
import time
import urllib.error
import urllib.request

# Make the providers registry importable — voice_mcp.py lives in scripts/ and
# the registry is its sibling. The same sys.path approach speak.py / dictate.py
# use; no package hierarchy here on purpose (a single file is the contract).
_PLUGINS_DIR = os.path.dirname(os.path.abspath(__file__))
if _PLUGINS_DIR not in sys.path:
    sys.path.insert(0, _PLUGINS_DIR)
import providers  # noqa: E402 — sys.path injection above is the contract
import wsclient  # noqa: E402 — same sys.path contract; wsclient dials the provider stream

# --- constants --------------------------------------------------------------

# Two env names this server reads. Both come from the manifest's mcpServers env block.
ENV_TTS_KEY = "CLAUDE_PLUGIN_OPTION_TTS_API_KEY"
ENV_STT_KEY = "CLAUDE_PLUGIN_OPTION_STT_API_KEY"

# ElevenLabs endpoints — the two the voice-design skill calls. Bodies and headers are
# the same ones the deleted SKILL.md snippets used; the key header is added here.
ELEVENLABS_PREVIEWS_URL = "https://api.elevenlabs.io/v1/text-to-voice/create-previews"
ELEVENLABS_CREATE_URL = "https://api.elevenlabs.io/v1/text-to-voice/create-voice-from-preview"

# Wire-protocol enums. ``bad-request`` is exactly the four conditions named in the
# ticket; nothing else earns that reason. ``clear-text-refused`` is the relay's
# answer to a configured http:// (or ws://) endpoint with a credential, refused
# at the relay because the key is here, in this process, and the relay holds it.
_REASONS = {
    "no-key",
    "bad-request",
    "provider-unreachable",
    "timeout",
    "clear-text-refused",
}

# Provider names the relay accepts — derived from providers.STT_PROVIDERS so the
# registry is the single source of truth, with an explicit frozenset for the
# closed-enum test in tests/voice-loop/tests/test_voice_mcp.py.
_VALID_PROVIDERS = frozenset(providers.STT_PROVIDERS.keys())

# Probe / takeover timings.
REBIND_RETRY_SECONDS = 30
SOCKET_PROBE_TIMEOUT_SECONDS = 1

# 64 KiB cap on the bytes before the first newline (the request line). A request
# line that grows past 64 KiB is malformed — no operator's config is 64 KiB —
# so the cap doubles as a refusal boundary.
REQUEST_LINE_MAX_BYTES = 64 * 1024

# 32 MiB cap on the WAV bytes (the body after the first newline). A longer clip
# is possible but every shipped recorder tops out well under that, and a body
# without a cap is a memory-exhaustion surface.
WAV_MAX_BYTES = 32 * 1024 * 1024

# 4 MiB cap on the provider response. A transcript is far under 100 KiB, so 4 MiB is
# generous for every shipped entry; the cap is here because a server that streams
# forever (a chatty error page, a misconfigured endpoint) would otherwise pin this
# process on ``resp.read()`` with no upper bound. Same shape as the dictate
# client's local cap; declared here so the relay does not import dictate.
PROVIDER_RESPONSE_MAX_BYTES = 4 * 1024 * 1024

# Per-frame cap on the byte copier that carries frames between the client and the
# provider. A stream leg never carries a frame larger than the wsclient
# ceiling (1 MiB), but a hostile peer could ask for a read that big — a cap here
# keeps the relay from allocating its way out of memory if it ever does.
STREAM_FRAME_MAX_BYTES = wsclient.MAX_FRAME_BYTES
# How long a frame's worth of reads can sit idle between the two legs. A live
# socket between a worker (who am reading a 250 ms WavChunk at a time) and a
# provider (who am talking the same socket) never idles this long; the cap is
# the wall-clock bound around the dial, the place a connect timeout is reported
# versus an unreachable host (windowsill#5881 R6).
STREAM_DIAL_TIMEOUT_SECONDS = 5.0


# --- server version ----------------------------------------------------------


def _read_plugin_version() -> str:
    """Read the version from ``../.claude-plugin/plugin.json`` relative to this
    file, with a fixed fallback only if the read fails. The three manifest sites
    (this read, ``.claude-plugin/marketplace.json``, the root ``README.md`` row)
    agree by being read from the same source — a hardcoded literal here would
    be a fourth place to keep in step, kept by hand, and is what the brief
    retired (windowsill#5870, R10).
    """
    try:
        path = os.path.join(_PLUGINS_DIR, "..", ".claude-plugin", "plugin.json")
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        return str(doc.get("version") or "0.0.0")
    except (OSError, ValueError):
        return "0.0.0"


# --- key resolution ---------------------------------------------------------


def _stt_key_from_env(entry: providers.SttProvider, tts_vendor: str) -> str:
    """The STT key, resolved in three steps — never through ``key_envs``.

    1. ``CLAUDE_PLUGIN_OPTION_STT_API_KEY`` if non-empty.
    2. else, ONLY when the request's ``tts_vendor`` is in the resolved entry's
       ``fallback_vendors`` — registry DATA, not a provider-name comparison here —
       then ``CLAUDE_PLUGIN_OPTION_TTS_API_KEY``. ElevenLabs is the only entry
       that carries a non-empty set, so no other provider's STT request can ever
       borrow the TTS key.
    3. else ``""`` — the caller responds ``no-key``.

    The relay does not call ``providers.SttProvider.key_envs`` — those return env-var
    *names* and ElevenLabs carries ``key_env_fallbacks=("VOICE_LOOP_TTS_API_KEY",)``,
    so a relay calling them would read exactly the named variable this ticket removes.
    """
    stt = os.environ.get(ENV_STT_KEY, "").strip()
    if stt:
        return stt
    if tts_vendor in entry.fallback_vendors:
        return os.environ.get(ENV_TTS_KEY, "").strip()
    return ""


def _tts_key_from_env() -> str:
    """The TTS key for the voice-design tools. Always the userConfig value."""
    return os.environ.get(ENV_TTS_KEY, "")


# --- STT wire protocol ------------------------------------------------------


def _bad_request(reason_detail: str) -> dict:
    return {"status": "failed", "reason": "bad-request", "detail": reason_detail}


def _ok(text: str) -> dict:
    return {"status": "ok", "text": text}


def _failed(reason: str, **detail: object) -> dict:
    out: dict = {"status": "failed", "reason": reason}
    out.update(detail)
    return out


def _validate_request_line(line: str) -> tuple[dict | None, str | None]:
    """Return ``(parsed, None)`` on success or ``(None, error_message)`` on bad-request.

    Bad-request covers exactly four conditions:
    1. the line is not valid UTF-8 JSON (we receive str here so utf-8 errors surface as ValueError);
    2. any of the seven keys is missing (``provider``, ``endpoint``, ``model``, ``language``,
       ``tts_vendor``, ``timeout``, ``stt_prompt``);
    3. the named ``provider`` is not in the registry;
    4. zero WAV bytes arrived after the newline (the caller enforces this).

    A line that names ``mode == "stream"`` is the streaming dictation request and is validated by
    ``_validate_stream_line`` instead — the four conditions above describe the BATCH request, which
    is exactly the request that has carried the seven keys since the v0 contract. A stream line without
    a ``mode`` is still the batch line (the worker dials without setting ``mode``), so a non-dict
    request line is the batch line's own concern and routes through the existing four-condition path.
    """
    try:
        parsed = json.loads(line)
    except (ValueError, UnicodeDecodeError) as err:
        return None, f"request line is not valid JSON: {type(err).__name__}"
    if not isinstance(parsed, dict):
        return None, "request line is not a JSON object"
    # A stream line declares its mode on its own JSON and doesn't lie: any line without
    # "mode" is the historical batch line, and the seven-key check covers it. A line that
    # names "mode" with a value other than "stream" is malformed and gets bad-request at
    # the dispatch step (``_dispatch_request_line``), where the dispatch decides batch
    # vs stream — not at the validator.
    if "mode" not in parsed:
        required = (
            "provider",
            "endpoint",
            "model",
            "language",
            "tts_vendor",
            "timeout",
            "stt_prompt",
        )
        missing = [k for k in required if k not in parsed]
        if missing:
            return None, f"missing keys: {missing}"
        if parsed["provider"] not in _VALID_PROVIDERS:
            return None, f"unknown provider: {parsed['provider']!r}"
    return parsed, None


def _validate_stream_line(line: str) -> tuple[dict | None, str | None]:
    """The streaming dictation return from a stream-line parse: ``(parsed, None)`` on success or
    ``(None, error_message)`` on bad-request.

    Bad-request covers exactly three conditions (windowsill#5881 R1):
    1. the line is not valid UTF-8 JSON;
    2. one of ``provider`` / ``url`` / ``tts_vendor`` / ``timeout`` is missing;
    3. the named ``provider`` is not in the registry, or its entry has ``streaming=None``
       (the same registry the batch dial keys off — no check, there is a name comparison).

    A stream line that names a clear-text ``ws://`` endpoint with a key is refused at the relay
    (the same ``clear-text-credential_error`` policy the batch dial applies) BEFORE this point, in
    the connection handler — the guard reads the resolved key, not the request line, so a missing
    key cannot disguise a clear-text endpoint and a present key cannot be carried in the clear.
    """
    try:
        parsed = json.loads(line)
    except (ValueError, UnicodeDecodeError) as err:
        return None, f"stream line is not valid JSON: {type(err).__name__}"
    if not isinstance(parsed, dict):
        return None, "stream line is not a JSON object"
    required = ("provider", "url", "tts_vendor", "timeout")
    missing = [k for k in required if k not in parsed]
    if missing:
        return None, f"stream line missing keys: {missing}"
    name = parsed["provider"]
    if name not in _VALID_PROVIDERS:
        return None, f"stream line unknown provider: {name!r}"
    if providers.STT_PROVIDERS[name].streaming is None:
        return None, f"stream line provider {name!r} has no streaming variant"
    return parsed, None


def _stream_dial_failure(reason: str) -> str:
    """Map a ``wsclient.WebSocketError`` from the dial to the relay's typed reason.

    wsclient reports a connect timeout as `WebSocketError("could not reach ...: …")` — the same
    message it uses for an unreachable host. The two cannot be distinguished from the exception
    text, so the relay enforces the line's ``timeout`` as a wall-clock bound around the dial
    and reports the breach as ``"timeout"``. Every other ``WebSocketError`` from the dial is
    ``"provider-unreachable"``, the same token the batch dial returns for a transport-level
    failure (windowsill#5881 R6: batch and stream cannot drift).
    """
    return "provider-unreachable" if reason != "timeout" else "timeout"


def _socket_timeout() -> type[OSError] | None:
    """Timeout exception type — ``socket.timeout`` lives under different names on some ports.

    Resolved ONCE at import: as written the except clause was a function call,
    not a class — the function always returned a type, but the call was never
    made in an except tuple. Storing the value here lets the except clause name
    a class, which is what the brief required.
    """
    return getattr(_socket, "timeout", None)


_SOCKET_TIMEOUT = _socket_timeout() or TimeoutError


def _post_provider(
    entry: providers.SttProvider,
    s: dict,
    key: str,
    wav_bytes: bytes,
    timeout: float,
) -> tuple[str | None, str | None, str | None]:
    """Post a WAV through the provider's own request builder.

    Returns ``(transcript, reason_or_None, detail_or_None)``. ``reason`` is one
    of the closed-enum members in ``_REASONS`` (and ``provider-http-<code>`` for
    an HTTP error); ``detail`` is the refusal text the relay surfaces to the
    client when the entry's transcript is None (an error document, a malformed
    body, the entry's own ``error_summary`` shape).

    Proxies are bypassed the same way the deleted SKILL.md snippets did — via a
    ``ProxyHandler({})``. Each provider's own request builder knows its own
    auth header (Bearer / xi-api-key / Token), its own path, its own field
    names and its own content type — the relay no longer spells any of those.
    The entry's ``content_type`` rides as the ``Content-Type`` header the way
    ``dictate.py``'s ``_post_bytes`` sends it: without it a multipart body
    ships as ``application/x-www-form-urlencoded`` and the provider cannot
    parse the form at all.
    """
    # The entry's endpoint() chooses among cloud_endpoint / default_host /
    # endpoint; the relay hands the entry the request line's "endpoint" under
    # "cloud_endpoint" so a configured value wins, then the entry's default_host
    # fills the gap. An empty resolved URL is the only thing that earns a
    # bad-request here.
    request_url = entry.endpoint(s)
    if not request_url:
        return None, "bad-request", "no endpoint"
    try:
        boundary = "----voice-mcp" + os.urandom(8).hex()
        request = entry.request(s, key, wav_bytes, boundary)
    except (OSError, ValueError, KeyError) as err:
        return None, "bad-request", type(err).__name__
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(
        request.url,
        data=request.body,
        headers={"Content-Type": request.content_type, **request.headers},
        method="POST",
    )
    try:
        with opener.open(req, timeout=timeout) as resp:
            # Read at most cap+1 bytes — the extra byte is the boundary the cap
            # decision uses (a body of exactly cap is fine; a body of cap+1 is
            # over). Tiny test doubles may expose only the no-argument urllib
            # shape, so the read is wrapped in a TypeError guard the same way
            # dictate.py's _bounded_response_read does.
            try:
                raw = resp.read(PROVIDER_RESPONSE_MAX_BYTES + 1)
            except TypeError:
                raw = resp.read()
            if len(raw) > PROVIDER_RESPONSE_MAX_BYTES:
                return None, "provider-unreachable", (
                    f"response over {PROVIDER_RESPONSE_MAX_BYTES} bytes"
                )
    except urllib.error.HTTPError as err:
        return None, f"provider-http-{err.code}", None
    except urllib.error.URLError:
        return None, "provider-unreachable", None
    except (TimeoutError, _SOCKET_TIMEOUT):
        return None, "timeout", None
    except http.client.HTTPException:
        # http.client raises HTTPException for protocol-level failures
        # (IncompleteRead, BadStatusLine, LineTooLong) that are not OSError
        # subclasses. Without this handler the exception propagates out of
        # _serve_one_client, the per-connection thread dies, and no reply line is
        # ever written — the client waits out its own deadline. The relay
        # treats it as provider-unreachable, the same verdict any other
        # transport-level failure earns.
        return None, "provider-unreachable", None
    except OSError:
        return None, "provider-unreachable", None
    try:
        data = providers.decode(raw)
    except ValueError:
        return None, "provider-unreachable", None
    if data is None:
        return None, "provider-unreachable", None
    try:
        text = entry.transcript(data)
    except (AttributeError, TypeError) as err:
        return None, "provider-unreachable", type(err).__name__
    if text is None:
        # The body carries no transcript field at all — an API error document,
        # an empty body, or something this provider's parser does not recognise.
        # Log its shape so the operator can tell a quota error from a bad model
        # name. An EMPTY transcript is deliberately NOT this case: a silent
        # clip transcribes to "" and that is a success.
        detail = entry.error_summary(data) if data is not None else None
        return None, "provider-unreachable", detail
    return text, None, None


def _serve_one_client(client_sock: _socket.socket, addr) -> None:
    """One connection, one WAV, one JSON reply. Closes the socket on exit.

    The wire is: one UTF-8 JSON request line (terminated by ``\\n``), then the
    raw WAV bytes until the client's ``shutdown(SHUT_WR)``, then one UTF-8
    JSON reply line. We split on the FIRST ``\\n`` anywhere in the buffer —
    the request line and the WAV can arrive in the same ``recv``, and reading
    only into a ``buf.endswith(b"\\n")`` loop misses the case where a partial
    read returned a newline in the middle of the buffer. The 64 KiB cap on the
    request line is applied ONLY to the bytes before the first newline.

    A line that names ``mode == "stream"`` is the streaming dictation request
    (windowsill#5881). The line arrives without a trailing WAV — the streaming
    client writes ONE byte back when it has finished its frame — and the dispatch
    below hands the connection to ``_serve_stream_client``. A line without
    ``mode`` is the historical batch line, unchanged.

    We never call ``shutdown(SHUT_WR)`` here — the client already half-closed
    its write side. A relay that half-closes its own write side and then
    sendall's the reply gets a ``BrokenPipeError`` and the client reads EOF as
    reason=timeout. The reply is one ``sendall`` followed by ``close()`` in
    the ``finally`` block, and only that.
    """
    try:
        buf = b""
        # Read until newline appears anywhere in the buffer — the request line
        # is one JSON object terminated by '\n'. ``buf.find(b"\n")`` returns -1
        # until the line is complete, regardless of where in the buffer the
        # newline arrived.
        client_sock.settimeout(5.0)
        while buf.find(b"\n") == -1:
            try:
                chunk = client_sock.recv(65536)
            except OSError:
                client_sock.sendall((json.dumps(_failed("timeout")) + "\n").encode())
                return
            if not chunk:
                # EOF before a newline — the client never finished the request
                # line. We send a bad-request and close. (The client half-closes
                # AFTER its WAV; a missing newline at this point is malformed.)
                client_sock.sendall(
                    (json.dumps(_bad_request("no newline in request line")) + "\n").encode()
                )
                return
            buf = buf + chunk
            if len(buf) > REQUEST_LINE_MAX_BYTES:
                client_sock.sendall(
                    (json.dumps(_bad_request("request line over 64 KiB")) + "\n").encode()
                )
                return
        newline_pos = buf.find(b"\n")
        line = buf[:newline_pos].decode("utf-8", errors="replace")
        # A stream line declares its mode on its own JSON. We peek at the parsed
        # shape BEFORE running the full batch validator: a stream line carries
        # ``mode == "stream"`` and NOT the seven batch keys (the worker's
        # request line is ``{"mode", "provider", "url", "tts_vendor", "timeout"}``),
        # so the batch validator would answer ``missing keys`` — wrong reason,
        # wrong verdict. The dispatch below hands the connection to
        # ``_serve_stream_client`` and reads nothing further off the socket;
        # the relay is then a bidirectional frame copier until both legs say
        # goodbye.
        try:
            peek = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            peek = None
        if isinstance(peek, dict) and peek.get("mode") == "stream":
            _serve_stream_client(client_sock, line, addr)
            return
        # The remainder is the start of the WAV; more bytes follow.
        wav = buf[newline_pos + 1:]
        parsed, err = _validate_request_line(line)
        if err is not None:
            client_sock.sendall((json.dumps(_bad_request(err)) + "\n").encode())
            return
        # Read the rest of the WAV — the client has already half-closed its write
        # side, so EOF on the read side marks the end of the body. We do NOT
        # call shutdown on our side; that would break the reply's sendall.
        client_sock.settimeout(max(1.0, float(parsed["timeout"]) + 5.0))
        try:
            while True:
                chunk = client_sock.recv(65536)
                if not chunk:
                    break
                wav = wav + chunk
                if len(wav) > WAV_MAX_BYTES:
                    client_sock.sendall(
                        (json.dumps(_bad_request("WAV over 32 MiB")) + "\n").encode()
                    )
                    return
        except OSError:
            pass
        if not wav:
            client_sock.sendall(
                (json.dumps(_bad_request("zero WAV bytes after newline")) + "\n").encode()
            )
            return
        # Resolve the provider entry from the registry; the request's "provider"
        # is the field the relay validates, the entry drives the request build.
        provider_name = parsed["provider"]
        entry = providers.stt_provider(provider_name)
        if entry is None:
            client_sock.sendall(
                (json.dumps(_bad_request(f"unknown provider: {provider_name!r}")) + "\n").encode()
            )
            return
        # Build the relay-side `s` dict the entry's build() expects. ``endpoint``
        # is deliberately empty here so entry.endpoint() picks the default host
        # from cloud_endpoint / default_host / endpoint order. ``stt_prompt`` is
        # the lexicon hint that the dictation client resolves from the config.
        s = {
            "stt_model": parsed["model"],
            "language": parsed["language"],
            "stt_prompt": parsed.get("stt_prompt", ""),
            "cloud_endpoint": parsed.get("endpoint", ""),
            "endpoint": "",  # the entry picks the default host from cloud_endpoint first
        }
        # Three-step STT key resolution: the STT key; the TTS key only when the
        # request's tts_vendor is in the entry's fallback_vendors (registry data
        # — the relay holds no provider-name comparison); else no-key. Resolved
        # BEFORE the clear-text check so the absence of a key is reported as
        # ``no-key`` even on a non-local http:// endpoint — a missing key and a
        # plain-text endpoint are two different configuration errors, and the
        # operator's fix depends on which one the reply names.
        key = _stt_key_from_env(entry, parsed.get("tts_vendor", ""))
        if not key:
            client_sock.sendall((json.dumps(_failed("no-key")) + "\n").encode())
            return
        # Clear-text refusal happens after the key check, with the resolved
        # key as the credential flag. A configured http:// (or ws://) endpoint
        # with a credential is a configuration error refused here, not a
        # warning sent along (windowsill #215). The key never leaves this
        # process before the clear-text check has passed; the request builder
        # below only runs on the accepted path.
        clear_text_refusal = providers.clear_text_credential_error(
            entry.endpoint(s), has_credential=bool(key)
        )
        if clear_text_refusal is not None:
            client_sock.sendall(
                (
                    json.dumps(
                        {
                            "status": "failed",
                            "reason": "clear-text-refused",
                            "detail": clear_text_refusal,
                        }
                    )
                    + "\n"
                ).encode()
            )
            return
        transcript, reason, detail = _post_provider(
            entry, s, key, wav, float(parsed["timeout"])
        )
        if reason is not None:
            payload: dict = {"status": "failed", "reason": reason}
            if detail is not None:
                payload["detail"] = detail
            client_sock.sendall((json.dumps(payload) + "\n").encode())
            return
        # transcript is non-None; empty string is a success (windowsill#93
        # silent-clip rule — an empty transcript is a real transcript).
        client_sock.sendall((json.dumps(_ok(transcript)) + "\n").encode())
    finally:
        try:
            client_sock.close()
        except OSError:
            pass


# --- STT streaming wire protocol (windowsill#5881) ----------------------------
#
# The streaming variant runs over the same AF_UNIX socket as the batch path
# and uses the same vouching — the client owns the lstat / mode check
# (dictate._relay_socket_safe) before it dials, and the relay owns the
# stale-socket question. The same single CONNECTED stream dial is what
# restores what #5870 cut: streaming cloud dictation, which the hotkey
# dictation path can no longer reach directly because it holds no key.
#
# Wire (from the client side):
#   {"mode": "stream", "provider": ..., "url": ..., "tts_vendor": ..., "timeout": ...}\n
# Then both sides write FRAMED byte sequences:
#   1 byte opcode (wsclient OP_TEXT/OP_BINARY/OP_CLOSE)
#   4 bytes big-endian payload length
#   <payload>
# The relay is a BYTE COPIER between the two legs. Every frame arriving on
# the client leg is forwarded to the provider leg and vice versa, one thread
# per direction, beside the batch handler. A frame's worth of reads closes both
# legs on EOF or any error — a metered provider connection never outlives
# the worker.


def _read_exact(client_sock: _socket.socket, n: int) -> bytes | None:
    """Read exactly ``n`` bytes, returning ``None`` on EOF before that point.

    Frames are small (8-byte head + ≤1 MiB payload) so this is a bounded
    dance: each call returns the whole thing or signals end-of-stream, and
    the caller falls through to a typed failure. A short read is a partial
    read we complete before moving on, not a failure to retry."""
    buf = bytearray()
    while len(buf) < n:
        try:
            chunk = client_sock.recv(n - len(buf))
        except OSError:
            return None
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def _stream_reply_failure(client_sock: _socket.socket, reason: str, detail: str | None = None) -> None:
    """Write one JSON line ``{"status": "failed", "reason": R[, "detail": ...]}`` and return. The
    caller closes. We use the same wire envelope the batch dial writes so the client's parser
    is the same one (windowsill#5881 R2)."""
    payload: dict = {"status": "failed", "reason": reason}
    if detail is not None:
        payload["detail"] = detail
    try:
        client_sock.sendall((json.dumps(payload) + "\n").encode())
    except OSError:
        pass


def _pump_frames(reader_sock, writer_sock, *, name: str, errors: list) -> None:
    """Copy one direction's frames between two sockets, byte for byte.

    Each frame: 1-byte opcode, 4-byte big-endian length, then the payload.
    Errors (OSError, websocketError, anything the provider hands back as a
    death signal) land in ``errors`` and the loop returns — the caller
    closes both legs."""
    while True:
        head = _read_exact(reader_sock, 5)
        if head is None:
            return
        opcode = head[0]
        length = int.from_bytes(head[1:5], "big")
        if length > STREAM_FRAME_MAX_BYTES:
            errors.append(f"{name}: frame over {STREAM_FRAME_MAX_BYTES} bytes")
            return
        payload = _read_exact(reader_sock, length) if length else b""
        if payload is None:
            return
        try:
            writer_sock.sendall(bytes([opcode]) + length.to_bytes(4, "big") + payload)
        except OSError as err:
            errors.append(f"{name}: {err}")
            return


def _serve_stream_client(client_sock: _socket.socket, line: str, addr) -> None:
    """The streaming leg of the relay: one stream request, one ok/fail JSON line,
    then bidirectional frame pumping until either end says goodbye.

    The dial has the wall-clock bound the line's ``timeout`` carries, the
    clear-text refusal is the same as the batch dial's, and the failure
    reasons are the same closed set (``_REASONS``). A metered provider
    connection outlives the worker on no path: any error on either leg closes
    the other (windowsill#5881 R3)."""
    parsed, err = _validate_stream_line(line)
    if err is not None:
        client_sock.sendall((json.dumps(_bad_request(err) | {"reason": "bad-request"}) + "\n").encode())
        return
    name = parsed["provider"]
    url = parsed["url"]
    tts_vendor = parsed.get("tts_vendor", "")
    timeout = float(parsed["timeout"])
    entry = providers.STT_PROVIDERS[name]
    # Key resolution is the SAME three-step rule the batch dial applies
    # (windowsill#5881 R5: "STT key first, TTS key only when the entry's
    # ``fallback_vendors`` admits the request's ``tts_vendor``"). A missing
    # key answers ``no-key`` BEFORE the clear-text refusal runs.
    key = _stt_key_from_env(entry, tts_vendor)
    if not key:
        _stream_reply_failure(client_sock, "no-key")
        return
    # Endpoint policy: the clear-text refusal (windowsill#5881 R4). wsclient
    # accepts ws:// freely — the library carries no scheme refusal — so the
    # guard is the relay's. ``providers.clear_text_credential_error`` admits
    # loopback, so the CI fake on 127.0.0.1 and the pytest fake provider are
    # unaffected. On refusal we answer the exact token the batch dial already
    # uses (``clear-text-refused``) and do not open the provider socket.
    clear_text_refusal = providers.clear_text_credential_error(url, has_credential=True)
    if clear_text_refusal is not None:
        client_sock.sendall(
            (
                json.dumps(
                    {"status": "failed", "reason": "clear-text-refused", "detail": clear_text_refusal}
                )
                + "\n"
            ).encode()
        )
        return
    # Wall-clock bound around the dial: wsclient raises ``WebSocketError
    # "could not reach ..."`` identically for a connect timeout and an
    # unreachable host, so the line's ``timeout`` is what tells them apart
    # (windowsill#5881 R6). We wrap the dial in a deadline thread.
    deadline = time.monotonic() + max(0.001, timeout)
    dial_result: dict = {"status": None}

    def _dial() -> None:
        try:
            dial_result["ws"] = wsclient.connect(url, entry.streaming.headers(key), timeout=timeout)
            dial_result["status"] = "ok"
        except wsclient.WebSocketError as err:
            dial_result["error"] = str(err)

    dial_thread = threading.Thread(target=_dial, daemon=True)
    dial_thread.start()
    dial_thread.join(timeout + 0.5)
    if dial_result.get("status") != "ok":
        reason = "timeout" if time.monotonic() >= deadline else _stream_dial_failure(
            "provider-unreachable"
        )
        _stream_reply_failure(client_sock, reason)
        return
    ws = dial_result["ws"]
    # ``ok`` line — the worker's adapter polls for it before the session
    # proceeds. From here both legs carry framed bytes.
    try:
        client_sock.sendall((json.dumps({"status": "ok"}) + "\n").encode())
    except OSError:
        ws.close()
        return
    errors: list = []
    # A thread per direction. Each one is a pure byte copier — no
    # request-level work, no JSON, no decoding. Errors land in the shared
    # ``errors`` tuple and both legs close on the first one.
    client_to_provider = threading.Thread(
        target=_pump_frames, args=(client_sock, ws._sock), kwargs={"name": "client->provider", "errors": errors}, daemon=True
    )
    provider_to_client = threading.Thread(
        target=_pump_frames, args=(ws._sock, client_sock), kwargs={"name": "provider->client", "errors": errors}, daemon=True
    )
    client_to_provider.start()
    provider_to_client.start()
    client_to_provider.join()
    provider_to_client.join()
    # Close both legs — a metered provider connection never outlives the
    # worker, and a worker whose client side just disconnected leaves a
    # dangling socket otherwise (windowsill#5881 R3).
    try:
        ws.close()
    except OSError:
        pass


def _socket_dir() -> str:
    runtime = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    if runtime:
        return os.path.join(runtime, "voice-loop")
    # Fallback: voice-loop state directory with mode 0700. Match XDG_STATE_HOME default.
    state = os.environ.get("XDG_STATE_HOME", "").strip() or os.path.expanduser(
        "~/.local/state"
    )
    return os.path.join(state, "voice-loop", "relay")


def _socket_path() -> str:
    return os.path.join(_socket_dir(), "stt.sock")


def _ensure_dir_mode(path: str, mode: int = 0o700) -> None:
    """Create ``path`` with mode 0700 (relaxed only by umask bits the user already owns)."""
    if not os.path.isdir(path):
        os.makedirs(path, mode=mode, exist_ok=False)
        # Re-assert mode after makedirs (umask may have narrowed it).
        os.chmod(path, mode)
    else:
        st = os.stat(path)
        if (st.st_mode & 0o777) != mode:
            os.chmod(path, mode)


def _probe_existing(path: str) -> bool:
    """One probe connection. ``True`` means another relay is live at this path."""
    s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    s.settimeout(SOCKET_PROBE_TIMEOUT_SECONDS)
    try:
        s.connect(path)
    except (OSError, _socket.error):
        return False
    finally:
        try:
            s.close()
        except OSError:
            pass
    return True


def _bind_socket(path: str) -> _socket.socket | None:
    """Bind a fresh AF_UNIX socket. Returns the listening socket or ``None`` on EADDRINUSE."""
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError:
        return None
    s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    try:
        s.bind(path)
    except OSError:
        return None
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    s.listen(8)
    return s


def _relay_loop() -> None:
    """Outer loop: own the stale socket, accept connections until the process exits.

    On start: create the directory with mode 0700. If the socket path exists, probe it
    once. Accepted → another relay is live, this instance skips binding and only serves
    MCP tools (we keep looping to retry the bind every REBIND_RETRY_SECONDS so a
    surviving session can take over within that interval). Refused / ENOENT → unlink
    and bind. We never read config.json — the only state we hold is the two env keys.
    """
    sock_dir = _socket_dir()
    sock_path = _socket_path()
    _ensure_dir_mode(sock_dir, 0o700)
    listener: _socket.socket | None = None
    while True:
        if listener is None:
            if os.path.exists(sock_path):
                if _probe_existing(sock_path):
                    # Another relay is live. Sleep and retry — when the binding session
                    # exits, the next probe will fail and we'll unlink+bind.
                    time.sleep(REBIND_RETRY_SECONDS)
                    continue
                # Stale. Try to unlink and bind. If unlink fails (race), loop.
                try:
                    os.unlink(sock_path)
                except FileNotFoundError:
                    pass
                except OSError:
                    time.sleep(REBIND_RETRY_SECONDS)
                    continue
            listener = _bind_socket(sock_path)
            if listener is None:
                time.sleep(REBIND_RETRY_SECONDS)
                continue
        try:
            client_sock, addr = listener.accept()
        except OSError:
            listener.close()
            listener = None
            continue
        t = threading.Thread(
            target=_serve_one_client, args=(client_sock, addr), daemon=True
        )
        t.start()


# --- MCP stdio loop ------------------------------------------------------


def _send(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def _recv() -> dict | None:
    line = sys.stdin.readline()
    if not line:
        return None
    try:
        return json.loads(line)
    except ValueError:
        return None


def _tool_design_previews(args: dict) -> dict:
    voice_description = args.get("voice_description", "")
    text = args.get("text", "")
    if not isinstance(voice_description, str) or not isinstance(text, str):
        return {"isError": True, "content": [{"type": "text", "text": "bad args"}]}
    key = _tts_key_from_env()
    if not key:
        return {
            "isError": True,
            "content": [{"type": "text", "text": "no tts_api_key in /config"}],
        }
    body = json.dumps({"voice_description": voice_description, "text": text}).encode()
    req = urllib.request.Request(
        ELEVENLABS_PREVIEWS_URL,
        data=body,
        headers={
            "xi-api-key": key,
            "Content-Type": "application/json",
        },
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=120) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as err:
        return {
            "isError": True,
            "content": [{"type": "text", "text": f"elevenlabs http {err.code}"}],
        }
    except (TimeoutError, urllib.error.URLError, OSError):
        return {
            "isError": True,
            "content": [{"type": "text", "text": "provider unreachable"}],
        }
    try:
        doc = json.loads(raw)
        previews = doc.get("previews", [])
    except ValueError:
        return {
            "isError": True,
            "content": [{"type": "text", "text": "provider returned non-JSON"}],
        }
    out_dir = os.path.expanduser("~/.local/share/voice-loop/previews")
    os.makedirs(out_dir, exist_ok=True)
    results = []
    for i, p in enumerate(previews, 1):
        if not isinstance(p, dict):
            continue
        audio_b64 = p.get("audio_base_64", "")
        generated_voice_id = p.get("generated_voice_id", "")
        try:
            audio = base64.b64decode(audio_b64)
        except (ValueError, TypeError):
            continue
        path = os.path.join(out_dir, f"preview-{i}.mp3")
        tmp = path + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(audio)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        results.append({"generated_voice_id": generated_voice_id, "path": path})
    return {"content": [{"type": "text", "text": json.dumps(results)}]}


def _tool_design_save(args: dict) -> dict:
    generated_voice_id = args.get("generated_voice_id", "")
    voice_name = args.get("voice_name", "")
    voice_description = args.get("voice_description", "")
    if not all(
        isinstance(s, str) and s
        for s in (generated_voice_id, voice_name, voice_description)
    ):
        return {"isError": True, "content": [{"type": "text", "text": "bad args"}]}
    key = _tts_key_from_env()
    if not key:
        return {
            "isError": True,
            "content": [{"type": "text", "text": "no tts_api_key in /config"}],
        }
    body = json.dumps(
        {
            "voice_name": voice_name,
            "voice_description": voice_description,
            "generated_voice_id": generated_voice_id,
        }
    ).encode()
    req = urllib.request.Request(
        ELEVENLABS_CREATE_URL,
        data=body,
        headers={
            "xi-api-key": key,
            "Content-Type": "application/json",
        },
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=120) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as err:
        return {
            "isError": True,
            "content": [{"type": "text", "text": f"elevenlabs http {err.code}"}],
        }
    except (TimeoutError, urllib.error.URLError, OSError):
        return {
            "isError": True,
            "content": [{"type": "text", "text": "provider unreachable"}],
        }
    try:
        doc = json.loads(raw)
        voice_id = doc.get("voice_id", "")
    except ValueError:
        return {
            "isError": True,
            "content": [{"type": "text", "text": "provider returned non-JSON"}],
        }
    return {"content": [{"type": "text", "text": json.dumps({"voice_id": voice_id})}]}


_TOOLS = {
    "design_previews": {
        "description": "Generate ElevenLabs voice-design previews from an English voice_description and a sample text. Returns [{generated_voice_id, path}] with each preview written under ~/.local/share/voice-loop/previews.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "voice_description": {"type": "string"},
                "text": {"type": "string"},
            },
            "required": ["voice_description", "text"],
        },
    },
    "design_save": {
        "description": "Save a chosen ElevenLabs preview as a permanent voice. Returns {voice_id}.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "generated_voice_id": {"type": "string"},
                "voice_name": {"type": "string"},
                "voice_description": {"type": "string"},
            },
            "required": ["generated_voice_id", "voice_name", "voice_description"],
        },
    },
}


def _dispatch(msg: dict) -> dict | None:
    method = msg.get("method", "")
    req_id = msg.get("id")
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "serverInfo": {"name": "voice-loop", "version": _read_plugin_version()},
                "capabilities": {"tools": {}},
            },
        }
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": list(_TOOLS.values())},
        }
    if method == "tools/call":
        params = msg.get("params", {})
        name = params.get("name", "")
        args = params.get("arguments", {})
        if name == "design_previews":
            result = _tool_design_previews(args)
        elif name == "design_save":
            result = _tool_design_save(args)
        else:
            result = {"isError": True, "content": [{"type": "text", "text": "unknown tool"}]}
        return {"jsonrpc": "2.0", "id": req_id, "result": result}
    if req_id is not None:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"method not found: {method}"},
        }
    return None


def _stdio_loop() -> None:
    """The MCP stdio loop. The relay socket runs in a daemon thread."""
    t = threading.Thread(target=_relay_loop, daemon=True)
    t.start()
    while True:
        msg = _recv()
        if msg is None:
            return
        reply = _dispatch(msg)
        if reply is not None:
            _send(reply)


def main(argv: list[str] | None = None) -> int:
    """The single entry point: the stdio MCP server with the relay as a daemon thread.

    The three flags that lived here (``--probe-socket-only``, ``--once-stt``,
    ``--print-config``) had no callers; the manifest's ``mcpServers`` block
    launches this script with no arguments, and the loopback CI starts it the
    same way. Deleting them is a no-op for the manifest and the loopback; the
    MCP server, both design tools and the relay all still ship (windowsill#5870, R9).
    """
    if argv:
        # Be loud rather than silent on a flag the manifest does not pass: the
        # ticket named the three retired flags as the load, but a stray argument
        # is a configuration error worth surfacing rather than ignoring.
        sys.stderr.write(
            f"voice_mcp: unexpected arguments: {argv!r}; the MCP server takes no flags\n"
        )
        return 2
    _stdio_loop()
    return 0


if __name__ == "__main__":
    sys.exit(main())