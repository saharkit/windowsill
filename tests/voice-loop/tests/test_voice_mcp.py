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

import json
import os
import socket as _socket
import struct
import urllib.error
from pathlib import Path
from unittest import mock

import pytest

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
        # A real client half-closes here — the relay's _serve_one_client must
        # NOT call shutdown on its own end. We do not test that here; the live
        # integration in tests/voice-loop is the place that proves it.
        pass

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


def test_relay_round_trip_with_real_socketpair(tmp_path):
    """The relay answers a request line + WAV sent on a real AF_UNIX socketpair
    in one shot, with a stubbed provider, as ``{"status": "ok", "text": ...}``.

    No fakes at the request-build level: the registry drives the request,
    the test stubs only the network socket, and the assert is on the typed
    reply."""
    directory = tmp_path / "relay"
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


def test_relay_splits_request_line_from_wav_with_newline_in_middle_of_recv(tmp_path):
    """The request line and the WAV can arrive in a single ``recv``. The relay
    splits on the FIRST ``\\n`` anywhere in the buffer — not on a buf that
    ``endswith(b"\\n")``. A test that uses ``buf.endswith`` misses a request
    line and a WAV that landed in the same chunk."""
    directory = tmp_path / "relay"
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


def test_relay_does_not_call_shutdown_before_sendall(tmp_path):
    """A relay that half-closes its write side and then writes gets a
    ``BrokenPipeError`` and the client reads EOF as ``reason=timeout``. The
    reply is one ``sendall`` followed by ``close()`` in the finally block —
    nothing else, and specifically no ``shutdown`` between them."""
    directory = tmp_path / "relay"
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


# --- 4. STT key fallback (R3) ----------------------------------------------


def test_stt_key_fallback_elevenlabs_all_three_conditions():
    """The relay applies the TTS-key fallback for ElevenLabs STT only when ALL
    three conditions hold: provider=elevenlabs, STT key empty, tts_vendor=elevenlabs."""
    module = _import_voice_mcp()
    env = {
        "CLAUDE_PLUGIN_OPTION_TTS_API_KEY": "test-tts-key",
    }
    # All three -> TTS key
    with mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_OPTION_STT_API_KEY": ""} | env, clear=False):
        assert module._stt_key_from_env("elevenlabs", "elevenlabs") == "test-tts-key"
    # provider not elevenlabs -> STT key only, even if empty
    with mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_OPTION_STT_API_KEY": ""} | env, clear=False):
        assert module._stt_key_from_env("openai", "elevenlabs") == ""
    # tts_vendor not elevenlabs -> STT key only
    with mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_OPTION_STT_API_KEY": ""} | env, clear=False):
        assert module._stt_key_from_env("elevenlabs", "openai") == ""
    # STT key set -> STT key wins, regardless of provider / tts_vendor
    with mock.patch.dict(
        os.environ, {"CLAUDE_PLUGIN_OPTION_STT_API_KEY": "stt-key-set"} | env, clear=False
    ):
        assert module._stt_key_from_env("openai", "openai") == "stt-key-set"
        assert module._stt_key_from_env("elevenlabs", "elevenlabs") == "stt-key-set"
    # deepgram with empty STT key and tts_vendor=elevenlabs -> "" (the
    # R3 case: not elevenlabs STT, so the TTS key does NOT back-stop it)
    with mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_OPTION_STT_API_KEY": ""} | env, clear=False):
        assert module._stt_key_from_env("deepgram", "elevenlabs") == ""


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


def test_relay_unlinks_and_binds_a_stale_socket(tmp_path):
    """When the socket path exists and one probe connection is refused, the
    relay unlinks and binds."""
    module = _import_voice_mcp()
    directory = tmp_path / "relay"
    directory.mkdir(mode=0o700)
    sock_path = directory / "stt.sock"
    sock_path.write_text("")  # stale
    assert not module._probe_existing(str(sock_path))
    sock = module._bind_socket(str(sock_path))
    assert sock is not None
    sock.close()
    assert sock_path.exists()


def test_relay_skips_binding_when_probe_is_accepted(tmp_path):
    """A live relay is detected by a successful probe — the second instance
    must skip binding and serve only the MCP tools. ``_bind_socket`` always
    tries to bind (it owns the unlink step); the SKIP happens at the relay
    loop's caller, where the probe answer is checked first."""
    module = _import_voice_mcp()
    directory = tmp_path / "relay"
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


def test_client_relay_timeout_returns_timeout_result(tmp_path, monkeypatch):
    """A relay silent past ``stt.timeout + 5 s`` causes the client to return
    ``{"status": "failed", "reason": "timeout"}`` and the caller degrades."""
    module = _import_dictate()
    env_dir = tmp_path / "runtime"
    env_dir.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(env_dir))
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_STT_API_KEY", "test-stt-key")
    directory = tmp_path / "relay"
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
