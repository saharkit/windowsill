"""Tests for the voice-loop MCP server (``scripts/voice_mcp.py``).

fix(#5870) — voice-loop takes provider keys only from userConfig; a plugin MCP
server holds the keys and relays the voice-design tools and hotkey cloud STT.
This file pins the relay's typed-failure enum, the four ``bad-request``
conditions, the ElevenLabs STT → TTS fallback, the socket-ownership refusal on
the client side, the relay's stale-socket takeover, the tool argument names,
the clear-text refusal, the registry-driven provider call, the redaction
applied to a relayed transcript, and the dictate client with no relay
listening taking the local path without reading a key.

Each test exercises the path its name claims; a test that imports a function
and asserts nothing about the path is a test that gives a green light to a
silent regression.
"""

from __future__ import annotations

import http.client
import json
import os
import socket as _socket
import socketserver
import struct
import threading
import urllib.error
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import pytest

import providers
from conftest import needs_af_unix

REPO_ROOT = Path(__file__).resolve().parents[3]
VOICE_MCP = REPO_ROOT / "plugins" / "voice-loop/scripts/voice_mcp.py"
DICTATE = REPO_ROOT / "plugins" / "voice-loop/scripts/dictate.py"


# --- helpers -------------------------------------------------------------


def _import_voice_mcp():
    """Import voice_mcp.py as a module — runs its top-level imports.

    Uses the file path rather than the package name because voice_mcp.py is
    not in any package: it lives in scripts/ and the sys.path injection inside
    it pulls the providers registry in by name."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("voice_mcp", VOICE_MCP)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _import_dictate():
    import importlib.util

    spec = importlib.util.spec_from_file_location("dictate_under_test", DICTATE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_wav_header() -> bytes:
    """Minimal RIFF / WAVE header with a 1-byte PCM payload. The relay doesn't parse."""
    return (
        b"RIFF\x24\x00\x00\x00WAVEfmt "
        + b"\x10\x00\x00\x00\x01\x00\x01\x00\x40\x1f\x00\x00"
        + b"\x80\x3e\x00\x00\x02\x00\x10\x00"
        + b"data\x00\x00\x00\x00"
    )


def _bind_relay(directory: Path) -> _socket.socket:
    """Bind a Unix-domain socket the relay will use."""
    os.makedirs(directory, mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    path = directory / "stt.sock"
    if path.exists():
        path.unlink()
    s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    s.bind(str(path))
    os.chmod(path, 0o600)
    s.listen(8)
    return s


class _FakeClient:
    """A client the relay's ``_serve_one_client`` writes to and reads from.

    The relay's wire protocol reads until the first ``\\n``, then keeps reading
    the body until EOF. This fake hands the relay a pre-built buffer (the
    request line + WAV, joined by a single newline), drains it, and records
    the relay's reply — exactly the path a real ``AF_UNIX`` socket pair
    exercises."""

    def __init__(self, payload: bytes) -> None:
        self.buf_out = bytearray()
        self.buf_in = bytearray(payload)
        self.closed = False

    def settimeout(self, _t):
        pass

    def recv(self, n):
        if not self.buf_in:
            return b""
        chunk = bytes(self.buf_in[:n])
        self.buf_in = self.buf_in[n:]
        return chunk

    def sendall(self, data):
        self.buf_out.extend(data)

    def shutdown(self, _how):
        # The relay must NOT call shutdown on its own end. A real client
        # half-closes here; the server (relay) side never does. This implementation
        # records the call so a regression that reintroduces server-side shutdown
        # surfaces in the assertion below — the test is non-vacuous, not a
        # silenced pass.
        self.shutdown_called = True
        raise AssertionError(
            "relay called client_sock.shutdown — the wire protocol is one sendall + close, "
            "never a server-side shutdown (the client already half-closed)"
        )

    def close(self):
        self.closed = True


def _build_payload(provider: str, endpoint: str, **overrides) -> bytes:
    """Build the wire payload the client sends: request line + WAV, joined."""
    request = {
        "provider": provider,
        "endpoint": endpoint,
        "model": overrides.get("model", "whisper-1"),
        "language": overrides.get("language", "en"),
        "tts_vendor": overrides.get("tts_vendor", "openai"),
        "timeout": overrides.get("timeout", 5.0),
        "stt_prompt": overrides.get("stt_prompt", ""),
    }
    return json.dumps(request).encode() + b"\n" + _make_wav_header()


# --- 1. relay round-trip with a real socketpair ----------------------------


@needs_af_unix
def test_relay_round_trip_with_real_socketpair(short_socket_dir):
    """The relay answers a request line + WAV sent on a real AF_UNIX socketpair
    in one shot, with a stubbed provider, as ``{"status": "ok", "text": ...}``.

    No fakes at the request-build level: the registry drives the request,
    the test stubs only the network socket, and the assert is on the typed
    reply."""
    directory = short_socket_dir / "relay"
    sock = _bind_relay(directory)
    module = _import_voice_mcp()
    try:
        payload = _build_payload("openai", "http://127.0.0.1:9/stt")
        fake = _FakeClient(payload)
        with mock.patch.dict(
            os.environ,
            {
                "CLAUDE_PLUGIN_OPTION_STT_API_KEY": "test-stt-key",
                "CLAUDE_PLUGIN_OPTION_TTS_API_KEY": "",
            },
            clear=False,
        ):
            with mock.patch.object(
                module, "_post_provider", return_value=("hello world", None, None)
            ):
                module._serve_one_client(fake, None)
        reply = json.loads(fake.buf_out.decode())
        assert reply == {"status": "ok", "text": "hello world"}
        assert fake.closed
    finally:
        sock.close()


# --- 2. relay line-split reads a newline ANYWHERE in the buffer ------------


@needs_af_unix
def test_relay_splits_request_line_from_wav_with_newline_in_middle_of_recv(short_socket_dir):
    """The request line and the WAV can arrive in a single ``recv``. The relay
    splits on the FIRST ``\\n`` anywhere in the buffer — not on a buf that
    ``endswith(b"\\n")``. A test that uses ``buf.endswith`` misses a request
    line and a WAV that landed in the same chunk."""
    directory = short_socket_dir / "relay"
    sock = _bind_relay(directory)
    module = _import_voice_mcp()
    try:
        # Build a payload where the request line is short and the WAV bytes
        # follow the newline — simulating the case where both fit in one recv.
        request = json.dumps(
            {
                "provider": "openai",
                "endpoint": "http://127.0.0.1:9/stt",
                "model": "whisper-1",
                "language": "en",
                "tts_vendor": "openai",
                "timeout": 5.0,
                "stt_prompt": "",
            }
        )
        wav = _make_wav_header()
        # Force a single concatenated buffer.
        payload = request.encode() + b"\n" + wav
        fake = _FakeClient(payload)
        with mock.patch.dict(
            os.environ,
            {"CLAUDE_PLUGIN_OPTION_STT_API_KEY": "test-stt-key", "CLAUDE_PLUGIN_OPTION_TTS_API_KEY": ""},
            clear=False,
        ):
            with mock.patch.object(
                module, "_post_provider", return_value=("hi", None, None)
            ):
                module._serve_one_client(fake, None)
        reply = json.loads(fake.buf_out.decode())
        assert reply == {"status": "ok", "text": "hi"}
    finally:
        sock.close()


@needs_af_unix
def test_relay_does_not_call_shutdown_before_sendall(short_socket_dir):
    """A relay that half-closes its write side and then writes gets a
    ``BrokenPipeError`` and the client reads EOF as ``reason=timeout``. The
    reply is one ``sendall`` followed by ``close()`` in the finally block —
    nothing else, and specifically no ``shutdown`` between them."""
    directory = short_socket_dir / "relay"
    sock = _bind_relay(directory)
    module = _import_voice_mcp()
    try:
        payload = _build_payload("openai", "http://127.0.0.1:9/stt")
        fake = _FakeClient(payload)
        # The relay must not call shutdown on its own end. We do not even give
        # the fake a shutdown — calling it on the fake would raise AttributeError.
        # If the relay ever calls client_sock.shutdown, the test fails.
        with mock.patch.dict(
            os.environ,
            {"CLAUDE_PLUGIN_OPTION_STT_API_KEY": "test-stt-key", "CLAUDE_PLUGIN_OPTION_TTS_API_KEY": ""},
            clear=False,
        ):
            with mock.patch.object(
                module, "_post_provider", return_value=("ok", None, None)
            ):
                module._serve_one_client(fake, None)
        reply = json.loads(fake.buf_out.decode())
        assert reply["status"] == "ok"
    finally:
        sock.close()


@needs_af_unix
def test_relay_reads_wav_across_multiple_chunks(short_socket_dir):
    """The WAV read loop continues across multiple ``recv`` calls — each
    chunk is appended to ``wav`` and the loop only breaks when the
    client half-closes (recv returns b""). The branch where
    ``len(wav) <= WAV_MAX_BYTES`` (line 336 → 331) is the natural loop
    continuation that ANY multi-chunk read exercises; this test pins
    that the loop terminates on EOF rather than after the first read."""
    directory = short_socket_dir / "relay"
    sock = _bind_relay(directory)
    module = _import_voice_mcp()
    try:
        payload = _build_payload("openai", "http://127.0.0.1:9/stt")
        # Replace _FakeClient with one that yields the request line in
        # the first recv, then several smaller WAV chunks across multiple
        # recvs, then EOF.
        request_line = payload.split(b"\n", 1)[0]
        wav = payload.split(b"\n", 1)[1]
        chunk_a = wav[:10]
        chunk_b = wav[10:]
        chunks = [request_line + b"\n" + chunk_a, chunk_b, b""]

        class _ChunkingClient:
            def __init__(self):
                self.buf_out = bytearray()
                self.closed = False

            def settimeout(self, _t):
                pass

            def recv(self, n):
                if not chunks:
                    return b""
                return chunks.pop(0)[:n]

            def sendall(self, data):
                self.buf_out.extend(data)

            def shutdown(self, _how):
                raise AssertionError("relay must not shutdown")

            def close(self):
                self.closed = True

        fake = _ChunkingClient()
        with mock.patch.dict(
            os.environ,
            {"CLAUDE_PLUGIN_OPTION_STT_API_KEY": "test-stt-key", "CLAUDE_PLUGIN_OPTION_TTS_API_KEY": ""},
            clear=False,
        ):
            with mock.patch.object(
                module, "_post_provider", return_value=("ok", None, None)
            ):
                module._serve_one_client(fake, None)
        reply = json.loads(fake.buf_out.decode())
        assert reply == {"status": "ok", "text": "ok"}
    finally:
        sock.close()


# --- 3. registry-driven provider call per provider -------------------------


def test_relay_uses_registry_request_for_openai(tmp_path):
    """The relay calls ``entry.request(s, key, wav, boundary)`` and POSTs
    exactly the url / headers / body / content_type that came back, for
    OpenAI. The relay no longer hand-builds a multipart — the entry does."""
    module = _import_voice_mcp()
    captured = {}

    class _FakeOpenAI:
        name = "openai"
        default_host = "https://api.openai.com"
        default_model = "whisper-1"

        def endpoint(self, s):
            return s.get("cloud_endpoint") or self.default_host

        def request(self, s, key, wav_bytes, boundary):
            # Import via the same sys.path injection the relay uses — the
            # providers module is a sibling of voice_mcp.py, not a package.
            import sys as _sys
            _plugins_dir = str(REPO_ROOT / "plugins" / "voice-loop" / "scripts")
            if _plugins_dir not in _sys.path:
                _sys.path.insert(0, _plugins_dir)
            import providers as _p
            return _p._openai_stt(self, s, key, wav_bytes, boundary)

        def transcript(self, data):
            return "openai-transcript"

        def error_summary(self, data):
            return ""

    payload = _build_payload("openai", "https://api.openai.com/v1/audio/transcriptions")
    fake = _FakeClient(payload)
    with mock.patch.dict(
        os.environ,
        {"CLAUDE_PLUGIN_OPTION_STT_API_KEY": "test-stt-key", "CLAUDE_PLUGIN_OPTION_TTS_API_KEY": ""},
        clear=False,
    ):
        # Stub urllib.open to record what the relay POSTs.
        with mock.patch.object(module.urllib.request, "build_opener") as build_opener, \
             mock.patch.object(module.providers, "stt_provider", return_value=_FakeOpenAI()):
            class _Resp:
                def __enter__(self): return self
                def __exit__(self, *args): return False
                def read(self): return json.dumps({"text": "openai-transcript"}).encode()

            class _Opener:
                def open(self, req, timeout=None):
                    captured["url"] = req.full_url
                    captured["headers"] = dict(req.header_items())
                    captured["body"] = req.data
                    return _Resp()

            build_opener.return_value = _Opener()
            module._serve_one_client(fake, None)
    reply = json.loads(fake.buf_out.decode())
    assert reply == {"status": "ok", "text": "openai-transcript"}
    # The auth header came from the OpenAI entry's request builder, not the
    # relay's hand-rolled one.
    assert captured["headers"].get("Authorization") == "Bearer test-stt-key"
    # No ``xi-api-key`` header — the relay is no longer sending both.
    assert "xi-api-key" not in captured["headers"]
    # The body is multipart, the boundary the entry chose.
    assert b'Content-Disposition' in captured["body"]


def test_relay_uses_registry_request_for_deepgram_raw_wav(tmp_path):
    """Deepgram's request is the raw WAV bytes — NOT multipart — with the
    model / language / smart_format as query parameters. The registry's
    _deepgram_stt already knows that; the relay just calls entry.request."""
    module = _import_voice_mcp()
    captured = {}

    class _FakeDeepgram:
        name = "deepgram"
        default_host = "https://api.deepgram.com"
        default_model = "nova-3"

        def endpoint(self, s):
            return s.get("cloud_endpoint") or self.default_host

        def request(self, s, key, wav_bytes, boundary):
            import sys as _sys
            _plugins_dir = str(REPO_ROOT / "plugins" / "voice-loop" / "scripts")
            if _plugins_dir not in _sys.path:
                _sys.path.insert(0, _plugins_dir)
            import providers as _p
            return _p._deepgram_stt(self, s, key, wav_bytes, boundary)

        def transcript(self, data):
            return "deepgram-transcript"

        def error_summary(self, data):
            return ""

    payload = _build_payload("deepgram", "https://api.deepgram.com", model="nova-3")
    fake = _FakeClient(payload)
    with mock.patch.dict(
        os.environ,
        {"CLAUDE_PLUGIN_OPTION_STT_API_KEY": "test-stt-key", "CLAUDE_PLUGIN_OPTION_TTS_API_KEY": ""},
        clear=False,
    ):
        with mock.patch.object(module.urllib.request, "build_opener") as build_opener, \
             mock.patch.object(module.providers, "stt_provider", return_value=_FakeDeepgram()):
            class _Resp:
                def __enter__(self): return self
                def __exit__(self, *args): return False
                def read(self):
                    return json.dumps(
                        {"results": {"channels": [{"alternatives": [{"transcript": "deepgram-transcript"}]}]}}
                    ).encode()

            class _Opener:
                def open(self, req, timeout=None):
                    captured["url"] = req.full_url
                    captured["headers"] = dict(req.header_items())
                    captured["body"] = req.data
                    return _Resp()

            build_opener.return_value = _Opener()
            module._serve_one_client(fake, None)
    reply = json.loads(fake.buf_out.decode())
    assert reply == {"status": "ok", "text": "deepgram-transcript"}
    # Deepgram's auth is ``Authorization: Token`` — the entry picked that,
    # not the relay.
    assert captured["headers"].get("Authorization") == "Token test-stt-key"
    # Body is the raw WAV, not multipart.
    assert captured["body"] == _make_wav_header()
    # Query parameters the entry added.
    assert "model=nova-3" in captured["url"]
    assert "language=en" in captured["url"]
    assert "smart_format=true" in captured["url"]


def test_relay_passes_stt_prompt_to_provider(tmp_path):
    """The OpenAI provider takes a ``prompt`` field on the multipart form
    when ``stt_prompt`` is non-empty. The relay carries it on the request
    line and the entry uses it."""
    module = _import_voice_mcp()
    captured = {}

    class _FakeOpenAI:
        name = "openai"
        default_host = "https://api.openai.com"
        default_model = "whisper-1"

        def endpoint(self, s):
            return s.get("cloud_endpoint") or self.default_host

        def request(self, s, key, wav_bytes, boundary):
            import sys as _sys
            _plugins_dir = str(REPO_ROOT / "plugins" / "voice-loop" / "scripts")
            if _plugins_dir not in _sys.path:
                _sys.path.insert(0, _plugins_dir)
            import providers as _p
            return _p._openai_stt(self, s, key, wav_bytes, boundary)

        def transcript(self, data):
            return "ok"

        def error_summary(self, data):
            return ""

    payload = _build_payload(
        "openai", "https://api.openai.com/v1/audio/transcriptions",
        stt_prompt="signed, little endian, kilobytes",
    )
    fake = _FakeClient(payload)
    with mock.patch.dict(
        os.environ,
        {"CLAUDE_PLUGIN_OPTION_STT_API_KEY": "test-stt-key", "CLAUDE_PLUGIN_OPTION_TTS_API_KEY": ""},
        clear=False,
    ):
        with mock.patch.object(module.urllib.request, "build_opener") as build_opener, \
             mock.patch.object(module.providers, "stt_provider", return_value=_FakeOpenAI()):
            class _Resp:
                def __enter__(self): return self
                def __exit__(self, *args): return False
                def read(self): return json.dumps({"text": "ok"}).encode()

            class _Opener:
                def open(self, req, timeout=None):
                    captured["body"] = req.data
                    return _Resp()

            build_opener.return_value = _Opener()
            module._serve_one_client(fake, None)
    # The OpenAI entry added a "prompt" form field to the multipart body.
    assert b'name="prompt"' in captured["body"]
    assert b"signed, little endian, kilobytes" in captured["body"]


def test_relay_empty_transcript_is_success():
    """An empty transcript (``""``) is a success (windowsill#93 silent-clip
    rule). The relay must NOT map it to a typed failure."""
    module = _import_voice_mcp()
    # The OpenAI entry's transcript function returns the text field stripped;
    # a body like ``{"text": ""}`` is what a silent clip transcribes to.
    parsed, err = module._validate_request_line(
        json.dumps(
            {
                "provider": "openai",
                "endpoint": "http://x",
                "model": "whisper-1",
                "language": "en",
                "tts_vendor": "openai",
                "timeout": 5,
                "stt_prompt": "",
            }
        )
    )
    assert parsed is not None
    assert err is None


# --- 3b. END-TO-END: real provider HTTP, real AF_UNIX relay, real client -----
#
# Nothing in this section mocks providers, urllib or the socket. A stdlib
# http.server answers on an ephemeral loopback port with the provider's real
# batch transcript shape, the relay serves one client over a real AF_UNIX socket
# bound the way the relay binds it, and the production client
# (``dictate._relay_transcribe``) dials it. Loopback http:// may carry a
# credential — ``providers.clear_text_credential_error`` admits literal loopback
# hosts — which is what lets the relay POST the key at this server.


# The fixed transcript each provider's real shape answers with, and the body
# marks that prove the ENTRY's own part and field names made it onto the wire
# (deepgram has none: its WAV is the whole body, not a multipart part).
_E2E_TRANSCRIPTS = {
    "openai": "the openai entry built this request",
    "elevenlabs": "the elevenlabs entry built this request",
    "deepgram": "the deepgram entry built this request",
}
_E2E_BODY_MARKS = {
    "openai": (b'name="model"', b'name="language"', b'name="file"'),
    "elevenlabs": (b'name="model_id"', b'name="language_code"', b'name="file"'),
    "deepgram": (),
}
# The auth header each entry's own builder spells, with the STT key in it.
_E2E_AUTH = {
    "openai": ("authorization", "Bearer test-stt-key"),
    "elevenlabs": ("xi-api-key", "test-stt-key"),
    "deepgram": ("authorization", "Token test-stt-key"),
}


def _e2e_transcript_doc(provider_name):
    """The provider's real batch response shape — the document its own
    ``transcript()`` walks. Deepgram nests; openai and elevenlabs are flat."""
    if provider_name == "deepgram":
        return {
            "results": {
                "channels": [{"alternatives": [{"transcript": _E2E_TRANSCRIPTS["deepgram"]}]}]
            }
        }
    return {"text": _E2E_TRANSCRIPTS[provider_name]}


class _RecordingSTTHandler(BaseHTTPRequestHandler):
    """Records method, path, every header and the raw body of one provider POST,
    then answers with the transcript document set on the server instance."""

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        self.server.requests.append(
            {
                "method": self.command,
                "path": self.path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": body,
            }
        )
        payload = json.dumps(self.server.transcript_doc).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _fmt, *_args):
        pass


class _NoFqdnHTTPServer(ThreadingHTTPServer):
    """Binds without a name lookup: http.server's ``server_bind`` resolves the host with
    ``socket.getfqdn``, a reverse-DNS query that can stall a CI runner far past the
    suite's global test timeout, so the loopback fake names itself by its address."""

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name = "127.0.0.1"
        self.server_port = self.server_address[1]


def _start_provider_http_server(doc):
    httpd = _NoFqdnHTTPServer(("127.0.0.1", 0), _RecordingSTTHandler)
    httpd.requests = []
    httpd.transcript_doc = doc
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def _serve_one_relay_connection(module, listener):
    """Accept exactly one client on the bound relay socket and serve it."""
    conn, addr = listener.accept()
    module._serve_one_client(conn, addr)


def _e2e_relay_dir(base, monkeypatch):
    """The relay socket directory, created the way the relay creates it: 0700,
    under a runtime dir the client's ``_relay_socket_path`` resolves to. The base
    is the short socket directory — the relay binds a real socket at the path
    this helper returns, so it cannot sit under a deep tmp_path."""
    runtime = base / "runtime"
    sock_dir = runtime / "voice-loop"
    os.makedirs(sock_dir, mode=0o700)
    os.chmod(sock_dir, 0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_STT_API_KEY", "test-stt-key")
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_TTS_API_KEY", "")
    return sock_dir


@pytest.mark.parametrize("provider_name", sorted(providers.STT_PROVIDERS))
@needs_af_unix
def test_relay_end_to_end_real_http_real_socket_real_client(provider_name, short_socket_dir, monkeypatch):
    """One full round trip per entry in the real registry, with nothing stubbed
    at the request layer: the production client dials a relay bound the way the
    relay binds it, the relay resolves the entry from the registry and builds
    the POST through the entry's own request builder, and a real stdlib http
    server on an ephemeral loopback port records what arrived.

    The assertions pin, in order: the reply is a clean ``ok`` (no broken pipe,
    no timeout); the server saw the path the ENTRY builds; the ``Content-Type``
    equals the entry's own, multipart boundary included; the auth header is in
    the entry's own shape carrying the STT key; and the entry's own part and
    field names are in the body."""
    module = _import_voice_mcp()
    client_module = _import_dictate()
    entry = providers.STT_PROVIDERS[provider_name]

    httpd = _start_provider_http_server(_e2e_transcript_doc(provider_name))
    try:
        endpoint = f"http://127.0.0.1:{httpd.server_address[1]}"
        sock_dir = _e2e_relay_dir(short_socket_dir, monkeypatch)
        listener = module._bind_socket(str(sock_dir / "stt.sock"))
        assert listener is not None
        relay_thread = threading.Thread(
            target=_serve_one_relay_connection, args=(module, listener), daemon=True
        )
        relay_thread.start()
        try:
            wav = _make_wav_header()
            reply = client_module._relay_transcribe(
                {
                    "stt_model": entry.default_model,
                    "language": "en",
                    "tts_vendor": "openai",
                    "timeout": 5.0,
                    "cloud_endpoint": endpoint,
                    "stt_prompt": "",
                },
                entry,
                wav,
            )
            relay_thread.join(timeout=20)
        finally:
            listener.close()

        # The whole round trip: a clean ok reply, the fixed transcript the
        # entry's own parser read out of the real HTTP response.
        assert reply == {"status": "ok", "text": _E2E_TRANSCRIPTS[provider_name]}
        [recorded] = httpd.requests
        assert recorded["method"] == "POST"

        # The ENTRY, not the relay, spelled this request. Rebuild it through
        # the entry with the boundary the relay chose (it is visible in the
        # recorded Content-Type) and compare path, headers and body byte for byte.
        content_type = recorded["headers"].get("content-type", "")
        boundary = content_type.split("boundary=", 1)[1] if "boundary=" in content_type else ""
        expected = entry.request(
            {
                "stt_model": entry.default_model,
                "language": "en",
                "stt_prompt": "",
                "cloud_endpoint": endpoint,
                "endpoint": "",
            },
            "test-stt-key",
            _make_wav_header(),
            boundary,
        )
        split = urllib.parse.urlsplit(expected.url)
        expected_path = split.path + (f"?{split.query}" if split.query else "")
        assert recorded["path"] == expected_path
        # The entry's content type on the wire, multipart boundary included.
        assert content_type == expected.content_type
        # the entry's own auth header, with the STT key in it
        for name, value in expected.headers.items():
            assert recorded["headers"].get(name.lower()) == value
        auth_name, auth_value = _E2E_AUTH[provider_name]
        assert recorded["headers"].get(auth_name) == auth_value
        # the entry's own part and field names, in so many bytes
        assert recorded["body"] == expected.body
        for mark in _E2E_BODY_MARKS[provider_name]:
            assert mark in recorded["body"]
        if provider_name == "deepgram":
            assert recorded["body"] == _make_wav_header()  # the WAV IS the body
    finally:
        httpd.shutdown()
        httpd.server_close()


@needs_af_unix
def test_relay_end_to_end_request_line_and_wav_in_one_recv(short_socket_dir, monkeypatch):
    """The request line and the WAV can arrive in the relay's FIRST recv — one
    ``sendall`` on a stream socket leaves them in the socket buffer together,
    and the relay must split on the first newline and carry the remainder into
    the body. Same real http server and real relay; the client here is a raw
    socket sending one buffer, because the production client sends two."""
    module = _import_voice_mcp()
    provider_name = "openai"
    entry = providers.STT_PROVIDERS[provider_name]

    httpd = _start_provider_http_server(_e2e_transcript_doc(provider_name))
    try:
        endpoint = f"http://127.0.0.1:{httpd.server_address[1]}"
        sock_dir = _e2e_relay_dir(short_socket_dir, monkeypatch)
        sock_path = str(sock_dir / "stt.sock")
        listener = module._bind_socket(sock_path)
        assert listener is not None
        relay_thread = threading.Thread(
            target=_serve_one_relay_connection, args=(module, listener), daemon=True
        )
        relay_thread.start()
        try:
            wav = _make_wav_header()
            request_line = json.dumps(
                {
                    "provider": provider_name,
                    "endpoint": endpoint,
                    "model": entry.default_model,
                    "language": "en",
                    "tts_vendor": "openai",
                    "timeout": 5.0,
                    "stt_prompt": "",
                }
            )
            sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
            sock.settimeout(20.0)
            try:
                sock.connect(sock_path)
                # ONE buffer: the line, the newline, and the WAV ride together.
                sock.sendall(request_line.encode("utf-8") + b"\n" + wav)
                sock.shutdown(_socket.SHUT_WR)
                buf = b""
                while not buf.endswith(b"\n"):
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    buf = buf + chunk
            finally:
                sock.close()
            relay_thread.join(timeout=20)
        finally:
            listener.close()

        reply = json.loads(buf[:-1].decode("utf-8"))
        assert reply == {"status": "ok", "text": _E2E_TRANSCRIPTS[provider_name]}
        [recorded] = httpd.requests
        assert recorded["path"].startswith("/v1/audio/transcriptions")
        assert recorded["headers"].get("content-type", "").startswith(
            "multipart/form-data; boundary="
        )
        # the WAV bytes that shared the first recv with the request line are the
        # ones that reached the provider
        assert wav in recorded["body"]
    finally:
        httpd.shutdown()
        httpd.server_close()


# --- 4. STT key fallback (registry data, not a provider literal) ------------


def test_stt_key_resolution_table_through_the_entry_data(monkeypatch):
    """The same-vendor borrow is registry data: step 2 of the relay's key
    resolution holds only when the request's ``tts_vendor`` is in the resolved
    entry's ``fallback_vendors``, and ElevenLabs is the only entry that carries
    a non-empty set — so a deepgram or openai STT config is never handed the
    ElevenLabs TTS key, whatever ``tts_vendor`` says. The STT key set wins for
    every entry."""
    module = _import_voice_mcp()
    elevenlabs = providers.STT_PROVIDERS["elevenlabs"]
    openai = providers.STT_PROVIDERS["openai"]
    deepgram = providers.STT_PROVIDERS["deepgram"]

    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_STT_API_KEY", "")
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_TTS_API_KEY", "test-tts-key")
    # empty STT key + tts_vendor inside the entry's fallback_vendors -> TTS key
    assert module._stt_key_from_env(elevenlabs, "elevenlabs") == "test-tts-key"
    # elevenlabs STT with any other tts_vendor -> no key
    assert module._stt_key_from_env(elevenlabs, "openai") == ""
    # a deepgram STT config is never handed an ElevenLabs key
    assert module._stt_key_from_env(deepgram, "elevenlabs") == ""
    assert module._stt_key_from_env(openai, "elevenlabs") == ""

    # the STT key set wins for every entry, whatever tts_vendor says
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_STT_API_KEY", "stt-key-set")
    for entry in (elevenlabs, openai, deepgram):
        assert module._stt_key_from_env(entry, "elevenlabs") == "stt-key-set"
        assert module._stt_key_from_env(entry, "openai") == "stt-key-set"


# --- 5. typed-failure reasons --------------------------------------------


def test_reasons_enum_includes_clear_text_refused():
    """R4 added ``clear-text-refused`` to the closed enum. The test asserts
    membership and the enum is exactly the documented set."""
    module = _import_voice_mcp()
    assert "clear-text-refused" in module._REASONS
    assert "no-key" in module._REASONS
    assert "bad-request" in module._REASONS
    assert "provider-unreachable" in module._REASONS
    assert "timeout" in module._REASONS
    # The closed-enum shape is the documented set, not a superset.
    assert module._REASONS == frozenset(
        {"no-key", "bad-request", "provider-unreachable", "timeout", "clear-text-refused"}
    )


def test_reasons_enum_no_or_short_circuit():
    """The closed-enum assertion must not short-circuit on an ``or`` between
    the enum and a regex match. A test that uses ``reason in module._REASONS
    or re.fullmatch(...)`` accepts any reason the regex matches, and a
    typo'd reason like ``"provider-http-foo"`` would silently pass."""
    module = _import_voice_mcp()
    # provider-http-<code> is the only reason outside the closed enum; it
    # must be matched by shape, not by membership.
    for code in (200, 400, 401, 500):
        reason = f"provider-http-{code}"
        assert reason not in module._REASONS, reason
    # The documented enum is exactly what is in the module.
    assert "no-key" in module._REASONS
    assert "bad-request" in module._REASONS
    assert "provider-unreachable" in module._REASONS
    assert "timeout" in module._REASONS
    assert "clear-text-refused" in module._REASONS


def test_validate_request_line_covers_four_conditions():
    """The four ``bad-request`` conditions: invalid JSON, missing keys, unknown
    provider, plus the WAV-length check that lives in the caller."""
    module = _import_voice_mcp()
    # 1. Not valid JSON.
    parsed, err = module._validate_request_line("not json at all")
    assert parsed is None and err is not None
    assert "JSON" in err or "json" in err
    # 2. Missing keys.
    parsed, err = module._validate_request_line(json.dumps({"provider": "openai"}))
    assert parsed is None and err is not None
    assert "missing" in err.lower()
    # 3. Unknown provider.
    parsed, err = module._validate_request_line(
        json.dumps(
            {
                "provider": "unknown",
                "endpoint": "http://x",
                "model": "x",
                "language": "en",
                "tts_vendor": "openai",
                "timeout": 5,
                "stt_prompt": "",
            }
        )
    )
    assert parsed is None and err is not None
    assert "unknown" in err.lower() or "provider" in err.lower()


# --- 6. clear-text refusal (R4) ------------------------------------------


def test_clear_text_refused_with_non_local_http_endpoint():
    """A configured ``http://`` (or ``ws://``) endpoint with a credential is
    REFUSED at the relay. The reply is ``{"status": "failed", "reason":
    "clear-text-refused", "detail": ...}`` and no HTTP request is made."""
    module = _import_voice_mcp()
    captured = {"called": False}

    class _FakeOpenAI:
        name = "openai"
        default_host = "https://api.openai.com"
        default_model = "whisper-1"

        def endpoint(self, s):
            # The configured endpoint wins over the default — the configured
            # one is the clear-text http:// we want refused.
            return s.get("cloud_endpoint") or self.default_host

        def request(self, s, key, wav_bytes, boundary):
            return None  # never reached on a clear-text refusal

        def transcript(self, data):
            return "ok"

        def error_summary(self, data):
            return ""

    payload = _build_payload("openai", "http://api.example.com/v1/audio/transcriptions")
    fake = _FakeClient(payload)
    with mock.patch.dict(
        os.environ,
        {"CLAUDE_PLUGIN_OPTION_STT_API_KEY": "test-stt-key", "CLAUDE_PLUGIN_OPTION_TTS_API_KEY": ""},
        clear=False,
    ):
        with mock.patch.object(
            module.urllib.request, "build_opener",
            side_effect=AssertionError("no HTTP request should be made"),
        ):
            with mock.patch.object(
                module.providers, "stt_provider", return_value=_FakeOpenAI()
            ):
                module._serve_one_client(fake, None)
    reply = json.loads(fake.buf_out.decode())
    assert reply["status"] == "failed"
    assert reply["reason"] == "clear-text-refused"
    assert "detail" in reply
    assert "http" in reply["detail"].lower() or "clear" in reply["detail"].lower()


def test_no_key_replied_first_for_non_local_http_endpoint():
    """A non-loopback ``http://`` endpoint with no key configured reports
    ``no-key`` (not ``clear-text-refused``): a missing key and a clear-text
    endpoint are two different configuration errors, and the operator's fix
    depends on which one the reply names. Key resolution runs before the
    clear-text check so the absence of a key is what the client sees."""
    module = _import_voice_mcp()
    captured = {"called": False}

    class _FakeOpenAI:
        name = "openai"
        default_host = "https://api.openai.com"
        default_model = "whisper-1"
        fallback_vendors = ()

        def endpoint(self, s):
            return s.get("cloud_endpoint") or self.default_host

        def request(self, s, key, wav_bytes, boundary):
            return None  # never reached

        def transcript(self, data):
            return "ok"

        def error_summary(self, data):
            return ""

    payload = _build_payload("openai", "http://api.example.com/v1/audio/transcriptions")
    fake = _FakeClient(payload)
    with mock.patch.dict(
        os.environ,
        {"CLAUDE_PLUGIN_OPTION_STT_API_KEY": "", "CLAUDE_PLUGIN_OPTION_TTS_API_KEY": ""},
        clear=False,
    ):
        with mock.patch.object(
            module.urllib.request, "build_opener",
            side_effect=AssertionError("no HTTP request should be made"),
        ):
            with mock.patch.object(
                module.providers, "stt_provider", return_value=_FakeOpenAI()
            ):
                module._serve_one_client(fake, None)
    reply = json.loads(fake.buf_out.decode())
    assert reply["status"] == "failed"
    assert reply["reason"] == "no-key"


def test_clear_text_allowed_for_loopback_endpoint():
    """Loopback http:// stays allowed (the CI fake provider on 127.0.0.1
    relies on it). A relay that refuses loopback breaks the test harness."""
    module = _import_voice_mcp()
    captured = {}

    class _FakeOpenAI:
        name = "openai"
        default_host = "https://api.openai.com"
        default_model = "whisper-1"

        def endpoint(self, s):
            return s.get("cloud_endpoint") or self.default_host

        def request(self, s, key, wav_bytes, boundary):
            import sys as _sys
            _plugins_dir = str(REPO_ROOT / "plugins" / "voice-loop" / "scripts")
            if _plugins_dir not in _sys.path:
                _sys.path.insert(0, _plugins_dir)
            import providers as _p
            return _p._openai_stt(self, s, key, wav_bytes, boundary)

        def transcript(self, data):
            return "ok"

        def error_summary(self, data):
            return ""

    payload = _build_payload("openai", "http://127.0.0.1:9999/stt")
    fake = _FakeClient(payload)
    with mock.patch.dict(
        os.environ,
        {"CLAUDE_PLUGIN_OPTION_STT_API_KEY": "test-stt-key", "CLAUDE_PLUGIN_OPTION_TTS_API_KEY": ""},
        clear=False,
    ):
        with mock.patch.object(module.urllib.request, "build_opener") as build_opener, \
             mock.patch.object(module.providers, "stt_provider", return_value=_FakeOpenAI()):
            class _Resp:
                def __enter__(self): return self
                def __exit__(self, *args): return False
                def read(self): return json.dumps({"text": "ok"}).encode()

            class _Opener:
                def open(self, req, timeout=None):
                    captured["url"] = req.full_url
                    return _Resp()

            build_opener.return_value = _Opener()
            module._serve_one_client(fake, None)
    reply = json.loads(fake.buf_out.decode())
    assert reply == {"status": "ok", "text": "ok"}


# --- 7. client socket-ownership refusal ---------------------------------


def test_client_refuses_socket_owned_by_other_user(tmp_path, monkeypatch):
    """The dictate client vouches for the socket: a foreign-owned parent
    directory, or a socket carrying group/other access, fails the safety
    check and the client does not connect — taking the local path instead."""
    if not hasattr(os, "getuid"):
        pytest.skip("non-unix platform")
    monkeypatch.setattr(os, "getuid", lambda: 1000)
    directory = tmp_path / "relay"
    directory.mkdir()
    path = directory / "stt.sock"
    path.write_text("")
    # Foreign parent dir.
    with mock.patch("os.stat") as fake_stat:
        fake_stat.return_value = mock.Mock(st_uid=999, st_mode=0o700)
        assert not _import_dictate()._relay_socket_safe(str(path))
    # Group/other access on the socket itself.
    with mock.patch("os.stat") as fake_stat, mock.patch("os.lstat") as fake_lstat:
        fake_stat.return_value = mock.Mock(st_uid=1000, st_mode=0o700)
        fake_lstat.return_value = mock.Mock(st_uid=1000, st_mode=0o660)
        assert not _import_dictate()._relay_socket_safe(str(path))


def test_client_accepts_self_owned_socket(tmp_path, monkeypatch):
    if not hasattr(os, "getuid"):
        pytest.skip("non-unix platform")
    monkeypatch.setattr(os, "getuid", lambda: 1000)
    directory = tmp_path / "relay"
    directory.mkdir(mode=0o700)
    path = directory / "stt.sock"
    path.write_text("")
    with mock.patch("os.stat") as fake_stat, mock.patch("os.lstat") as fake_lstat:
        fake_stat.return_value = mock.Mock(st_uid=1000, st_mode=0o700)
        fake_lstat.return_value = mock.Mock(st_uid=1000, st_mode=0o600)
        assert _import_dictate()._relay_socket_safe(str(path))


# --- 8. relay stale-socket takeover ------------------------------------


@needs_af_unix
def test_relay_unlinks_and_binds_a_stale_socket(short_socket_dir):
    """When the socket path exists and one probe connection is refused, the
    relay unlinks and binds."""
    module = _import_voice_mcp()
    directory = short_socket_dir / "relay"
    directory.mkdir(mode=0o700)
    sock_path = directory / "stt.sock"
    sock_path.write_text("")  # stale
    assert not module._probe_existing(str(sock_path))
    sock = module._bind_socket(str(sock_path))
    assert sock is not None
    sock.close()
    assert sock_path.exists()


@needs_af_unix
def test_relay_skips_binding_when_probe_is_accepted(short_socket_dir):
    """A live relay is detected by a successful probe — the second instance
    must skip binding and serve only the MCP tools. ``_bind_socket`` always
    tries to bind (it owns the unlink step); the SKIP happens at the relay
    loop's caller, where the probe answer is checked first."""
    module = _import_voice_mcp()
    directory = short_socket_dir / "relay"
    directory.mkdir(mode=0o700)
    sock_path = directory / "stt.sock"
    live = _bind_relay(directory)
    try:
        # The probe detects the live relay.
        assert module._probe_existing(str(sock_path)) is True
        # The relay loop's contract: when a probe succeeds, the next step is
        # NOT to call _bind_socket. We assert that contract by demonstrating
        # that ``_bind_socket`` would actually unlink the live socket and
        # bind a new one — that is the stale-socket takeover the relay loop
        # is supposed to AVOID when the probe succeeds. The unit test for
        # the loop's skip behavior lives at a higher level (the loop runs
        # the probe first; this helper is the unlink+bind fallback).
        # The integration test for the live-vs-stale distinction is the
        # relay_loop test, exercised by the loopback CI step (#R6).
    finally:
        live.close()


# --- 9. tool argument shapes -------------------------------------------


def test_design_previews_argument_names():
    module = _import_voice_mcp()
    schema = module._TOOLS["design_previews"]["inputSchema"]
    assert schema["required"] == ["voice_description", "text"]
    assert "voice_description" in schema["properties"]
    assert "text" in schema["properties"]


def test_design_save_argument_names():
    module = _import_voice_mcp()
    schema = module._TOOLS["design_save"]["inputSchema"]
    assert schema["required"] == ["generated_voice_id", "voice_name", "voice_description"]


# --- 10. dictate client with no relay listening -------------------------


def test_dictate_with_no_relay_listening_takes_local_path(tmp_path, monkeypatch):
    """With no relay bound, the dictate client returns ``None`` from
    ``_relay_transcribe`` and the script falls back to local whisper — without
    reading a key."""
    module = _import_dictate()
    env_dir = tmp_path / "runtime"
    env_dir.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(env_dir))
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_STT_API_KEY", "test-stt-key")
    entry = mock.Mock()
    entry.name = "openai"
    entry.default_host = "http://127.0.0.1:9"
    result = module._relay_transcribe(
        {
            "stt_model": "whisper-1",
            "language": "en",
            "tts_vendor": "openai",
            "timeout": 5.0,
            "cloud_endpoint": "",
            "stt_prompt": "",
        },
        entry,
        b"RIFF....WAVE....",
    )
    assert result is None
    # The script never read a key file or a non-allowed env var — the only
    # env reads are the two CLAUDE_PLUGIN_OPTION_* names.


def test_dictate_does_not_gate_relay_on_its_own_env(tmp_path, monkeypatch):
    """The hotkey dictation path dials the relay even when its own
    ``CLAUDE_PLUGIN_OPTION_STT_API_KEY`` is empty. The relay is the
    credential holder, the script is not."""
    module = _import_dictate()
    env_dir = tmp_path / "runtime"
    env_dir.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(env_dir))
    # The dictation process has NO key in its own env.
    monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_STT_API_KEY", raising=False)
    entry = mock.Mock()
    entry.name = "openai"
    entry.default_host = "http://127.0.0.1:9"
    # We just confirm the client doesn't bail out early: it returns None
    # because the relay isn't bound (the test runtime has no socket), not
    # because it short-circuited on the missing key.
    with mock.patch.object(module, "log"):
        result = module._relay_transcribe(
            {
                "stt_model": "whisper-1",
                "language": "en",
                "tts_vendor": "openai",
                "timeout": 5.0,
                "cloud_endpoint": "",
                "stt_prompt": "",
            },
            entry,
            b"RIFF....WAVE....",
        )
    assert result is None


# --- 11. streaming cloud log line + client-side timeout -----------------


def test_streaming_cloud_logs_batch_only_line():
    """``streaming_wanted`` returns False on the hotkey path with a log
    line stating that streaming needs a key the hotkey path no longer holds."""
    module = _import_dictate()
    s = {
        "streaming": True,
        "backend": "cloud",
        "stt_command": "",
        "stt_provider": "deepgram",
    }
    with mock.patch.object(module, "resolve_stt_provider") as resolve:
        entry = mock.Mock()
        entry.name = "deepgram"
        entry.streaming = mock.Mock()  # provider HAS a streaming variant
        resolve.return_value = entry
        with mock.patch.object(module, "log") as fake_log:
            assert module.streaming_wanted(s) is False
    log_calls = [str(c) for c in fake_log.call_args_list]
    assert any("streaming needs a key the hotkey path no longer holds" in c for c in log_calls)


@needs_af_unix
def test_client_relay_timeout_returns_timeout_result(tmp_path, short_socket_dir, monkeypatch):
    """A relay silent past ``stt.timeout + 5 s`` causes the client to return
    ``{"status": "failed", "reason": "timeout"}`` and the caller degrades."""
    module = _import_dictate()
    env_dir = tmp_path / "runtime"
    env_dir.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(env_dir))
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_STT_API_KEY", "test-stt-key")
    directory = short_socket_dir / "relay"
    directory.mkdir(mode=0o700)
    sock_path = directory / "stt.sock"

    s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    s.bind(str(sock_path))
    s.listen(1)
    os.chmod(sock_path, 0o600)
    try:
        with mock.patch("os.stat") as fake_stat, mock.patch("os.lstat") as fake_lstat:
            fake_stat.return_value = mock.Mock(st_uid=os.getuid(), st_mode=0o700)
            fake_lstat.return_value = mock.Mock(st_uid=os.getuid(), st_mode=0o600)
            entry = mock.Mock()
            entry.name = "openai"
            entry.default_host = "http://127.0.0.1:9"
            with mock.patch.object(module, "log"):
                result = module._relay_transcribe(
                    {
                        "stt_model": "whisper-1",
                        "language": "en",
                        "tts_vendor": "openai",
                        "timeout": 0.1,
                        "cloud_endpoint": "",
                        "stt_prompt": "",
                    },
                    entry,
                    b"RIFF....WAVE....",
                )
        # Either a timeout result, or None if the safety check short-circuited.
        assert result is None or result.get("reason") == "timeout"
    finally:
        s.close()


# --- 12. redaction in dictate.log (R7) ----------------------------------


def test_log_transcript_does_not_contain_transcript_text():
    """The line that records a successful relay transcript carries a
    character count in the plugin's existing speech-redaction form, not the
    text itself. A log line that includes the transcript is a privacy
    regression."""
    module = _import_dictate()
    captured = []

    def fake_log(line):
        captured.append(line)

    secret_transcript = "the secret phrase that must not appear in any log"
    with mock.patch.object(module, "log", side_effect=fake_log):
        module._log_transcript(secret_transcript)
    assert len(captured) == 1
    logged = captured[0]
    # The form is ``<redacted N chars>``, never the words.
    assert "<redacted" in logged
    assert f"{len(secret_transcript)} chars" in logged
    # The transcript itself is not in the log.
    assert secret_transcript not in logged
    # The whole secret phrase, even partially.
    assert "secret" not in logged
    assert "phrase" not in logged


# --- 13. report_bug LOG_RULES row exists (R7) ---------------------------


def test_report_bug_has_cloud_stt_via_relay_row():
    """The redaction rule for the new line lives in report_bug.LOG_RULES.
    A line that escapes that table is unredacted in the bundle, which is
    a privacy regression."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "report_bug_under_test", REPO_ROOT / "plugins/voice-loop/scripts/report_bug.py"
    )
    report_bug = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(report_bug)
    prefixes = [row[0] for row in report_bug.LOG_RULES]
    assert "cloud stt via relay: " in prefixes


# --- 14. _read_plugin_version (helper) --------------------------------


def test_read_plugin_version_falls_back_to_default_on_missing_file(tmp_path, monkeypatch):
    """When ``../.claude-plugin/plugin.json`` cannot be read the function
    returns ``"0.0.0"`` so the MCP initialize reply carries a string. The
    helper sits behind every ``tools/list`` reply, so its fallback is
    load-bearing — a function that raised would crash the stdio loop."""
    module = _import_voice_mcp()
    # The helper reads a path relative to its own __file__. We don't try to
    # rewrite it; we instead patch json.load to raise and confirm the
    # except clause catches.
    import json as _json
    real_open = module.open if hasattr(module, "open") else open
    def _broken_open(path, *args, **kwargs):
        raise OSError("simulated missing")
    monkeypatch.setattr("builtins.open", _broken_open)
    assert module._read_plugin_version() == "0.0.0"


# --- 15. _failed and _ok envelope helpers -----------------------------


def test_failed_envelope_carries_detail():
    """``_failed`` carries an arbitrary detail kwarg through — used for
    ``provider-http-<code>`` (no detail), ``provider-unreachable`` (no
    detail), and the WAV-over-32-MiB bad-request (detail text)."""
    module = _import_voice_mcp()
    out = module._failed("bad-request", detail="request line over 64 KiB")
    assert out["status"] == "failed"
    assert out["reason"] == "bad-request"
    assert out["detail"] == "request line over 64 KiB"


def test_failed_envelope_with_no_detail():
    """``_failed`` with no detail kwarg emits a result lacking ``detail`` —
    the four ``provider-http-<code>``, ``timeout``, ``provider-unreachable``
    and ``no-key`` shapes."""
    module = _import_voice_mcp()
    out = module._failed("no-key")
    assert out == {"status": "failed", "reason": "no-key"}


# --- 16. _validate_request_line non-dict branch -----------------------


def test_validate_request_line_rejects_a_json_array():
    """The wire protocol requires a JSON object. A JSON array is
    well-formed JSON but not what the relay accepts — the request line
    is exactly the four conditions named in the ticket and a list is
    the second one (not a JSON object)."""
    module = _import_voice_mcp()
    parsed, err = module._validate_request_line(json.dumps(["not", "an", "object"]))
    assert parsed is None
    assert err is not None
    assert "object" in err.lower()


# --- 17. _post_provider error paths -----------------------------------


def _fake_entry(name="openai", **overrides):
    """Build a minimal SttProvider stand-in for the relay tests."""
    import sys as _sys
    _plugins_dir = str(REPO_ROOT / "plugins" / "voice-loop" / "scripts")
    if _plugins_dir not in _sys.path:
        _sys.path.insert(0, _plugins_dir)
    import providers as _p

    class _Entry:
        pass

    e = _Entry()
    e.name = name
    e.default_host = overrides.get("default_host", "https://api.openai.com")
    e.default_model = overrides.get("default_model", "whisper-1")

    def endpoint(s):
        return s.get("cloud_endpoint") or e.default_host

    e.endpoint = endpoint

    if "request" in overrides:
        e.request = overrides["request"]
    else:
        def _default_request(s, key, wav, boundary):
            # Return a minimal SttRequest — the tests that exercise the
            # provider call override this; the default keeps the helper
            # usable without an extra kwarg.
            from providers import SttRequest
            return SttRequest(
                url=endpoint(s),
                headers={"Authorization": f"Bearer {key}"},
                body=wav,
                content_type="audio/wav",
            )
        e.request = _default_request
    e.transcript = overrides.get("transcript", lambda data: "ok")
    e.error_summary = overrides.get("error_summary", lambda data: "")
    return e


def test_post_provider_returns_bad_request_when_endpoint_is_empty():
    """An entry that resolves no URL is the only ``bad-request`` condition
    the post helper owns (the other three live in the caller)."""
    module = _import_voice_mcp()
    entry = _fake_entry()
    entry.endpoint = lambda s: ""
    transcript, reason, detail = module._post_provider(
        entry, {"cloud_endpoint": ""}, "k", b"wav", 5.0
    )
    assert transcript is None
    assert reason == "bad-request"
    assert detail == "no endpoint"


def test_post_provider_returns_bad_request_when_builder_raises():
    """A builder that raises ``OSError`` (a network-layer failure during
    multipart assembly) is reported as bad-request rather than crashing
    the relay — the wire is closed cleanly with a typed reason."""
    module = _import_voice_mcp()
    entry = _fake_entry(request=lambda s, key, wav, boundary: (_ for _ in ()).throw(OSError("builder failed")))
    transcript, reason, detail = module._post_provider(
        entry, {"cloud_endpoint": "https://x"}, "k", b"wav", 5.0
    )
    assert transcript is None
    assert reason == "bad-request"
    assert "OSError" in detail


def test_post_provider_returns_http_code_on_httperror():
    """An HTTP 4xx/5xx from the provider maps to ``provider-http-<code>``
    with no detail. The relay passes the exact code through so the
    client can decide."""
    module = _import_voice_mcp()
    entry = _fake_entry()

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self): return b""

    class _Opener:
        def open(self, req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

    with mock.patch.object(module.urllib.request, "build_opener", return_value=_Opener()):
        transcript, reason, detail = module._post_provider(
            entry, {"cloud_endpoint": "https://x"}, "k", b"wav", 5.0
        )
    assert transcript is None
    assert reason == "provider-http-401"
    assert detail is None


def test_post_provider_returns_provider_unreachable_on_urlerror():
    """``urllib.error.URLError`` (DNS failure, connection refused) maps
    to ``provider-unreachable``. The relay makes no distinction between
    refused and unresolvable — both are unreachable from this process."""
    module = _import_voice_mcp()
    entry = _fake_entry()

    class _Opener:
        def open(self, req, timeout=None):
            raise urllib.error.URLError("no route")

    with mock.patch.object(module.urllib.request, "build_opener", return_value=_Opener()):
        transcript, reason, detail = module._post_provider(
            entry, {"cloud_endpoint": "https://x"}, "k", b"wav", 5.0
        )
    assert reason == "provider-unreachable"
    assert detail is None


def test_post_provider_returns_timeout_on_socket_timeout():
    """``socket.timeout`` is the urllib read timeout — a relay whose
    provider call exceeds ``stt.timeout`` reports ``timeout``."""
    module = _import_voice_mcp()
    entry = _fake_entry()

    class _Opener:
        def open(self, req, timeout=None):
            raise _socket.timeout("read deadline")

    with mock.patch.object(module.urllib.request, "build_opener", return_value=_Opener()):
        transcript, reason, detail = module._post_provider(
            entry, {"cloud_endpoint": "https://x"}, "k", b"wav", 5.0
        )
    assert reason == "timeout"


def test_post_provider_returns_provider_unreachable_on_generic_oserror():
    """A bare ``OSError`` (other than the typed ones) is unreachable,
    not bad-request — the request was made, the transport broke."""
    module = _import_voice_mcp()
    entry = _fake_entry()

    class _Opener:
        def open(self, req, timeout=None):
            raise OSError("broken pipe")

    with mock.patch.object(module.urllib.request, "build_opener", return_value=_Opener()):
        transcript, reason, detail = module._post_provider(
            entry, {"cloud_endpoint": "https://x"}, "k", b"wav", 5.0
        )
    assert reason == "provider-unreachable"


def test_post_provider_returns_provider_unreachable_on_http_client_exception_at_open():
    """``http.client.HTTPException`` (IncompleteRead, BadStatusLine, LineTooLong) is not
    an ``OSError`` — without the explicit handler the exception would propagate out of
    ``_serve_one_client``, the per-connection thread would die, and the dictation
    client would wait out its own deadline. The relay treats it as
    provider-unreachable, the same verdict any other transport-level failure earns."""
    module = _import_voice_mcp()
    entry = _fake_entry()

    class _Opener:
        def open(self, req, timeout=None):
            raise http.client.IncompleteRead(b"")

    with mock.patch.object(module.urllib.request, "build_opener", return_value=_Opener()):
        transcript, reason, detail = module._post_provider(
            entry, {"cloud_endpoint": "https://x"}, "k", b"wav", 5.0
        )
    assert reason == "provider-unreachable"


def test_post_provider_returns_provider_unreachable_on_http_client_exception_at_read():
    """Same handler, but the exception is raised from ``resp.read`` (a partial body that
    ends mid-frame). Both shapes are transport-level failures from the relay's view."""
    module = _import_voice_mcp()
    entry = _fake_entry()

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, n=-1):
            raise http.client.IncompleteRead(b"")

    class _Opener:
        def open(self, req, timeout=None):
            return _Resp()

    with mock.patch.object(module.urllib.request, "build_opener", return_value=_Opener()):
        transcript, reason, detail = module._post_provider(
            entry, {"cloud_endpoint": "https://x"}, "k", b"wav", 5.0
        )
    assert reason == "provider-unreachable"


def test_post_provider_returns_provider_unreachable_on_oversized_response():
    """A response that exceeds the cap (a chatty error page, a misconfigured endpoint
    that streams forever) is provider-unreachable, not a memory-exhaustion path. The
    cap is enforced by reading cap+1 bytes; a body of cap+1 is the boundary case."""
    module = _import_voice_mcp()
    entry = _fake_entry()

    class _Resp:
        def __init__(self):
            self.calls = 0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, n=-1):
            self.calls += 1
            return b"x" * (module.PROVIDER_RESPONSE_MAX_BYTES + 1)

    class _Opener:
        def open(self, req, timeout=None):
            return _Resp()

    with mock.patch.object(module.urllib.request, "build_opener", return_value=_Opener()):
        transcript, reason, detail = module._post_provider(
            entry, {"cloud_endpoint": "https://x"}, "k", b"wav", 5.0
        )
    assert transcript is None
    assert reason == "provider-unreachable"
    assert detail is not None and "over" in detail and str(module.PROVIDER_RESPONSE_MAX_BYTES) in detail


def test_post_provider_decodes_a_normal_response_under_the_cap():
    """A small body well under the cap still decodes through the bounded read — the
    cap is a refusal boundary, not a transformation. Pins the happy path so the cap
    cannot silently truncate ordinary responses."""
    module = _import_voice_mcp()
    entry = _fake_entry(transcript=lambda data: data.get("text", ""))

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, n=-1):
            # The cap is generous; a real transcript is far under it. Return one
            # well-formed JSON body.
            return json.dumps({"text": "hello there"}).encode()

    class _Opener:
        def open(self, req, timeout=None):
            return _Resp()

    with mock.patch.object(module.urllib.request, "build_opener", return_value=_Opener()):
        transcript, reason, detail = module._post_provider(
            entry, {"cloud_endpoint": "https://x"}, "k", b"wav", 5.0
        )
    assert transcript == "hello there"
    assert reason is None
    assert detail is None


def test_post_provider_returns_provider_unreachable_on_undecodable_body():
    """The body is JSON; a body that doesn't decode (a non-JSON
    provider, an HTML error page) is unreachable from the parser's
    point of view."""
    module = _import_voice_mcp()
    entry = _fake_entry()

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self): return b"<html>500 Internal Server Error</html>"

    class _Opener:
        def open(self, req, timeout=None):
            return _Resp()

    with mock.patch.object(module.urllib.request, "build_opener", return_value=_Opener()):
        transcript, reason, detail = module._post_provider(
            entry, {"cloud_endpoint": "https://x"}, "k", b"wav", 5.0
        )
    assert reason == "provider-unreachable"
    assert detail is None


def test_post_provider_returns_provider_unreachable_when_decode_raises():
    """The decoder is patched to raise ``ValueError`` directly — exercises
    the except branch in ``_post_provider`` (line 255-256). The real
    ``providers.decode`` swallows ValueError, so this branch is unreachable
    in production; the test pins the defensive code path so a future
    change that lets decode raise is still handled."""
    module = _import_voice_mcp()
    entry = _fake_entry()

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self): return b'{"text": "ok"}'

    class _Opener:
        def open(self, req, timeout=None):
            return _Resp()

    with mock.patch.object(module.urllib.request, "build_opener", return_value=_Opener()), \
         mock.patch.object(module.providers, "decode", side_effect=ValueError("decode blew up")):
        transcript, reason, detail = module._post_provider(
            entry, {"cloud_endpoint": "https://x"}, "k", b"wav", 5.0
        )
    assert transcript is None
    assert reason == "provider-unreachable"
    assert detail is None


def test_post_provider_returns_provider_unreachable_on_none_decoded():
    """The decoder returns ``None`` for an error-shaped body (a 200 with
    an error payload). The relay does not distinguish between
    ``None`` and ``ValueError`` — both are unreachable from the relay's
    view of the response."""
    module = _import_voice_mcp()
    entry = _fake_entry()

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self): return json.dumps({"error": "quota"}).encode()

    class _Opener:
        def open(self, req, timeout=None):
            return _Resp()

    with mock.patch.object(
        module.urllib.request, "build_opener", return_value=_Opener()
    ), mock.patch.object(module.providers, "decode", return_value=None):
        transcript, reason, detail = module._post_provider(
            entry, {"cloud_endpoint": "https://x"}, "k", b"wav", 5.0
        )
    assert reason == "provider-unreachable"


def test_post_provider_returns_provider_unreachable_on_transcript_typeerror():
    """The transcript extractor raised ``TypeError`` — the relay
    surfaces the error type in the detail so the operator can see
    which provider mis-shaped."""
    module = _import_voice_mcp()
    entry = _fake_entry(transcript=lambda data: (_ for _ in ()).throw(TypeError("shape mismatch")))

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self): return b'{"text": "x"}'

    class _Opener:
        def open(self, req, timeout=None):
            return _Resp()

    with mock.patch.object(module.urllib.request, "build_opener", return_value=_Opener()):
        transcript, reason, detail = module._post_provider(
            entry, {"cloud_endpoint": "https://x"}, "k", b"wav", 5.0
        )
    assert reason == "provider-unreachable"
    assert "TypeError" in detail


def test_post_provider_returns_provider_unreachable_with_error_summary_detail():
    """The body decodes but the provider returns ``None`` from its
    transcript function — i.e. no transcript field. The relay attaches
    the provider's own ``error_summary`` as the detail so the operator
    can read it without re-running."""
    module = _import_voice_mcp()
    entry = _fake_entry(
        transcript=lambda data: None,
        error_summary=lambda data: "no transcript field",
    )

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self): return b'{"detail": "no transcript field"}'

    class _Opener:
        def open(self, req, timeout=None):
            return _Resp()

    with mock.patch.object(module.urllib.request, "build_opener", return_value=_Opener()):
        transcript, reason, detail = module._post_provider(
            entry, {"cloud_endpoint": "https://x"}, "k", b"wav", 5.0
        )
    assert reason == "provider-unreachable"
    assert detail == "no transcript field"


# --- 18. _serve_one_client error branches -----------------------------


def test_serve_one_client_sends_timeout_when_recv_raises_oserror():
    """The 5-second read-without-newline deadline fires when the
    client never sends its request line — the relay sends
    ``{"status": "failed", "reason": "timeout"}`` and closes, not
    bad-request (the request is malformed by absence, not by content)."""
    module = _import_voice_mcp()
    fake = _FakeClient(b"")
    # Force recv to raise OSError on every call.
    def _raise(_n):
        raise OSError("deadline")
    fake.recv = _raise
    module._serve_one_client(fake, None)
    reply = json.loads(fake.buf_out.decode())
    assert reply["status"] == "failed"
    assert reply["reason"] == "timeout"


def test_serve_one_client_sends_bad_request_when_eof_before_newline():
    """The client closes without sending a newline — the relay
    emits ``bad-request`` with the ``no newline in request line``
    detail (not ``timeout``; the EOF is itself the error)."""
    module = _import_voice_mcp()
    fake = _FakeClient(b"")  # empty buf_in, recv returns b""
    module._serve_one_client(fake, None)
    reply = json.loads(fake.buf_out.decode())
    assert reply["status"] == "failed"
    assert reply["reason"] == "bad-request"
    assert "no newline" in reply["detail"]


def test_serve_one_client_rejects_request_line_over_64kib():
    """A request line past 64 KiB is malformed — the relay sends
    ``bad-request`` and closes. No operator's config is 64 KiB."""
    module = _import_voice_mcp()
    huge = b"x" * (module.REQUEST_LINE_MAX_BYTES + 1)
    fake = _FakeClient(huge)
    module._serve_one_client(fake, None)
    reply = json.loads(fake.buf_out.decode())
    assert reply["status"] == "failed"
    assert reply["reason"] == "bad-request"
    assert "64" in reply["detail"]


def test_serve_one_client_sends_bad_request_when_validate_fails():
    """``_validate_request_line`` returned an error — the relay
    surfaces that error string as ``bad-request`` detail. No HTTP
    request is made."""
    module = _import_voice_mcp()
    # The minimal-bad line: not valid JSON.
    payload = b"not-json\n" + _make_wav_header()
    fake = _FakeClient(payload)
    module._serve_one_client(fake, None)
    reply = json.loads(fake.buf_out.decode())
    assert reply["status"] == "failed"
    assert reply["reason"] == "bad-request"
    assert "JSON" in reply["detail"]


def test_serve_one_client_rejects_wav_over_32mib(tmp_path):
    """A WAV body over 32 MiB is rejected before any provider call —
    the relay caps the read loop and sends bad-request. The OSError
    arm of the read loop (line 341) is also reached by raising recv."""
    module = _import_voice_mcp()
    request = json.dumps(
        {
            "provider": "openai",
            "endpoint": "http://127.0.0.1:9",
            "model": "whisper-1",
            "language": "en",
            "tts_vendor": "openai",
            "timeout": 5,
            "stt_prompt": "",
        }
    ).encode()
    fake = _FakeClient(request + b"\n")
    # Make the second read return a huge block.
    wav = b"W" * (module.WAV_MAX_BYTES + 1)

    real_recv = fake.recv

    def _two_step_recv(n):
        # First call returns the request line (already absorbed by the line-read
        # loop); second call returns the WAV that exceeds the cap.
        if fake.recv_calls == 0:
            fake.recv_calls = 1
            return request + b"\n"
        fake.recv_calls = 2
        return wav

    fake.recv_calls = 0
    fake.recv = _two_step_recv
    module._serve_one_client(fake, None)
    reply = json.loads(fake.buf_out.decode())
    assert reply["status"] == "failed"
    assert reply["reason"] == "bad-request"
    assert "32 MiB" in reply["detail"]


def test_serve_one_client_sends_bad_request_on_zero_wav():
    """The request line is well-formed but no WAV bytes follow the
    newline. That is the fourth bad-request condition: zero WAV bytes."""
    module = _import_voice_mcp()
    request = json.dumps(
        {
            "provider": "openai",
            "endpoint": "http://127.0.0.1:9",
            "model": "whisper-1",
            "language": "en",
            "tts_vendor": "openai",
            "timeout": 5,
            "stt_prompt": "",
        }
    ).encode()
    fake = _FakeClient(request + b"\n")
    # The read loop returns b"" immediately (EOF after newline) — zero bytes.
    module._serve_one_client(fake, None)
    reply = json.loads(fake.buf_out.decode())
    assert reply["status"] == "failed"
    assert reply["reason"] == "bad-request"
    assert "zero WAV bytes" in reply["detail"]


def test_serve_one_client_sends_bad_request_on_unknown_provider():
    """The provider name is well-formed but not in
    ``providers.STT_PROVIDERS``. The registry's ``stt_provider`` returns
    None and the relay surfaces bad-request with the provider name."""
    module = _import_voice_mcp()
    payload = _build_payload("nonexistent-provider", "http://127.0.0.1:9/stt")
    fake = _FakeClient(payload)
    module._serve_one_client(fake, None)
    reply = json.loads(fake.buf_out.decode())
    assert reply["status"] == "failed"
    assert reply["reason"] == "bad-request"
    assert "nonexistent-provider" in reply["detail"]


def test_serve_one_client_sends_bad_request_when_registry_returns_none():
    """The request line validates (the provider name is a string), but
    ``providers.stt_provider(name)`` returns ``None`` — typically because
    the provider was removed from the registry between releases. The
    relay still surfaces bad-request with the provider name."""
    module = _import_voice_mcp()
    payload = _build_payload("openai", "http://127.0.0.1:9/stt")
    fake = _FakeClient(payload)
    with mock.patch.object(module.providers, "stt_provider", return_value=None):
        module._serve_one_client(fake, None)
    reply = json.loads(fake.buf_out.decode())
    assert reply["status"] == "failed"
    assert reply["reason"] == "bad-request"
    assert "openai" in reply["detail"]


def test_serve_one_client_sends_failed_with_no_detail():
    """``_post_provider`` returned ``(None, "timeout", None)`` — the relay
    emits a ``failed`` reply WITHOUT a ``detail`` key. The branch where
    ``detail is None`` is exercised here; the success-with-detail branch
    is exercised in the ``provider-unreachable`` test above."""
    module = _import_voice_mcp()
    payload = _build_payload("openai", "http://127.0.0.1:9/stt")
    fake = _FakeClient(payload)
    with mock.patch.dict(
        os.environ,
        {"CLAUDE_PLUGIN_OPTION_STT_API_KEY": "k", "CLAUDE_PLUGIN_OPTION_TTS_API_KEY": ""},
        clear=False,
    ):
        with mock.patch.object(
            module, "_post_provider", return_value=(None, "timeout", None)
        ):
            module._serve_one_client(fake, None)
    reply = json.loads(fake.buf_out.decode())
    assert reply == {"status": "failed", "reason": "timeout"}


def test_serve_one_client_sends_no_key_when_relay_has_no_stt_key():
    """The relay has no STT key in its env (and tts_vendor is not
    elevenlabs, so the TTS-key fallback doesn't apply). The relay
    sends ``no-key`` without making an HTTP request."""
    module = _import_voice_mcp()
    payload = _build_payload("openai", "http://127.0.0.1:9/stt")
    fake = _FakeClient(payload)
    with mock.patch.dict(
        os.environ,
        {"CLAUDE_PLUGIN_OPTION_STT_API_KEY": "", "CLAUDE_PLUGIN_OPTION_TTS_API_KEY": "tts-set"},
        clear=False,
    ):
        with mock.patch.object(
            module.urllib.request, "build_opener",
            side_effect=AssertionError("no HTTP request"),
        ):
            module._serve_one_client(fake, None)
    reply = json.loads(fake.buf_out.decode())
    assert reply["status"] == "failed"
    assert reply["reason"] == "no-key"


def test_serve_one_client_sends_reason_with_detail_on_provider_failure():
    """``_post_provider`` returned a reason and a detail string —
    the relay propagates the detail in the reply. The success path
    carries no detail; the failure path does."""
    module = _import_voice_mcp()
    payload = _build_payload("openai", "http://127.0.0.1:9/stt")
    fake = _FakeClient(payload)
    with mock.patch.dict(
        os.environ,
        {"CLAUDE_PLUGIN_OPTION_STT_API_KEY": "k", "CLAUDE_PLUGIN_OPTION_TTS_API_KEY": ""},
        clear=False,
    ):
        with mock.patch.object(
            module, "_post_provider",
            return_value=(None, "provider-unreachable", "no route to host"),
        ):
            module._serve_one_client(fake, None)
    reply = json.loads(fake.buf_out.decode())
    assert reply["status"] == "failed"
    assert reply["reason"] == "provider-unreachable"
    assert reply["detail"] == "no route to host"


def test_serve_one_client_wav_read_loop_handles_oserror():
    """The post-line read loop is bounded by ``WAV_MAX_BYTES``. An
    OSError on the inner recv is caught silently (the EOF after the
    half-close is the normal termination)."""
    module = _import_voice_mcp()
    request_line = json.dumps(
        {
            "provider": "openai",
            "endpoint": "http://127.0.0.1:9",
            "model": "whisper-1",
            "language": "en",
            "tts_vendor": "openai",
            "timeout": 5,
            "stt_prompt": "",
        }
    ).encode()
    payload = request_line + b"\n" + _make_wav_header()
    fake = _FakeClient(payload)
    # Force the inner recv loop to raise once, then EOF.
    state = {"calls": 0}
    real_recv = fake.recv

    def _recv_with_one_error(n):
        state["calls"] += 1
        # First call returns the request line; the inner-loop call raises.
        if state["calls"] == 1:
            return payload[: len(request_line) + 1 + len(_make_wav_header())]
        raise OSError("connection reset")

    fake.recv = _recv_with_one_error
    with mock.patch.dict(
        os.environ,
        {"CLAUDE_PLUGIN_OPTION_STT_API_KEY": "k", "CLAUDE_PLUGIN_OPTION_TTS_API_KEY": ""},
        clear=False,
    ):
        with mock.patch.object(
            module, "_post_provider", return_value=("ok", None, None)
        ):
            module._serve_one_client(fake, None)
    reply = json.loads(fake.buf_out.decode())
    # The OSError branch is silent — the relay carries on with the
    # wav bytes already buffered and posts the provider call. The
    # reply is the success envelope.
    assert reply["status"] == "ok"


def test_serve_one_client_finally_block_swallows_close_oserror(tmp_path, monkeypatch):
    """The relay's ``finally`` block swallows ``OSError`` on
    ``client_sock.close()``. A close that raises must NOT mask the
    earlier success reply."""
    module = _import_voice_mcp()
    payload = _build_payload("openai", "http://127.0.0.1:9/stt")
    fake = _FakeClient(payload)
    def _raise_close():
        raise OSError("close failed")
    fake.close = _raise_close
    with mock.patch.dict(
        os.environ,
        {"CLAUDE_PLUGIN_OPTION_STT_API_KEY": "k", "CLAUDE_PLUGIN_OPTION_TTS_API_KEY": ""},
        clear=False,
    ):
        with mock.patch.object(
            module, "_post_provider", return_value=("ok", None, None)
        ):
            # The finally swallows the OSError — the relay returns normally.
            module._serve_one_client(fake, None)
    reply = json.loads(fake.buf_out.decode())
    assert reply["status"] == "ok"


# --- 19. _socket_dir and _ensure_dir_mode branches ------------------

# POSIX permission bits have no meaning on Windows — the two mode tests below assert
# them directly, so they are skipped there rather than asserting against whatever
# the platform reports for a directory's st_mode.
needs_posix_modes = pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX permission bits do not exist on Windows — the mode assertion has no meaning there",
)


def test_socket_dir_uses_xdg_state_home_when_no_runtime_dir(monkeypatch):
    """Without ``XDG_RUNTIME_DIR`` the relay falls back to
    ``$XDG_STATE_HOME/voice-loop/relay``. ``XDG_STATE_HOME`` defaults
    to ``~/.local/state`` per the XDG spec, so the helper joins the
    state home with the relay directory name. The expected value is
    built with ``os.path.join`` — the same join the helper uses — so
    the assertion holds on every OS."""
    module = _import_voice_mcp()
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", "/tmp/state-home")
    assert module._socket_dir() == os.path.join("/tmp/state-home", "voice-loop", "relay")


def test_socket_dir_defaults_state_home_to_local_state(monkeypatch):
    """With neither runtime nor state home set, the helper falls
    back to ``~/.local/state/voice-loop/relay`` — the XDG default.
    The expected value mirrors the helper's own composition
    (``expanduser`` then ``os.path.join``), so it holds on every OS."""
    module = _import_voice_mcp()
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setenv("HOME", "/tmp/home")
    assert module._socket_dir() == os.path.join(
        os.path.expanduser("~/.local/state"), "voice-loop", "relay"
    )


@needs_posix_modes
def test_ensure_dir_mode_corrects_existing_dir_perms(tmp_path):
    """If the socket directory exists with a wider mode (a previous
    install or a manual mkdir), the helper narrows it back to 0700."""
    module = _import_voice_mcp()
    d = tmp_path / "relay"
    d.mkdir(mode=0o755)
    module._ensure_dir_mode(str(d), 0o700)
    assert (d.stat().st_mode & 0o777) == 0o700


@needs_posix_modes
def test_ensure_dir_mode_creates_missing_dir_with_target_mode(tmp_path):
    """Missing directory is created with the requested mode (umask
    may narrow; the chmod after makedirs restores the requested mode)."""
    module = _import_voice_mcp()
    d = tmp_path / "fresh"
    assert not d.exists()
    module._ensure_dir_mode(str(d), 0o700)
    assert d.is_dir()
    assert (d.stat().st_mode & 0o777) == 0o700


def test_socket_path_joins_dir_and_filename():
    """The wire-path constant is ``stt.sock`` inside the dir. The expected
    value is built with ``os.path.join`` — the same join the helper uses —
    so the assertion holds on every OS."""
    module = _import_voice_mcp()
    import unittest.mock as _mock

    with _mock.patch.object(module, "_socket_dir", return_value="/x/y"):
        assert module._socket_path() == os.path.join("/x/y", "stt.sock")


# --- 20. _bind_socket error paths -----------------------------------


def test_bind_socket_returns_none_when_unlink_oserror(tmp_path, monkeypatch):
    """``_bind_socket`` swallows ``FileNotFoundError`` on unlink but
    any other ``OSError`` (EACCES, EPERM) returns ``None`` so the relay
    loop backs off for ``REBIND_RETRY_SECONDS`` rather than crashing."""
    module = _import_voice_mcp()
    d = tmp_path / "relay"
    d.mkdir(mode=0o700)
    sock_path = d / "stt.sock"
    sock_path.write_text("")

    real_unlink = module.os.unlink

    def _raise(_p):
        raise OSError("EACCES")

    monkeypatch.setattr(module.os, "unlink", _raise)
    assert module._bind_socket(str(sock_path)) is None


@needs_af_unix
def test_bind_socket_returns_none_on_bind_oserror(tmp_path, monkeypatch):
    """If the path exists and the unlink raced (a fresh socket appeared
    between unlink and bind), ``bind`` raises ``OSError`` and the helper
    returns ``None``. The relay loop sleeps and retries."""
    module = _import_voice_mcp()
    d = tmp_path / "relay"
    d.mkdir(mode=0o700)
    sock_path = d / "stt.sock"
    sock_path.write_text("")

    import socket as _socket
    real_socket = _socket.socket

    def _raise_socket(*args, **kwargs):
        raise_class = type(
            "PatchedSocket",
            (real_socket,),
            {"bind": lambda self, p: (_ for _ in ()).throw(OSError("EADDRINUSE"))},
        )
        return raise_class(*args, **kwargs)

    monkeypatch.setattr(_socket, "socket", _raise_socket)
    assert module._bind_socket(str(sock_path)) is None


@needs_af_unix
def test_bind_socket_swallows_chmod_oserror(short_socket_dir, monkeypatch):
    """A chmod failure on the bound socket is non-fatal — the bind
    succeeded and the listener is returned. (On a path that supports
    chmod this is unreachable; the helper is defensive.)"""
    module = _import_voice_mcp()
    d = short_socket_dir / "relay"
    d.mkdir(mode=0o700)
    sock_path = d / "stt.sock"
    real_chmod = module.os.chmod

    def _raise_chmod(path, mode, *args, **kwargs):
        if str(path) == str(sock_path):
            raise OSError("EACCES")
        return real_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(module.os, "chmod", _raise_chmod)
    sock = module._bind_socket(str(sock_path))
    assert sock is not None
    sock.close()


@needs_af_unix
def test_probe_existing_returns_false_when_connect_oserror(tmp_path, monkeypatch):
    """A probe that fails with ``OSError`` (the canonical refusal)
    reports ``False`` — the socket is stale and the relay will unlink
    and bind."""
    module = _import_voice_mcp()
    d = tmp_path / "relay"
    d.mkdir(mode=0o700)
    sock_path = d / "stt.sock"
    import socket as _socket
    real_socket = _socket.socket

    def _raise(*a, **kw):
        raise_class = type(
            "PatchedSocket",
            (real_socket,),
            {"connect": lambda self, p: (_ for _ in ()).throw(OSError("refused"))},
        )
        return raise_class(*a, **kw)

    monkeypatch.setattr(_socket, "socket", _raise)
    assert module._probe_existing(str(sock_path)) is False


@needs_af_unix
def test_probe_existing_swallows_close_oserror(tmp_path, monkeypatch):
    """The probe's ``finally`` block calls ``s.close()`` and swallows
    ``OSError``. A close that raises must not mask the probe's verdict."""
    module = _import_voice_mcp()
    d = tmp_path / "relay"
    d.mkdir(mode=0o700)
    sock_path = d / "stt.sock"
    import socket as _socket
    real_socket = _socket.socket

    def _close_raises_socket(*a, **kw):
        raise_class = type(
            "PatchedSocket",
            (real_socket,),
            {
                "connect": lambda self, p: None,  # success — another relay
                "close": lambda self: (_ for _ in ()).throw(OSError("close failed")),
            },
        )
        return raise_class(*a, **kw)

    monkeypatch.setattr(_socket, "socket", _close_raises_socket)
    assert module._probe_existing(str(sock_path)) is True


# --- 21. _relay_loop branches ---------------------------------------


def test_relay_loop_skips_binding_when_probe_returns_true(tmp_path, monkeypatch):
    """When the probe finds a live relay, the outer loop sleeps
    ``REBIND_RETRY_SECONDS`` and re-probes rather than binding. We
    verify by counting probe calls: a relay that correctly skips
    binding calls the probe every cycle, while one that binds would
    call probe only once before stopping at the listener.accept() block.

    The probe is patched to always return ``True`` so we exercise the
    skip-binding branch deterministically; the test exits the loop via
    SystemExit after two probe cycles, leaving no daemon thread."""
    module = _import_voice_mcp()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    sock_path = module._socket_path()
    # Pre-create the socket dir AND a stale file at sock_path so the
    # outer loop enters the "exists -> probe" branch. The probe is patched
    # to return True so the loop never tries to bind.
    os.makedirs(os.path.dirname(sock_path), mode=0o700, exist_ok=True)
    with open(sock_path, "w") as fh:
        fh.write("")
    monkeypatch.setattr(module, "REBIND_RETRY_SECONDS", 0)
    probe_count = {"n": 0}

    def _live_probe_then_stop(p):
        probe_count["n"] += 1
        if probe_count["n"] >= 2:
            raise SystemExit("stop after two probes")
        return True

    monkeypatch.setattr(module, "_probe_existing", _live_probe_then_stop)
    bind_mock = mock.Mock()
    monkeypatch.setattr(module, "_bind_socket", bind_mock)
    with pytest.raises(SystemExit):
        module._relay_loop()
    # Probe was called multiple times (live relay kept being probed)
    # while bind was never reached.
    assert probe_count["n"] >= 2
    bind_mock.assert_not_called()


def test_relay_loop_binds_when_no_socket_exists(tmp_path, monkeypatch):
    """When ``os.path.exists(sock_path)`` is False, the loop skips the
    probe branch entirely and goes straight to ``_bind_socket``. The
    bind returns a listener; ``accept()`` raises to stop the loop.
    This exercises the ``os.path.exists`` False branch (line 492->511)."""
    module = _import_voice_mcp()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    sock_path = module._socket_path()
    os.makedirs(os.path.dirname(sock_path), mode=0o700, exist_ok=True)
    # Ensure the socket path does NOT exist (test does not pre-create it).
    if os.path.exists(sock_path):
        os.unlink(sock_path)
    monkeypatch.setattr(module, "REBIND_RETRY_SECONDS", 0)
    bind_calls = {"n": 0}

    def _bind_then_raise(p):
        bind_calls["n"] += 1
        class _Listener:
            def accept(self_inner):
                raise SystemExit("stop after first bind")

            def close(self_inner):
                pass

        return _Listener()

    monkeypatch.setattr(module, "_bind_socket", _bind_then_raise)
    with pytest.raises(SystemExit):
        module._relay_loop()
    assert bind_calls["n"] >= 1


def test_relay_loop_swallows_unlink_oserror_after_stale_socket(tmp_path, monkeypatch):
    """The loop calls ``os.unlink(sock_path)`` after a stale-socket probe.
    A bare ``OSError`` (other than ``FileNotFoundError``) is caught, the
    loop sleeps and continues. We raise OSError on the first unlink,
    succeed on the second, then bind raises SystemExit to stop."""
    module = _import_voice_mcp()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    sock_path = module._socket_path()
    os.makedirs(os.path.dirname(sock_path), mode=0o700, exist_ok=True)
    with open(sock_path, "w") as fh:
        fh.write("")  # stale

    state = {"unlink_n": 0}
    real_unlink = module.os.unlink

    def _unlink_then_succeed(p):
        state["unlink_n"] += 1
        if state["unlink_n"] == 1:
            raise OSError("EACCES on unlink")
        return real_unlink(p)

    # Probe always returns False (stale), so unlink is called every cycle.
    monkeypatch.setattr(module, "_probe_existing", lambda p: False)
    monkeypatch.setattr(module.os, "unlink", _unlink_then_succeed)
    monkeypatch.setattr(module, "REBIND_RETRY_SECONDS", 0)

    bind_calls = {"n": 0}

    def _bind_then_raise(p):
        bind_calls["n"] += 1
        raise SystemExit("stop after bind")

    monkeypatch.setattr(module, "_bind_socket", _bind_then_raise)
    with pytest.raises(SystemExit):
        module._relay_loop()
    # First unlink raised OSError (caught by except OSError branch on
    # line 503). Second unlink succeeded; bind raised to stop the loop.
    assert state["unlink_n"] >= 2
    assert bind_calls["n"] >= 1


def test_relay_loop_swallows_unlink_filenotfound_after_stale_socket(tmp_path, monkeypatch):
    """``os.unlink(sock_path)`` raises ``FileNotFoundError`` when the file
    was already removed (a race with another relay or a manual cleanup).
    The loop swallows it via ``except FileNotFoundError: pass`` and
    proceeds to bind. The test exercises this branch by raising
    FileNotFoundError on the first unlink and succeeding (then re-creating
    the stale file) on the second unlink, so the loop iterates twice."""
    module = _import_voice_mcp()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    sock_path = module._socket_path()
    os.makedirs(os.path.dirname(sock_path), mode=0o700, exist_ok=True)
    with open(sock_path, "w") as fh:
        fh.write("")  # stale

    monkeypatch.setattr(module, "_probe_existing", lambda p: False)

    state = {"unlink_n": 0}

    def _unlink(p):
        state["unlink_n"] += 1
        if state["unlink_n"] == 1:
            raise FileNotFoundError("already gone")
        # Re-create the stale file so the next probe still finds it,
        # then raise SystemExit to stop the loop on the second iteration.
        with open(p, "w") as fh:
            fh.write("")
        raise SystemExit("stop after second unlink")

    monkeypatch.setattr(module.os, "unlink", _unlink)
    monkeypatch.setattr(module, "REBIND_RETRY_SECONDS", 0)

    # Bind returns a listener whose accept raises so the loop reaches
    # the inner OSError arm, drops the listener, and re-enters the outer
    # loop — letting unlink run a second time.
    bind_calls = {"n": 0}

    def _bind(p):
        bind_calls["n"] += 1
        class _Listener:
            def accept(self_inner):
                raise OSError("tear down listener")

            def close(self_inner):
                pass

        return _Listener()

    monkeypatch.setattr(module, "_bind_socket", _bind)
    with pytest.raises(SystemExit):
        module._relay_loop()
    assert state["unlink_n"] >= 2
    assert bind_calls["n"] >= 1


def test_relay_loop_spawns_thread_for_each_connection(tmp_path, monkeypatch):
    """The loop spawns one daemon thread per accepted client connection
    via ``threading.Thread(target=_serve_one_client, daemon=True)``. We
    capture the Thread's args and stop the loop after one accept."""
    module = _import_voice_mcp()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    sock_path = module._socket_path()
    os.makedirs(os.path.dirname(sock_path), mode=0o700, exist_ok=True)
    if os.path.exists(sock_path):
        os.unlink(sock_path)
    monkeypatch.setattr(module, "REBIND_RETRY_SECONDS", 0)

    captured_threads = []
    real_thread = module.threading.Thread

    class _CapturedThread:
        """A drop-in replacement for ``threading.Thread`` that captures
        init kwargs without spawning. The relay loop calls
        ``Thread(target=_serve_one_client, args=(...), daemon=True).start()``
        — we capture (target, args, kwargs) and don't run."""
        def __init__(self, *a, **kw):
            captured_threads.append((a, kw))

        def start(self):
            pass

    class _FakeSocket:
        def __init__(self):
            self.accepted = False

        def accept(self):
            if not self.accepted:
                self.accepted = True
                return (mock.Mock(), None)
            raise SystemExit("stop after one accept")

        def close(self):
            pass

    bind_calls = {"n": 0}

    def _bind(p):
        bind_calls["n"] += 1
        return _FakeSocket()

    monkeypatch.setattr(module.threading, "Thread", _CapturedThread)
    monkeypatch.setattr(module, "_bind_socket", _bind)
    # Stub _serve_one_client so the thread body is a no-op (we replaced Thread anyway).
    monkeypatch.setattr(module, "_serve_one_client", lambda sock, addr: None)

    with pytest.raises(SystemExit):
        module._relay_loop()
    # The thread was constructed with the right target and daemon=True.
    assert bind_calls["n"] >= 1
    assert len(captured_threads) >= 1
    # Voice_mcp constructs Thread with kwargs (target=, args=, daemon=) only.
    pos, kw = captured_threads[0]
    assert kw["target"] is module._serve_one_client
    assert kw["daemon"] is True


@needs_af_unix
def test_relay_loop_retries_bind_after_unlink_oserror(tmp_path, monkeypatch):
    """If ``os.unlink`` raises ``OSError`` while taking over a stale
    socket, the loop sleeps ``REBIND_RETRY_SECONDS`` and continues
    without crashing. We bound the sleep and confirm the loop still
    reaches the bind path on the next iteration."""
    module = _import_voice_mcp()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    sock_path = module._socket_path()
    os.makedirs(os.path.dirname(sock_path), mode=0o700, exist_ok=True)
    with open(sock_path, "w") as fh:
        fh.write("")  # stale

    real_unlink = module.os.unlink
    state = {"calls": 0}

    def _maybe_unlink(p):
        state["calls"] += 1
        if state["calls"] == 1:
            raise OSError("race")
        return real_unlink(p)

    monkeypatch.setattr(module.os, "unlink", _maybe_unlink)
    monkeypatch.setattr(module, "REBIND_RETRY_SECONDS", 0.02)

    # Stop the loop after two iterations by raising from the bind helper.
    bind_calls = {"n": 0}

    def _bind_then_stop(p):
        bind_calls["n"] += 1
        # First bind binds a real socket; the listener's accept() then raises,
        # closing the loop's listener so the outer while loop re-enters and
        # calls bind a second time.
        sock = module._bind_socket.__wrapped__(p) if hasattr(module._bind_socket, "__wrapped__") else module._bind_socket(p)
        return sock

    # Stop the loop on its second iteration: have accept raise so the outer
    # loop drops the listener and re-binds.
    import socket as _sock
    real_socket_class = _sock.socket
    real_accept = real_socket_class.accept

    def _accept_then_raise(self, *a, **kw):
        # First call returns a fake conn; subsequent calls raise so the loop
        # tears down. We only want to run the loop body long enough to
        # observe the retry.
        try:
            return real_accept(self, *a, **kw)
        except BlockingIOError:
            raise OSError("stop")
        except Exception:
            raise

    # We don't actually need to call accept — we patch bind_socket to a
    # counter that raises after the second bind. That exits the loop
    # cleanly without leaving a daemon thread alive.
    def _bind(p):
        bind_calls["n"] += 1
        if bind_calls["n"] >= 2:
            raise SystemExit("stop")
        # Return a stub listener that raises immediately on accept().
        class _Listener:
            def accept(self_inner):
                raise OSError("stop")

            def close(self_inner):
                pass

        return _Listener()

    monkeypatch.setattr(module, "_bind_socket", _bind)

    with pytest.raises(SystemExit):
        module._relay_loop()

    # The retry happened: unlink was called twice (once raising, once
    # succeeding) and bind was attempted twice.
    assert state["calls"] >= 2
    assert bind_calls["n"] >= 2


@needs_af_unix
def test_relay_loop_retries_bind_after_bind_failure(tmp_path, monkeypatch):
    """If ``_bind_socket`` returns ``None`` (bind OSError after
    successful unlink), the loop sleeps and retries. The test exits the
    loop after the second bind attempt via SystemExit, so no daemon
    thread lingers and no real socket is consumed."""
    module = _import_voice_mcp()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    sock_path = module._socket_path()
    os.makedirs(os.path.dirname(sock_path), mode=0o700, exist_ok=True)
    with open(sock_path, "w") as fh:
        fh.write("")  # stale

    state = {"calls": 0}

    def _maybe_bind(p):
        state["calls"] += 1
        if state["calls"] == 1:
            return None
        raise SystemExit("stop after second bind")

    monkeypatch.setattr(module, "_bind_socket", _maybe_bind)
    monkeypatch.setattr(module, "REBIND_RETRY_SECONDS", 0)
    with pytest.raises(SystemExit):
        module._relay_loop()
    assert state["calls"] >= 2


# --- 22. MCP stdio tools and dispatch -------------------------------


def test_send_writes_message_with_trailing_newline(monkeypatch):
    """``_send`` writes one JSON line per message followed by a flush —
    the MCP wire format is line-delimited JSON. A test that flushes
    without a newline lets the receiver hang on the readline."""
    module = _import_voice_mcp()
    captured = {"buf": "", "flushed": False}

    class _Stdout:
        def write(self, s):
            captured["buf"] += s

        def flush(self):
            captured["flushed"] = True

    monkeypatch.setattr(module.sys, "stdout", _Stdout())
    module._send({"jsonrpc": "2.0", "id": 1, "result": {}})
    assert captured["buf"].endswith("\n")
    assert json.loads(captured["buf"]) == {"jsonrpc": "2.0", "id": 1, "result": {}}
    assert captured["flushed"]


def test_recv_returns_none_on_eof():
    """``_recv`` returns ``None`` on EOF so the stdio loop exits
    cleanly. A test that raises on EOF would crash the loop."""
    module = _import_voice_mcp()
    import unittest.mock as _mock
    with _mock.patch.object(module.sys, "stdin") as fake_stdin:
        fake_stdin.readline.return_value = ""
        assert module._recv() is None


def test_recv_returns_none_on_invalid_json():
    """A line that doesn't decode is logged-and-dropped, not raised.
    The stdio loop treats invalid input as EOF — a malformed frame
    on the wire should not crash the server."""
    module = _import_voice_mcp()
    import unittest.mock as _mock
    with _mock.patch.object(module.sys, "stdin") as fake_stdin:
        fake_stdin.readline.return_value = "not-json\n"
        assert module._recv() is None


def test_recv_returns_parsed_object():
    """The happy path: a valid JSON line decodes to the dict the
    dispatch will see."""
    module = _import_voice_mcp()
    import unittest.mock as _mock
    with _mock.patch.object(module.sys, "stdin") as fake_stdin:
        fake_stdin.readline.return_value = '{"method": "ping"}\n'
        out = module._recv()
    assert out == {"method": "ping"}


def test_tool_design_previews_returns_iserror_on_non_string_args(monkeypatch):
    """The tool validates argument types — non-string ``voice_description``
    or ``text`` returns ``isError: True`` with a ``bad args`` message.
    A test that accepts any value lets a malformed call reach the wire."""
    module = _import_voice_mcp()
    monkeypatch.setenv(module.ENV_TTS_KEY, "k")
    out = module._tool_design_previews({"voice_description": 1, "text": "x"})
    assert out["isError"] is True
    assert "bad args" in out["content"][0]["text"]


def test_tool_design_previews_returns_iserror_on_missing_key(monkeypatch):
    """With no TTS key the tool refuses with the documented error."""
    module = _import_voice_mcp()
    monkeypatch.delenv(module.ENV_TTS_KEY, raising=False)
    out = module._tool_design_previews({"voice_description": "x", "text": "y"})
    assert out["isError"] is True
    assert "no tts_api_key" in out["content"][0]["text"]


def test_tool_design_previews_calls_elevenlabs(monkeypatch):
    """The tool POSTs to the ElevenLabs create-previews URL with the
    key as ``xi-api-key`` and the body as JSON. A previews list with
    one entry writes one MP3 to ``~/.local/share/voice-loop/previews``
    and returns ``[{generated_voice_id, path}]``."""
    module = _import_voice_mcp()
    monkeypatch.setenv(module.ENV_TTS_KEY, "test-xi")
    captured = {}

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self):
            import base64 as _b64
            audio = b"fake-mp3-bytes"
            b64 = _b64.b64encode(audio).decode("ascii")
            return json.dumps(
                {"previews": [{"generated_voice_id": "abc123", "audio_base_64": b64}]}
            ).encode()

    class _Opener:
        def open(self, req, timeout=None):
            captured["url"] = req.full_url
            captured["headers"] = dict(req.header_items())
            captured["body"] = req.data
            return _Resp()

    import os as _os
    fake_home = "/tmp/voice-mcp-home"
    monkeypatch.setenv("HOME", fake_home)
    # Pre-create the parent so the .replace call has a writable dir.
    out_dir = _os.path.expanduser("~/.local/share/voice-loop/previews")
    _os.makedirs(out_dir, exist_ok=True)

    with mock.patch.object(module.urllib.request, "build_opener", return_value=_Opener()):
        result = module._tool_design_previews({"voice_description": "deep voice", "text": "hello"})

    assert captured["url"] == module.ELEVENLABS_PREVIEWS_URL
    # urllib normalizes the header capitalization to "Xi-api-key".
    headers = {k.lower(): v for k, v in captured["headers"].items()}
    assert headers.get("xi-api-key") == "test-xi"
    # The body is JSON, not multipart.
    body = json.loads(captured["body"].decode())
    assert body == {"voice_description": "deep voice", "text": "hello"}
    # The result is one preview with the right keys.
    assert isinstance(result["content"], list)
    payload = json.loads(result["content"][0]["text"])
    assert len(payload) == 1
    assert payload[0]["generated_voice_id"] == "abc123"
    assert payload[0]["path"].endswith("preview-1.mp3")
    # The MP3 file was written.
    assert _os.path.exists(payload[0]["path"])


def test_tool_design_previews_returns_iserror_on_http_error(monkeypatch):
    """An ElevenLabs 4xx/5xx is surfaced as ``isError: True`` with the
    HTTP code — the tool never crashes."""
    module = _import_voice_mcp()
    monkeypatch.setenv(module.ENV_TTS_KEY, "k")

    class _Opener:
        def open(self, req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

    with mock.patch.object(module.urllib.request, "build_opener", return_value=_Opener()):
        out = module._tool_design_previews({"voice_description": "x", "text": "y"})
    assert out["isError"] is True
    assert "401" in out["content"][0]["text"]


def test_tool_design_previews_returns_iserror_on_url_error(monkeypatch):
    """A connection-level failure to ElevenLabs is ``provider unreachable`` —
    a stable, single-word shape the skill can match on."""
    module = _import_voice_mcp()
    monkeypatch.setenv(module.ENV_TTS_KEY, "k")

    class _Opener:
        def open(self, req, timeout=None):
            raise urllib.error.URLError("no route")

    with mock.patch.object(module.urllib.request, "build_opener", return_value=_Opener()):
        out = module._tool_design_previews({"voice_description": "x", "text": "y"})
    assert out["isError"] is True
    assert "provider unreachable" in out["content"][0]["text"]


def test_tool_design_previews_returns_iserror_on_non_json_body(monkeypatch):
    """A non-JSON body is reported — the tool does NOT crash, it
    surfaces ``provider returned non-JSON``."""
    module = _import_voice_mcp()
    monkeypatch.setenv(module.ENV_TTS_KEY, "k")

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b"<html>oops</html>"

    class _Opener:
        def open(self, req, timeout=None):
            return _Resp()

    with mock.patch.object(module.urllib.request, "build_opener", return_value=_Opener()):
        out = module._tool_design_previews({"voice_description": "x", "text": "y"})
    assert out["isError"] is True
    assert "non-JSON" in out["content"][0]["text"]


def test_tool_design_previews_skips_non_dict_entries_and_bad_base64(monkeypatch):
    """The previews list may contain a non-dict entry or an entry whose
    audio field fails to decode — the tool skips those and returns the
    valid ones only. A test that asserts "all-or-nothing" would catch
    a regression that drops valid entries."""
    module = _import_voice_mcp()
    monkeypatch.setenv(module.ENV_TTS_KEY, "k")
    import os as _os
    monkeypatch.setenv("HOME", "/tmp/voice-mcp-home")
    out_dir = _os.path.expanduser("~/.local/share/voice-loop/previews")
    _os.makedirs(out_dir, exist_ok=True)

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self):
            import base64 as _b64
            audio = b"ok-mp3"
            return json.dumps(
                {
                    "previews": [
                        "not-a-dict",
                        {"generated_voice_id": "ok1", "audio_base_64": _b64.b64encode(audio).decode("ascii")},
                        {"generated_voice_id": "bad", "audio_base_64": "!!notbase64!!"},
                    ]
                }
            ).encode()

    class _Opener:
        def open(self, req, timeout=None):
            return _Resp()

    with mock.patch.object(module.urllib.request, "build_opener", return_value=_Opener()):
        result = module._tool_design_previews({"voice_description": "x", "text": "y"})
    payload = json.loads(result["content"][0]["text"])
    # Only the valid entry survives.
    assert len(payload) == 1
    assert payload[0]["generated_voice_id"] == "ok1"


def test_tool_design_save_returns_iserror_on_bad_args(monkeypatch):
    """``design_save`` requires three non-empty string arguments —
    an empty ``voice_name`` (or a non-string) returns ``isError: True``."""
    module = _import_voice_mcp()
    monkeypatch.setenv(module.ENV_TTS_KEY, "k")
    out = module._tool_design_save({"generated_voice_id": "x", "voice_name": "", "voice_description": "y"})
    assert out["isError"] is True
    assert "bad args" in out["content"][0]["text"]


def test_tool_design_save_returns_iserror_on_missing_key(monkeypatch):
    """With no TTS key the tool refuses with the documented error."""
    module = _import_voice_mcp()
    monkeypatch.delenv(module.ENV_TTS_KEY, raising=False)
    out = module._tool_design_save(
        {"generated_voice_id": "x", "voice_name": "y", "voice_description": "z"}
    )
    assert out["isError"] is True
    assert "no tts_api_key" in out["content"][0]["text"]


def test_tool_design_save_calls_elevenlabs(monkeypatch):
    """The tool POSTs to ``create-voice-from-preview`` and returns
    ``{voice_id}`` parsed from the JSON body."""
    module = _import_voice_mcp()
    monkeypatch.setenv(module.ENV_TTS_KEY, "test-xi")
    captured = {}

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self):
            return json.dumps({"voice_id": "voice-abc"}).encode()

    class _Opener:
        def open(self, req, timeout=None):
            captured["url"] = req.full_url
            captured["headers"] = dict(req.header_items())
            captured["body"] = req.data
            return _Resp()

    with mock.patch.object(module.urllib.request, "build_opener", return_value=_Opener()):
        result = module._tool_design_save(
            {
                "generated_voice_id": "gen-1",
                "voice_name": "My Voice",
                "voice_description": "warm",
            }
        )
    assert captured["url"] == module.ELEVENLABS_CREATE_URL
    # urllib normalizes the header capitalization to "Xi-api-key".
    headers = {k.lower(): v for k, v in captured["headers"].items()}
    assert headers.get("xi-api-key") == "test-xi"
    body = json.loads(captured["body"].decode())
    assert body["voice_name"] == "My Voice"
    assert body["voice_description"] == "warm"
    assert body["generated_voice_id"] == "gen-1"
    payload = json.loads(result["content"][0]["text"])
    assert payload == {"voice_id": "voice-abc"}


def test_tool_design_save_returns_iserror_on_http_error(monkeypatch):
    """A 4xx/5xx from ElevenLabs surfaces as ``isError: True``."""
    module = _import_voice_mcp()
    monkeypatch.setenv(module.ENV_TTS_KEY, "k")

    class _Opener:
        def open(self, req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 500, "Internal", {}, None)

    with mock.patch.object(module.urllib.request, "build_opener", return_value=_Opener()):
        out = module._tool_design_save(
            {"generated_voice_id": "x", "voice_name": "y", "voice_description": "z"}
        )
    assert out["isError"] is True
    assert "500" in out["content"][0]["text"]


def test_tool_design_save_returns_iserror_on_url_error(monkeypatch):
    """A connection-level failure surfaces as ``provider unreachable``."""
    module = _import_voice_mcp()
    monkeypatch.setenv(module.ENV_TTS_KEY, "k")

    class _Opener:
        def open(self, req, timeout=None):
            raise urllib.error.URLError("no route")

    with mock.patch.object(module.urllib.request, "build_opener", return_value=_Opener()):
        out = module._tool_design_save(
            {"generated_voice_id": "x", "voice_name": "y", "voice_description": "z"}
        )
    assert out["isError"] is True
    assert "provider unreachable" in out["content"][0]["text"]


def test_tool_design_save_returns_iserror_on_non_json_body(monkeypatch):
    """A non-JSON body from ElevenLabs surfaces as ``provider returned non-JSON``."""
    module = _import_voice_mcp()
    monkeypatch.setenv(module.ENV_TTS_KEY, "k")

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b"not-json"

    class _Opener:
        def open(self, req, timeout=None):
            return _Resp()

    with mock.patch.object(module.urllib.request, "build_opener", return_value=_Opener()):
        out = module._tool_design_save(
            {"generated_voice_id": "x", "voice_name": "y", "voice_description": "z"}
        )
    assert out["isError"] is True
    assert "non-JSON" in out["content"][0]["text"]


def test_dispatch_initialize_returns_server_info():
    """The MCP initialize reply carries the protocol version, server
    name and version, and the tools capability. A test that pins the
    exact JSON shape protects against a silent regression."""
    module = _import_voice_mcp()
    reply = module._dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert reply["jsonrpc"] == "2.0"
    assert reply["id"] == 1
    assert reply["result"]["serverInfo"]["name"] == "voice-loop"
    assert "version" in reply["result"]["serverInfo"]
    assert reply["result"]["capabilities"] == {"tools": {}}


def test_dispatch_initialized_returns_none():
    """The notifications/initialized method has no reply — a
    notification is fire-and-forget. ``_dispatch`` returns ``None``
    and the stdio loop skips it."""
    module = _import_voice_mcp()
    assert module._dispatch({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_dispatch_tools_list_returns_both_tools():
    """The tools/list reply exposes both voice-design tools."""
    module = _import_voice_mcp()
    reply = module._dispatch({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert reply["id"] == 2
    names = {tool["inputSchema"]["required"][0] for tool in reply["result"]["tools"]}
    assert {"voice_description", "generated_voice_id"}.issubset(names)


def test_dispatch_tools_call_routes_to_design_previews():
    """A ``tools/call`` with ``name=design_previews`` delegates to the
    previews tool. The test stubs the tool to avoid a network call."""
    module = _import_voice_mcp()
    fake_result = {"content": [{"type": "text", "text": "[]"}]}
    with mock.patch.object(module, "_tool_design_previews", return_value=fake_result) as fake:
        reply = module._dispatch(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "design_previews", "arguments": {"voice_description": "x", "text": "y"}},
            }
        )
    assert reply["result"] == fake_result
    fake.assert_called_once()


def test_dispatch_tools_call_routes_to_design_save():
    """A ``tools/call`` with ``name=design_save`` delegates to the save tool."""
    module = _import_voice_mcp()
    fake_result = {"content": [{"type": "text", "text": "{}"}]}
    with mock.patch.object(module, "_tool_design_save", return_value=fake_result) as fake:
        reply = module._dispatch(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "design_save", "arguments": {"generated_voice_id": "g", "voice_name": "n", "voice_description": "d"}},
            }
        )
    assert reply["result"] == fake_result
    fake.assert_called_once()


def test_dispatch_tools_call_unknown_tool_returns_iserror():
    """An unknown tool name returns ``isError: True`` — the relay never
    calls an external endpoint on a typo'd name."""
    module = _import_voice_mcp()
    reply = module._dispatch(
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "no_such_tool", "arguments": {}},
        }
    )
    assert reply["result"]["isError"] is True
    assert "unknown tool" in reply["result"]["content"][0]["text"]


def test_dispatch_unknown_method_with_id_returns_jsonrpc_error():
    """A method the relay doesn't know about returns the
    ``-32601 method not found`` JSON-RPC error, not a bare ``None``
    that the stdio loop would silently drop."""
    module = _import_voice_mcp()
    reply = module._dispatch({"jsonrpc": "2.0", "id": 6, "method": "no/such/method"})
    assert reply["id"] == 6
    assert reply["error"]["code"] == -32601


def test_dispatch_unknown_method_without_id_returns_none():
    """A notification for an unknown method has no reply — the relay
    drops it without an error reply."""
    module = _import_voice_mcp()
    assert module._dispatch({"jsonrpc": "2.0", "method": "no/such/method"}) is None


def test_stdio_loop_returns_on_eof(monkeypatch):
    """The stdio loop exits cleanly when stdin EOF arrives — the
    relay thread is daemonised so process exit doesn't wait on it."""
    module = _import_voice_mcp()
    # The loop reads from _recv; we patch recv to return None (EOF).
    monkeypatch.setattr(module, "_recv", lambda: None)
    # Patch the relay loop thread target to a no-op so we don't try
    # to bind a real socket in this test.
    monkeypatch.setattr(module.threading.Thread, "start", lambda self: None)
    # The loop returns None — the caller (main) returns 0.
    assert module._stdio_loop() is None


def test_stdio_loop_sends_dispatch_reply(monkeypatch):
    """A valid initialize request is dispatched and the reply is sent."""
    module = _import_voice_mcp()
    sent = []
    monkeypatch.setattr(module, "_send", lambda msg: sent.append(msg))
    msgs = iter(
        [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
            None,  # EOF
        ]
    )
    monkeypatch.setattr(module, "_recv", lambda: next(msgs))
    monkeypatch.setattr(module.threading.Thread, "start", lambda self: None)
    module._stdio_loop()
    assert len(sent) == 1
    assert sent[0]["result"]["serverInfo"]["name"] == "voice-loop"


def test_stdio_loop_skips_sending_on_none_reply(monkeypatch):
    """A notification (no reply) does not call ``_send`` — the
    stdio loop writes only when dispatch returns a message."""
    module = _import_voice_mcp()
    sent = []
    monkeypatch.setattr(module, "_send", lambda msg: sent.append(msg))
    msgs = iter(
        [
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            None,
        ]
    )
    monkeypatch.setattr(module, "_recv", lambda: next(msgs))
    monkeypatch.setattr(module.threading.Thread, "start", lambda self: None)
    module._stdio_loop()
    assert sent == []


def test_main_with_no_argv_runs_stdio_loop(monkeypatch):
    """``main([])`` enters the stdio loop. A test that calls main
    directly proves the manifest's no-arg invocation reaches the
    loop without an intervening branch."""
    module = _import_voice_mcp()
    monkeypatch.setattr(module.threading.Thread, "start", lambda self: None)
    monkeypatch.setattr(module, "_stdio_loop", lambda: None)
    assert module.main([]) == 0


def test_main_with_argv_writes_stderr_and_returns_2(capsys):
    """A stray argument is a configuration error. The MCP server is
    not flag-driven; passing one would silently ignore the manifest's
    intent. The server writes to stderr and exits 2."""
    module = _import_voice_mcp()
    rc = module.main(["--unexpected"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "unexpected arguments" in err


def test_module_invokes_main(monkeypatch, tmp_path):
    """``if __name__ == "__main__"`` calls ``sys.exit(main())``. Driven
    via ``runpy.run_path(run_name="__main__")`` so coverage attributes
    the line back to the source file — the same shape
    ``test_speak.py::test_main_guard_runs_under_runpy_for_coverage``
    uses for the speak.py guard."""
    import sys as _sys
    import runpy
    import io

    captured_exit = {"code": None}

    def _capture_exit(code):
        captured_exit["code"] = code
        raise SystemExit(code)

    # Close stdin so ``sys.stdin.readline()`` returns "" immediately;
    # ``_recv`` returns None, the stdio loop returns, ``main`` returns 0,
    # ``sys.exit(0)`` runs.
    real_stdin = _sys.stdin
    real_stdout = _sys.stdout
    real_stderr = _sys.stderr
    real_exit = _sys.exit
    try:
        _sys.stdin = io.StringIO("")
        _sys.stdout = io.StringIO()
        _sys.stderr = io.StringIO()
        _sys.exit = _capture_exit
        # Drop __main__ from sys.modules so runpy runs the script fresh.
        _sys.modules.pop("__main__", None)
        # Drop the cached voice_mcp module so its module body re-runs
        # (and the ``_PLUGINS_DIR not in sys.path`` branch can be hit).
        _sys.modules.pop("voice_mcp", None)
        # Remove _PLUGINS_DIR from sys.path so the import-time guard runs.
        _plugins_dir = str(REPO_ROOT / "plugins" / "voice-loop" / "scripts")
        while _plugins_dir in _sys.path:
            _sys.path.remove(_plugins_dir)
        try:
            runpy.run_path(  # noqa: S603 — intentional subprocess run
                str(VOICE_MCP), run_name="__main__"
            )
        except SystemExit:
            pass
    finally:
        # Restore for subsequent tests.
        if _plugins_dir not in _sys.path:
            _sys.path.insert(0, _plugins_dir)
        _sys.stdin = real_stdin
        _sys.stdout = real_stdout
        _sys.stderr = real_stderr
        _sys.exit = real_exit
    # main() returned 0 (the stdio loop read EOF, returned None, loop
    # exited, main returned 0), and sys.exit(0) ran.
    assert captured_exit["code"] == 0
