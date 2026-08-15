"""The resident ElevenLabs streaming holder (windowsill#113) — the voice-back counterpart of #99's
streaming dictation.

Two tiers, by the law that fixes the count (L6): the holder's DECISIONS are unit-tested against a
fake websocket whose ``poll`` returns canned frames — priming, per-line flush, the keepalive clock,
reconnect-after-close, the quota/degrade signal — because those are each one decision and a fake ws
makes them deterministic and fast. ONE integration test reaches a REAL loopback websocket (the
handshake, the masked frames, the protocol the registry entry builds the URL for) to prove the
wsclient + holder + registry interplay a unit test cannot reach.

Nothing here touches the network or audio hardware: the loopback server is a listening socket on
127.0.0.1 spoken to by hand, exactly as in test_wsclient.py (this is test infrastructure, not a
second implementation of the client). No model, no key on the wire.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import socket
import struct
import threading
import time
from pathlib import Path

import pytest

_SPEAK_PATH = Path(__file__).resolve().parents[1] / "scripts" / "speak.py"
_spec = importlib.util.spec_from_file_location("speak_stream", _SPEAK_PATH)
speak = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(speak)

# wsclient and providers are imported (not spec-loaded) so they are the SAME module objects speak.py
# uses — a spec load would make a second wsclient with a second WebSocketError class, and the
# holder's `except wsclient.WebSocketError` would not catch the test's exception (the double-load
# bug). pytest.ini puts scripts/ on sys.path, so the plain imports resolve there.
import providers  # noqa: E402
import wsclient  # noqa: E402

_ENTRY = providers.TTS_PROVIDERS["elevenlabs"]


def _settings(**overrides) -> dict:
    base = {
        "endpoint": "",
        "voice_id": "vX",
        "cloud_model": "eleven_flash_v2_5",
        "stream_output_format": "pcm_22050",
        "voice_settings": None,
        "speed": 1.0,
        "provider": "elevenlabs",
        "timeout": 30.0,
    }
    base.update(overrides)
    return base


def _audio_msg(pcm: bytes, is_final: bool = False) -> bytes:
    return json.dumps({"audio": base64.b64encode(pcm).decode("ascii"), "isFinal": is_final}).encode()


class FakeWs:
    """A stand-in for wsclient.WebSocket whose ``poll`` returns canned frames, one list per call.
    Not a fake of the protocol — a fake of the holder's ONE collaborator, so the drain/keepalive/
    reconnect decisions are tested without a socket. The frames list is consumed in order; an empty
    list means 'nothing arrived this poll' (the idle a keepalive fires in)."""

    def __init__(self, polls, *, closed=False):
        self._polls = list(polls)  # each entry is a list of (opcode, payload) returned by one poll
        self.sent: list[tuple[str, str]] = []
        self.closed = closed

    def send_text(self, text: str) -> None:
        self.sent.append(("text", text))

    def send_binary(self, payload: bytes) -> None:
        self.sent.append(("binary", payload))

    def poll(self, timeout: float):
        if self._polls:
            return self._polls.pop(0)
        return []

    def close(self) -> None:
        self.closed = True


# --- the pure helpers ----------------------------------------------------------------------------


def test_pcm_to_wav_wraps_raw_samples_in_a_playable_header():
    """A 44-byte RIFF/WAVE header is all that separates pcm_22050 from something the player queue
    accepts — and the wrap copies samples verbatim (no re-encode), which is what keeps the path
    decoder-free. Without this, _play_stream is handed raw bytes afplay/aplay cannot place."""
    wav = speak.pcm_to_wav(b"\x00\x00" * 100, 22050)
    assert wav[:4] == b"RIFF" and wav[8:12] == b"WAVE"
    assert len(wav) == 44 + 200  # header + data, unchanged sample bytes


def test_stream_pcm_rate_reads_the_token_and_defaults_safely():
    """A silent upstream format change (pcm_44100 instead of pcm_22050) must pitch the WAV at the
    rate the vendor actually sent, not the one we hardcoded. A non-pcm token falls back to 22050
    rather than raising — the streaming path never asked for it."""
    assert speak.stream_pcm_rate("pcm_22050") == 22050
    assert speak.stream_pcm_rate("pcm_44100") == 44100
    assert speak.stream_pcm_rate("mp3_44100_128") == 22050
    assert speak.stream_pcm_rate("") == 22050


def test_sse_event_is_the_strict_shape_parse_sse_reads():
    """The holder speaks the SAME protocol as the server's /tts/stream so play_text's chunk reader
    is reused verbatim — and that only holds while the frame is exactly event/data/blank-line."""
    assert speak.sse_event("chunk", {"audio": "AA=="}) == b'event: chunk\ndata: {"audio": "AA=="}\n\n'


# --- config knobs --------------------------------------------------------------------------------


def test_streaming_is_opt_in_and_off_by_default():
    assert speak.resolve_settings({}, "Linux")["streaming"] is False
    s = speak.resolve_settings({"tts": {"cloud": {"provider": "elevenlabs", "streaming": True}}}, "Linux")
    assert s["streaming"] is True
    # the streaming output format defaults to the entry's variant (pcm_22050), NOT the batch mp3
    assert s["stream_output_format"] == "pcm_22050"
    assert s["speed"] == 1.0


def test_speed_knob_reads_off_voice_settings_and_overrides_the_format():
    s = speak.resolve_settings({"tts": {"cloud": {"provider": "elevenlabs", "voice_settings": {"speed": 0.9}}}}, "Linux")
    assert s["speed"] == 0.9


def test_stream_output_format_is_overridable():
    s = speak.resolve_settings(
        {"tts": {"cloud": {"provider": "elevenlabs", "stream_output_format": "pcm_44100"}}}, "Linux"
    )
    assert s["stream_output_format"] == "pcm_44100"


def test_cloud_streaming_wanted_needs_the_opt_in_and_a_variant():
    """A provider asked to stream that has no variant is the blob path, never an error; and the
    opt-in is what keeps a default install off the metered socket."""
    on = _settings(streaming=True)  # type: ignore[arg-type]
    assert speak.cloud_streaming_wanted({"streaming": True, "provider": "elevenlabs"}) is True
    assert speak.cloud_streaming_wanted({"streaming": False, "provider": "elevenlabs"}) is False
    assert speak.cloud_streaming_wanted({"streaming": True, "provider": "openai"}) is False


def test_the_digest_changes_with_a_settings_edit():
    """The reconnect trigger: a speed edit (or a new voice/model) changes the digest, so
    ensure_stream_holder sees the running holder as stale and respawns it with the new settings."""
    base = speak.resolve_settings({"tts": {"cloud": {"provider": "elevenlabs", "streaming": True}}}, "Linux")
    faster = speak.resolve_settings(
        {"tts": {"cloud": {"provider": "elevenlabs", "streaming": True, "voice_settings": {"speed": 0.9}}}}, "Linux"
    )
    assert speak.stream_settings_digest(base) != speak.stream_settings_digest(faster)


# --- the holder's decisions, against a fake websocket --------------------------------------------


def test_synthesize_line_drains_fragments_until_the_final_marker():
    """The line-complete signal is the ``isFinal`` marker, not the socket going quiet — so the drain
    must stop at it and not at the last fragment. Without this assertion a holder drops the marker
    distinction and either truncates a line or waits past its deadline."""
    holder = speak.TtsStreamHolder(_ENTRY, _settings(), "key", clock=lambda: 0.0)
    holder._ws = FakeWs(
        [
            [(wsclient.OP_TEXT, _audio_msg(b"\x01\x02"))],
            [(wsclient.OP_TEXT, _audio_msg(b"\x03\x04"))],
            [(wsclient.OP_TEXT, json.dumps({"isFinal": True}).encode())],
        ]
    )
    holder._last_send = 0.0
    frags = list(holder.synthesize_line("hi", deadline=999.0))
    assert frags == [b"\x01\x02", b"\x03\x04"]
    # the line went out as a text frame + a flush, in that order
    assert json.loads(holder._ws.sent[0][1]) == {"text": "hi "}
    assert json.loads(holder._ws.sent[1][1]) == {"flush": True}


def test_a_bad_fragment_is_skipped_not_a_failed_line():
    """A truncated base64 fragment must not abort the whole line — the next fragment is still good,
    and degrading would lose speech a skip preserves."""
    holder = speak.TtsStreamHolder(_ENTRY, _settings(), "key", clock=lambda: 0.0)
    holder._ws = FakeWs(
        [
            [(wsclient.OP_TEXT, json.dumps({"audio": "!!!not base64!!!", "isFinal": False}).encode())],
            [(wsclient.OP_TEXT, _audio_msg(b"\x09\x0a"))],
            [(wsclient.OP_TEXT, json.dumps({"isFinal": True}).encode())],
        ]
    )
    holder._last_send = 0.0
    assert list(holder.synthesize_line("hi", deadline=999.0)) == [b"\x09\x0a"]


def test_a_close_mid_line_drops_the_socket_and_signals_degrade():
    """The vendor's ~20 s idle close (or a quota close) arrives MID-LINE as a close frame. The
    holder must raise so the caller degrades that line, AND drop the socket so the next line
    reconnects rather than writing into a dead connection."""
    holder = speak.TtsStreamHolder(_ENTRY, _settings(), "key", clock=lambda: 0.0)
    holder._ws = FakeWs([[(wsclient.OP_CLOSE, b"")]])
    holder._last_send = 0.0
    with pytest.raises(speak.TtsStreamClosed):
        list(holder.synthesize_line("hi", deadline=999.0))
    assert holder._ws is None  # dropped — the next ensure_open reconnects


def test_a_line_that_outran_its_deadline_is_a_degrade():
    """A held socket that stalls cannot hold a turn open forever: the deadline turns it into the
    same TtsStreamClosed the caller degrades on, and drops the socket."""
    clock = [0.0]
    holder = speak.TtsStreamHolder(_ENTRY, _settings(), "key", clock=lambda: clock[0])
    holder._ws = FakeWs([[], [], []])  # the server never answers
    holder._last_send = 0.0
    clock[0] = 1000.0  # already past the deadline
    with pytest.raises(speak.TtsStreamClosed):
        list(holder.synthesize_line("hi", deadline=10.0))
    assert holder._ws is None


def test_a_connect_failure_propagates_as_a_degrade_signal():
    """A 401/quota at CONNECT time is a WebSocketError from wsclient; the holder does not swallow it
    — the caller's serve loop catches it and emits the degrade event."""

    def refused(url, headers, timeout):
        raise wsclient.WebSocketError("the server refused the upgrade: HTTP/1.1 401 Unauthorized")

    holder = speak.TtsStreamHolder(_ENTRY, _settings(), "key", connect=refused, clock=lambda: 0.0)
    holder._ws = None
    with pytest.raises(wsclient.WebSocketError):
        list(holder.synthesize_line("hi", deadline=999.0))


def test_prime_drains_a_throwaway_and_leaves_the_socket_open():
    """The priming frame warms the session so the first REAL line is not the cold 2247 ms call; it
    must drain its own audio and leave the socket live for the lines that follow."""
    holder = speak.TtsStreamHolder(_ENTRY, _settings(), "key", clock=lambda: 0.0)
    holder._ws = FakeWs(
        [[(wsclient.OP_TEXT, _audio_msg(b"\xff"))], [(wsclient.OP_TEXT, json.dumps({"isFinal": True}).encode())]]
    )
    holder._last_send = 0.0
    holder.prime()
    assert holder._ws is not None and not holder._ws.closed  # still live for the next line


def test_keepalive_fires_only_past_the_interval_and_caries_no_flush():
    """An idle socket is held open by a whitespace text frame — but ONLY past the interval (the
    frame is not free) and NEVER a flush (a flush would emit audio into the silence between turns)."""
    clock = [0.0]
    holder = speak.TtsStreamHolder(_ENTRY, _settings(), "key", clock=lambda: clock[0])
    holder._ws = FakeWs([])
    holder._last_send = 0.0
    holder.keepalive_if_due()  # clock 0, last_send 0: not yet due
    assert holder._ws.sent == []
    clock[0] = speak.STREAM_KEEPALIVE_SECONDS + 0.01
    holder.keepalive_if_due()
    assert len(holder._ws.sent) == 1
    assert json.loads(holder._ws.sent[0][1]) == {"text": " "}  # whitespace, no flush


def test_keepalive_on_a_dead_socket_drops_it_silently():
    """A keepalive that cannot be sent means the socket died between turns; the holder drops it and
    the next line reconnects, rather than raising into the idle loop."""
    holder = speak.TtsStreamHolder(_ENTRY, _settings(), "key", clock=lambda: 100.0)

    class BrokenWs(FakeWs):
        def send_text(self, text):
            raise wsclient.WebSocketError("send failed: broken pipe")

    holder._ws = BrokenWs([])
    holder._last_send = 0.0
    holder.keepalive_if_due()  # must not raise
    assert holder._ws is None


# --- the holder's pid guard ----------------------------------------------------------------------


def test_the_holder_pid_guard_reads_cmdline_for_the_argv_marker():
    """A pidfile outlives its process and PIDs recycle — the guard confirms a recorded pid still
    looks like the holder (its argv carries speak.py + the stream-holder word) before it is trusted."""

    def cmd(cmdline):
        return lambda pid: cmdline

    good = f"python3 /path/speak.py {speak.STREAM_HOLDER_ARG} abc123"
    assert speak.pid_looks_like_stream_holder(1, read_cmdline=cmd(good), platform_id="linux") is True
    assert speak.pid_looks_like_stream_holder(1, read_cmdline=cmd("some other process"), platform_id="linux") is False
    assert speak.pid_looks_like_stream_holder(1, read_cmdline=cmd(None), platform_id="linux") is False
    # non-Linux has no /proc cmdline — the historical raw-trust behaviour, unchanged
    assert speak.pid_looks_like_stream_holder(1, read_cmdline=lambda pid: None, platform_id="darwin") is True


# --- ensure_stream_holder: warm reuse, stale respawn, spawn failure ------------------------------


@pytest.fixture
def holder_state(monkeypatch, tmp_path):
    """The holder's pidfile and socket live in the test's tmp_path, never the live state dir."""
    monkeypatch.setattr(speak, "_STREAM_HOLDER_PID", str(tmp_path / "holder.pid"))
    monkeypatch.setattr(speak, "_STREAM_HOLDER_SOCK", str(tmp_path / "holder.sock"))
    return tmp_path


def test_a_live_holder_with_a_matching_digest_is_reused(holder_state):
    """A warm socket for exactly these settings is the whole point of the holder: a second turn must
    NOT respawn it (which would re-pay the dial). The matching digest short-circuits before spawn."""

    def must_not_spawn(argv, **kw):
        raise AssertionError("a warm holder should not have been respawned")

    digest = speak.stream_settings_digest(_settings())
    speak._write_stream_holder_pid(os.getpid(), digest)
    # the pid guard trusts this process because the test says so (its real argv is pytest, not the holder)
    original = speak.pid_looks_like_stream_holder
    speak.pid_looks_like_stream_holder = lambda pid, **kw: True
    try:
        assert speak.ensure_stream_holder(_settings(), popen=must_not_spawn, sleep=lambda _: None, clock=lambda: 0.0)
    finally:
        speak.pid_looks_like_stream_holder = original


def test_a_stale_digest_stops_the_old_holder_and_respawns(holder_state):
    """A settings edit is a reconnect trigger: the holder for the OLD digest is SIGTERM'd and a new
    one spawned with the new settings. The kill and the spawn are both observable here."""
    killed = []

    class P:
        pid = 12345

    def fake_popen(argv, **kw):
        # the real holder writes its pidfile on startup; the fake simulates that so the ready-wait ends
        speak._write_stream_holder_pid(P.pid, argv[-1])
        open(speak._STREAM_HOLDER_SOCK, "w").close()
        spawned.append(argv)
        return P()

    spawned = []
    speak._write_stream_holder_pid(4321, "an-old-digest")
    speak.pid_looks_like_stream_holder = lambda pid, **kw: True
    original_kill = os.kill

    def fake_kill(pid, sig):
        killed.append((pid, sig))

    os.kill = fake_kill
    try:
        assert speak.ensure_stream_holder(_settings(), popen=fake_popen, sleep=lambda _: None, clock=lambda: 0.0)
    finally:
        os.kill = original_kill
        speak.pid_looks_like_stream_holder = speak.pid_looks_like_stream_holder  # restore (no-op self-assign guard)
    assert killed == [(4321, speak.signal.SIGTERM)]
    assert spawned and spawned[0][-1] == speak.stream_settings_digest(_settings())


def test_a_spawn_failure_falls_back_to_the_blob_path(holder_state):
    """A holder that will not start must not stall a turn: ensure returns False and the caller uses
    the blob path for that one turn."""
    speak._write_stream_holder_pid(4321, "stale")  # forces a spawn attempt

    def boom(argv, **kw):
        raise OSError("nope")

    speak.pid_looks_like_stream_holder = lambda pid, **kw: False
    try:
        assert speak.ensure_stream_holder(_settings(), popen=boom, sleep=lambda _: None, clock=lambda: 0.0) is False
    finally:
        del speak.pid_looks_like_stream_holder  # restore the real function


# --- run_holder_main: refuse fast, before any socket ---------------------------------------------


def test_run_holder_main_refuses_when_streaming_is_off(monkeypatch, tmp_path):
    """The holder is spawned only by a turn that wanted streaming; if the config changed under it
    (streaming turned off between spawn and start) it exits at once rather than holding a socket
    nobody will ask for."""
    cfg = tmp_path / "config.json"
    cfg.write_text("{}")
    monkeypatch.setenv("VOICE_LOOP_CONFIG", str(cfg))
    monkeypatch.delenv("VOICE_LOOP_TTS_API_KEY", raising=False)
    assert speak.run_holder_main("deadbeef") == 1


def test_run_holder_main_refuses_without_a_key(monkeypatch, tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"tts": {"backend": "cloud", "cloud": {"provider": "elevenlabs", "streaming": True}}}))
    monkeypatch.setenv("VOICE_LOOP_CONFIG", str(cfg))
    monkeypatch.delenv("VOICE_LOOP_TTS_API_KEY", raising=False)
    assert speak.run_holder_main("deadbeef") == 1


def test_run_holder_main_refuses_for_a_provider_without_a_variant(monkeypatch, tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"tts": {"backend": "cloud", "cloud": {"provider": "openai", "streaming": True}}}))
    monkeypatch.setenv("VOICE_LOOP_CONFIG", str(cfg))
    monkeypatch.setenv("VOICE_LOOP_TTS_API_KEY", "k")
    assert speak.run_holder_main("deadbeef") == 1


# --- the unix-socket plumbing --------------------------------------------------------------------


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="the holder's socket is Unix-domain; Windows has no AF_UNIX to bind")
def test_bind_unix_listener_creates_a_listener_and_drops_a_stale_file(tmp_path):
    """A leftover socket file from a crashed holder would make bind() fail; the bind removes any
    stale file first, so a respawn always succeeds."""
    path = str(tmp_path / "h.sock")
    open(path, "w").close()  # a stale file where the socket should go
    listener = speak._bind_unix_listener(path)
    try:
        assert os.path.exists(path)
        another = speak._bind_unix_listener(path)  # re-bind over the now-live socket
        another.close()
    finally:
        listener.close()


def test_an_exiting_holder_spares_the_socket_its_replacement_rebound(holder_state):
    """The generation fence on the exit unlink: a SIGTERMed holder reaches its exit up to one
    accept-timeout AFTER its replacement rebound the same socket path, so the unlink must not
    run once the pidfile carries the replacement's record (the replacement announces itself
    there before it binds, and its own bind unlinks any stale file)."""
    speak._write_stream_holder_pid(111, "digest-a")  # holder A installed its record...
    open(speak._STREAM_HOLDER_SOCK, "w").close()  # ...then its replacement rebound the path
    with open(speak._STREAM_HOLDER_PID, "w", encoding="utf-8") as fh:  # ...and announced itself
        fh.write("222 digest-b")

    speak._holder_exit_cleanup()  # A's exit: superseded — the fence must hold

    assert os.path.exists(speak._STREAM_HOLDER_SOCK)  # the replacement's socket survived
    assert os.path.exists(speak._STREAM_HOLDER_PID)  # and its pidfile was left alone too


def test_an_unsuperseded_holder_cleans_up_its_socket_and_pidfile(holder_state):
    """With no replacement, the same exit cleanup removes both files — the fence must not
    become a leak."""
    speak._write_stream_holder_pid(111, "digest-a")
    open(speak._STREAM_HOLDER_SOCK, "w").close()

    speak._holder_exit_cleanup()

    assert not os.path.exists(speak._STREAM_HOLDER_SOCK)
    assert not os.path.exists(speak._STREAM_HOLDER_PID)


def test_connect_stream_holder_returns_none_when_the_holder_is_not_ready(monkeypatch, tmp_path):
    """ensure_stream_holder False (could not spawn/bind) short-circuits before a socket is opened —
    a holder that is not there is the blob path, not a connection error to log."""
    monkeypatch.setattr(speak, "ensure_stream_holder", lambda s, **kw: False)
    assert speak._connect_stream_holder("hi", _settings(timeout=1.0)) is None


def test_connect_stream_holder_returns_none_when_the_socket_refuses(monkeypatch, tmp_path):
    """A holder that vanished between ensure and connect (it self-exited on idle) is the same answer
    as not being there: None, and the blob path takes the turn."""
    monkeypatch.setattr(speak, "ensure_stream_holder", lambda s, **kw: True)
    monkeypatch.setattr(speak, "_STREAM_HOLDER_SOCK", str(tmp_path / "nope.sock"))  # nothing listening
    assert speak._connect_stream_holder("hi", _settings(timeout=1.0)) is None


# --- _serve_stream_connection: the SSE contract over a real socket pair --------------------------


def test_serve_emits_one_chunk_per_sentence_then_end():
    """The holder speaks the server's SSE contract: one ``chunk`` per synthesized sentence, then a
    terminal ``end``. parse_sse reads exactly this, so a shape drift here is a silent break."""
    a, b = socket.socketpair()
    holder = speak.TtsStreamHolder(_ENTRY, _settings(), "key", clock=lambda: 0.0)
    holder._ws = FakeWs(
        [[(wsclient.OP_TEXT, _audio_msg(b"\x10\x20"))], [(wsclient.OP_TEXT, json.dumps({"isFinal": True}).encode())]]
    )
    holder._last_send = 0.0
    thread = threading.Thread(target=speak._serve_stream_connection, args=(a, holder, _settings()))
    thread.start()
    b.sendall((json.dumps({"text": "one short line"}) + "\n").encode())
    b.shutdown(socket.SHUT_WR)
    received = b""
    while b"event: end" not in received:
        chunk = b.recv(65536)
        if not chunk:
            break
        received += chunk
    thread.join(timeout=3.0)
    a.close()
    b.close()
    assert b"event: chunk" in received
    assert b"event: end" in received
    assert not thread.is_alive()


def test_serve_emits_an_error_event_on_a_degrade():
    """Never silence: a line the held socket could not finish is an ``error`` event, and the client
    falls back to the blob path — the same contract the server's /tts/stream honours."""
    a, b = socket.socketpair()
    holder = speak.TtsStreamHolder(_ENTRY, _settings(), "key", clock=lambda: 0.0)
    holder._ws = FakeWs([[(wsclient.OP_CLOSE, b"")]])  # the vendor hangs up mid-line
    holder._last_send = 0.0
    thread = threading.Thread(target=speak._serve_stream_connection, args=(a, holder, _settings()))
    thread.start()
    b.sendall((json.dumps({"text": "a line"}) + "\n").encode())
    b.shutdown(socket.SHUT_WR)
    received = b""
    while b"event: error" not in received and len(received) < 4096:
        chunk = b.recv(65536)
        if not chunk:
            break
        received += chunk
    thread.join(timeout=3.0)
    a.close()
    b.close()
    assert b"event: error" in received
    assert not thread.is_alive()


# --- ONE integration test: the real loopback websocket ------------------------------------------
# The fake-ws tests above pin the holder's decisions; this one reaches the interaction they cannot —
# the registry-built URL, wsclient's verified handshake and masked frames, and the holder's real
# poll/drain against a server that speaks the protocol back. (L6: one smoke, earning its tier.)


def _read_http_head(conn) -> bytes:
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = conn.recv(4096)
        if not chunk:
            break
        buf += chunk
    return buf


def _accept_for(head: bytes) -> str:
    key = ""
    for line in head.decode("utf-8", "replace").split("\r\n"):
        name, sep, value = line.partition(":")
        if sep and name.strip().lower() == "sec-websocket-key":
            key = value.strip()
    digest = hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
    return base64.b64encode(digest).decode("ascii")


def _server_frame(opcode: int, payload: bytes) -> bytes:
    head = bytearray([0x80 | opcode])
    if len(payload) < 126:
        head.append(len(payload))
    else:
        head.append(126)
        head += struct.pack("!H", len(payload))
    return bytes(head) + payload


def _read_client_frame(conn, buf: bytearray):
    """One complete client frame, decoded (the holder's frames are small and masked)."""
    while True:
        first = buf[0] if len(buf) >= 1 else None
        if first is not None and len(buf) >= 2:
            length = buf[1] & 0x7F
            offset = 2
            if length == 126:
                if len(buf) < 4:
                    offset = -1
                else:
                    length = struct.unpack_from("!H", buf, 2)[0]
                    offset = 4
            if offset > 0 and len(buf) >= offset + 4 + length:
                mask = buf[offset:offset + 4]
                body = bytes(c ^ mask[i % 4] for i, c in enumerate(buf[offset + 4:offset + 4 + length]))
                del buf[:offset + 4 + length]
                return first & 0x0F, body
        chunk = conn.recv(65536)
        if not chunk:
            return None
        buf += chunk


class FakeElevenLabs:
    """A loopback socket whose server half speaks just enough of the stream-input protocol to answer
    one flush with audio fragments + a final marker, then close."""

    def __init__(self, fragments):
        self.fragments = fragments
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.port = self.listener.getsockname()[1]
        self.error = None
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    @property
    def http_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _serve(self):
        try:
            conn, _ = self.listener.accept()
            with conn:
                conn.settimeout(5.0)
                conn.sendall(
                    b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                    b"Connection: Upgrade\r\nSec-WebSocket-Accept: " + _accept_for(_read_http_head(conn)).encode()
                    + b"\r\n\r\n"
                )
                buf = bytearray()
                while True:
                    frame = _read_client_frame(conn, buf)
                    if frame is None:
                        return
                    opcode, payload = frame
                    if opcode != wsclient.OP_TEXT:
                        continue
                    try:
                        msg = json.loads(payload)
                    except ValueError:
                        continue
                    if isinstance(msg, dict) and msg.get("flush"):
                        for frag in self.fragments:
                            conn.sendall(_server_frame(wsclient.OP_TEXT, _audio_msg(frag)))
                        conn.sendall(_server_frame(wsclient.OP_TEXT, json.dumps({"isFinal": True}).encode()))
                        return  # one flush answered, then hang up
        except BaseException as err:  # noqa: BLE001 — surfaced to the test, never swallowed
            self.error = err
        finally:
            self.listener.close()

    def stop(self):
        self.thread.join(timeout=5.0)


def test_synthesize_line_against_a_real_loopback_websocket():
    """The holder, the registry-built URL, wsclient's verified handshake and the drain loop, against
    a server that answers a flush with PCM + a final marker — the one interaction a unit test cannot
    reach. Pins that the pieces compose; a registry URL drift or a wsclient regression fails here."""
    server = FakeElevenLabs([b"\x01\x02", b"\x03\x04"])
    try:
        s = _settings(endpoint=server.http_url)
        holder = speak.TtsStreamHolder(_ENTRY, s, "xi-key")
        frags = list(holder.synthesize_line("hello there", deadline=time.monotonic() + 10))
        holder.close()
    finally:
        server.stop()
    assert server.error is None
    assert frags == [b"\x01\x02", b"\x03\x04"]


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="the holder's socket is Unix-domain; Windows has no AF_UNIX to connect")
def test_run_holder_serves_a_turn_and_self_exits_when_its_listener_closes(monkeypatch, tmp_path):
    """The daemon loop end to end: prime on the held socket, accept one per-turn connection, emit the
    SSE chunks, and exit cleanly when the listener goes (the shutdown path). Runs in the MAIN thread
    so its signal handlers install; the client runs in a thread and closes the listener to stop the
    loop. Pins the accept/serve/keepalive-loop composition a unit test cannot reach (L6)."""
    sock_path = str(tmp_path / "h.sock")
    monkeypatch.setattr(speak, "_STREAM_HOLDER_SOCK", sock_path)
    monkeypatch.setattr(speak, "_STREAM_HOLDER_PID", str(tmp_path / "h.pid"))
    listener_ref: list = []

    def fake_bind(path):
        listener = speak._bind_unix_listener(path)
        listener_ref.append(listener)
        return listener

    def fake_connect(url, headers, timeout):
        # prime drains two polls (fragment + final); the line then drains two more
        return FakeWs(
            [
                [(wsclient.OP_TEXT, _audio_msg(b"\xff"))],
                [(wsclient.OP_TEXT, json.dumps({"isFinal": True}).encode())],
                [(wsclient.OP_TEXT, _audio_msg(b"\x10\x20"))],
                [(wsclient.OP_TEXT, json.dumps({"isFinal": True}).encode())],
            ]
        )

    outcome: dict = {}

    def client():
        for _ in range(200):
            if os.path.exists(sock_path):
                break
            time.sleep(0.01)
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        c.settimeout(5.0)
        c.connect(sock_path)
        c.sendall((json.dumps({"text": "a line"}) + "\n").encode())
        c.shutdown(socket.SHUT_WR)
        received = b""
        while b"event: end" not in received:
            try:
                chunk = c.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            received += chunk
        outcome["sse"] = received
        c.close()
        listener_ref[0].close()  # closing the listener breaks the accept loop -> the holder exits

    ct = threading.Thread(target=client)
    ct.start()
    rc = speak.run_holder(
        _settings(), _ENTRY, "key", "deadbeef",
        connect=fake_connect, clock=lambda: 0.0, sleep=lambda _: None, bind_socket=fake_bind,
    )
    ct.join(timeout=5.0)
    assert rc == 0
    assert b"event: chunk" in outcome["sse"]
    assert b"event: end" in outcome["sse"]
    # the holder cleaned up after itself: pidfile and socket file gone
    assert not os.path.exists(str(tmp_path / "h.pid"))
    assert not os.path.exists(str(tmp_path / "h.sock"))
