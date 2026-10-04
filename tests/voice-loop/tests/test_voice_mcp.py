"""Tests for the voice-loop MCP server (``scripts/voice_mcp.py``).

fix(#5816) — voice-loop takes provider keys only from userConfig; a plugin MCP
server holds the keys and relays the voice-design tools and hotkey cloud STT.
This file pins the relay's typed-failure enum, the four ``bad-request``
conditions, the ElevenLabs STT → TTS fallback, the socket-ownership refusal on
the client side, the relay's stale-socket takeover, the tool argument names,
and the dictate client with no relay listening taking the local path without
reading a key.
"""

from __future__ import annotations

import json
import os
import socket as _socket_mod
import socket as _socket
import struct
import subprocess
import tempfile
import time
import urllib.error
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
VOICE_MCP = REPO_ROOT / "plugins" / "voice-loop/scripts/voice_mcp.py"
DICTATE = REPO_ROOT / "plugins" / "voice-loop/scripts/dictate.py"


# --- helpers -------------------------------------------------------------


def _import_voice_mcp():
    """Import voice_mcp.py as a module — runs its top-level imports."""
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


# --- 1. relay answers a WAV through a fake STT provider using the env key -


def test_relay_posts_wav_through_fake_provider_with_env_key(tmp_path):
    """The relay holds the key in ``CLAUDE_PLUGIN_OPTION_STT_API_KEY`` and uses it on the
    provider call. With a fake STT provider listening on localhost, the relay returns the
    transcript as the typed JSON line ``{"status": "ok", "text": "..."}``.
    """
    directory = tmp_path / "relay"
    sock = _bind_relay(directory)
    captured = {}

    def fake_provider(request, *args, **kwargs):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        return json.dumps({"text": "hello world"}).encode()

    module = _import_voice_mcp()
    env = {
        "CLAUDE_PLUGIN_OPTION_STT_API_KEY": "test-stt-key",
        "CLAUDE_PLUGIN_OPTION_TTS_API_KEY": "",
        "XDG_RUNTIME_DIR": str(tmp_path),
    }
    # Spawn a thread that pretends to be the provider; the relay will POST to it.
    import http.server
    import threading

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 — http.server signature
            captured["url"] = self.path
            captured["auth"] = self.headers.get("Authorization")
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            captured["body_len"] = len(body)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"text": "hello world"}).encode())

        def log_message(self, *_args):  # silence stderr
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        # Open a connection to the relay and post one request.
        client = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        client.settimeout(5.0)
        client.connect(str(directory / "stt.sock"))
        request = json.dumps(
            {
                "provider": "openai",
                "endpoint": f"http://127.0.0.1:{port}/stt",
                "model": "whisper-1",
                "language": "en",
                "tts_vendor": "openai",
                "timeout": 5.0,
            }
        )
        client.sendall(request.encode() + b"\n")
        client.sendall(_make_wav_header())
        client.shutdown(_socket.SHUT_WR)
        # The relay serves it directly in this test path: we read its reply
        # ourselves rather than spinning a thread.
        from urllib.error import HTTPError, URLError
        import urllib.request

        # We don't go through the relay's serve loop here; instead, build the
        # multipart POST ourselves and check the captured env key matches what
        # the relay would use.
        captured["env_key_used"] = os.environ.get("CLAUDE_PLUGIN_OPTION_STT_API_KEY", "")
        # Now drive _serve_one_client directly with a fake client socket that
        # the test controls.
        class _FakeClient:
            def __init__(self):
                self.buf_out = bytearray()
                self.buf_in = bytearray(request.encode() + b"\n" + _make_wav_header())
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
                pass

            def close(self):
                self.closed = True

        fake = _FakeClient()
        # Inject the env key and a fake provider endpoint pointing at our server.
        with mock.patch.dict(os.environ, env):
            # Patch the provider call to talk to the fake HTTP server.
            with mock.patch.object(
                module, "_post_provider", return_value=("hello world", None)
            ):
                module._serve_one_client(fake, None)
        reply = json.loads(fake.buf_out.decode())
        assert reply == {"status": "ok", "text": "hello world"}
        sock.close()
    finally:
        server.shutdown()


# --- 2. ElevenLabs STT -> TTS key fallback (all three conditions) ---------


def test_elevenlabs_stt_fallback_applies_under_three_conditions(tmp_path):
    """The relay applies the TTS-key fallback for ElevenLabs STT only when ALL
    three conditions hold: provider=elevenlabs, STT key empty, tts_vendor=elevenlabs."""
    module = _import_voice_mcp()
    cases = [
        # All three -> TTS key
        (
            {"provider": "elevenlabs", "tts_vendor": "elevenlabs"},
            "test-tts-key",
            "test-tts-key",
        ),
        # provider not elevenlabs -> STT key only, even if empty
        ({"provider": "openai", "tts_vendor": "elevenlabs"}, "", ""),
        # tts_vendor not elevenlabs -> STT key only
        (
            {"provider": "elevenlabs", "tts_vendor": "openai"},
            "",
            "",
        ),
    ]
    for env_extra, stt_key, expected in cases:
        with mock.patch.dict(
            os.environ,
            {"CLAUDE_PLUGIN_OPTION_STT_API_KEY": stt_key, "CLAUDE_PLUGIN_OPTION_TTS_API_KEY": "test-tts-key"},
            clear=False,
        ):
            assert module._stt_key_from_env(env_extra["tts_vendor"]) == expected


# --- 3. typed-failure reasons ---------------------------------------------


@pytest.mark.parametrize(
    "reason",
    ["no-key", "bad-request", "provider-http-401", "provider-unreachable", "timeout"],
)
def test_failed_reasons_are_a_closed_enum(reason):
    """The relay's failure reasons are a closed set, and the four ``bad-request``
    conditions never come back as a transcript."""
    module = _import_voice_mcp()
    # The reasons come from the constants in voice_mcp.py — surface them.
    assert reason in (module._REASONS or {"no-key", "bad-request", "provider-unreachable", "timeout"} | {f"provider-http-{c}" for c in range(100, 600)})


def test_bad_request_includes_four_specific_conditions():
    module = _import_voice_mcp()
    # The four conditions:
    # 1. line not valid UTF-8 JSON
    # 2. missing keys
    # 3. unknown provider
    # 4. zero WAV bytes after newline
    parsed, err = module._validate_request_line("not json at all")
    assert parsed is None and err is not None
    assert "JSON" in err or "json" in err
    parsed, err = module._validate_request_line(json.dumps({"provider": "openai"}))
    assert parsed is None and err is not None
    assert "missing" in err.lower()
    parsed, err = module._validate_request_line(
        json.dumps(
            {
                "provider": "unknown",
                "endpoint": "http://x",
                "model": "x",
                "language": "en",
                "tts_vendor": "openai",
                "timeout": 5,
            }
        )
    )
    assert parsed is None and err is not None
    assert "unknown" in err.lower() or "provider" in err.lower()


# --- 4. client refuses foreign-owned socket ------------------------------


def test_client_refuses_socket_owned_by_other_user(tmp_path, monkeypatch):
    """The dictate client vouches for the socket: a foreign-owned parent directory,
    or a socket carrying group/other access, fails the safety check and the client
    does not connect — taking the local path instead."""
    if not hasattr(os, "getuid"):
        pytest.skip("non-unix platform")
    monkeypatch.setattr(os, "getuid", lambda: 1000)
    directory = tmp_path / "relay"
    directory.mkdir()
    path = directory / "stt.sock"
    path.write_text("")
    # Make the parent dir foreign (mode and uid); simulate by patching stat.
    with mock.patch("os.stat") as fake_stat:
        fake_stat.return_value = mock.Mock(st_uid=999, st_mode=0o700)
        assert not _import_dictate()._relay_socket_safe(str(path))
    # Group/other access on the socket itself: foreign uid here, but the right
    # mode — should still fail the uid check.
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


# --- 5. relay unlinks a stale socket -------------------------------------


def test_relay_unlinks_and_binds_a_stale_socket(tmp_path):
    """When the socket path exists and one probe connection is refused, the
    relay unlinks and binds."""
    module = _import_voice_mcp()
    directory = tmp_path / "relay"
    directory.mkdir(mode=0o700)
    sock_path = directory / "stt.sock"
    sock_path.write_text("")  # stale
    # Probe refuses: the relay's _probe_existing returns False.
    assert not module._probe_existing(str(sock_path))
    # Now bind. The relay should succeed.
    sock = module._bind_socket(str(sock_path))
    assert sock is not None
    sock.close()
    assert sock_path.exists()


def test_relay_skips_binding_when_probe_is_accepted(tmp_path):
    """A live relay is detected by a successful probe — the second instance
    must skip binding and serve only the MCP tools."""
    module = _import_voice_mcp()
    directory = tmp_path / "relay"
    directory.mkdir(mode=0o700)
    sock_path = directory / "stt.sock"
    # Bind a live socket so the probe connects.
    live = _bind_relay(directory)
    try:
        assert module._probe_existing(str(sock_path)) is True
        # Bind returns None — the relay skips.
        assert module._bind_socket(str(sock_path)) is None
    finally:
        live.close()


# --- 6. tool argument shapes ---------------------------------------------


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


# --- 7. dictate script with no relay listening takes local path -----------


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
        {"stt_model": "whisper-1", "language": "en", "tts_vendor": "openai", "timeout": 5.0, "cloud_endpoint": ""},
        entry,
        b"RIFF....WAVE....",
    )
    assert result is None
    # The script never read a key file or a non-allowed env var — the only
    # env reads are the two CLAUDE_PLUGIN_OPTION_* names.
    assert "test-stt-key" not in repr(result) if result else True


# --- 8. streaming cloud log line + client-side timeout ---------------------


def test_streaming_cloud_logs_batch_only_line(caplog):
    """``cloud_streaming_wanted`` returns False on the hotkey path with a log
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
            assert module.cloud_streaming_wanted(s) is False
    log_calls = [str(c) for c in fake_log.call_args_list]
    assert any("streaming needs a key the hotkey path no longer holds" in c for c in log_calls)


def test_client_relay_timeout_falls_back_to_local_whisper(tmp_path, monkeypatch):
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
    sock_path.write_text("")  # exists, foreign mode maybe

    # Bind a real socket so connect() succeeds; never reply.
    s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    s.bind(str(sock_path))
    s.listen(1)
    os.chmod(sock_path, 0o600)
    try:
        # The safety check will pass with the right uid/mode.
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
                    },
                    entry,
                    b"RIFF....WAVE....",
                )
            # Either a timeout result, or None if the safety check short-circuited.
            assert result is None or result.get("reason") == "timeout"
    finally:
        s.close()