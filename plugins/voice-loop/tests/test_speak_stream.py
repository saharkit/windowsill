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
import tempfile
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
        "cloud_endpoint": "",
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


def test_the_digest_changes_when_only_the_cloud_endpoint_changes():
    """windowsill#270: the streaming URL reads ``cloud_endpoint`` first, so two configs that differ
    only in that key cannot share one warm socket — the digest must include it or a settings edit
    would silently reuse the wrong vendor's socket."""
    base = speak.resolve_settings(
        {"tts": {"cloud": {"provider": "elevenlabs", "streaming": True}}}, "Linux"
    )
    overridden = speak.resolve_settings(
        {"tts": {"cloud": {"provider": "elevenlabs", "streaming": True,
                           "endpoint": "https://gateway.internal"}}},
        "Linux",
    )
    assert base["cloud_endpoint"] == ""
    assert overridden["cloud_endpoint"] == "https://gateway.internal"
    assert speak.stream_settings_digest(base) != speak.stream_settings_digest(overridden)


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


def test_the_kill_side_holder_pid_guard_refuses_when_identity_cannot_be_established():
    """The kill-side guard gates a SIGTERM — a recycled PID pointing at an unrelated process would
    terminate it. The guard MUST return False (i.e. refuse to authorise the kill) when identity
    cannot be established: Windows has no cheap identity read; a vanished or unreadable process
    returns ``None`` from the cmdline reader; an unknown platform returns False. Only Linux with a
    readable /proc and macOS with a readable ``ps`` output that match the argv marker authorise."""

    good = f"python3 /path/speak.py {speak.STREAM_HOLDER_ARG} abc123"
    bad = "some other process"

    # Linux: identity via /proc/<pid>/cmdline
    assert speak.pid_is_stream_holder_to_signal(1, read_cmdline=lambda pid: good, platform_id="linux") is True
    assert speak.pid_is_stream_holder_to_signal(1, read_cmdline=lambda pid: bad, platform_id="linux") is False
    assert speak.pid_is_stream_holder_to_signal(1, read_cmdline=lambda pid: None, platform_id="linux") is False

    # macOS: identity via `ps -p <pid> -o command=` (shells no shell, signals nothing)
    assert speak.pid_is_stream_holder_to_signal(1, read_ps_cmdline=lambda pid: good, platform_id="darwin") is True
    assert speak.pid_is_stream_holder_to_signal(1, read_ps_cmdline=lambda pid: bad, platform_id="darwin") is False
    assert speak.pid_is_stream_holder_to_signal(1, read_ps_cmdline=lambda pid: None, platform_id="darwin") is False

    # Windows: no cheap identity read — refuse unconditionally, never signal.
    # The real call site is short-circuited there by the AF_UNIX check in _connect_stream_holder, so
    # returning False is free; this test asserts the FAIL-CLOSED contract even if a future caller
    # forgets to short-circuit.
    assert speak.pid_is_stream_holder_to_signal(1, read_cmdline=lambda pid: good, platform_id="win32") is False
    assert speak.pid_is_stream_holder_to_signal(1, read_ps_cmdline=lambda pid: good, platform_id="win32") is False


def test_ensure_stream_holder_does_not_signal_when_kill_guard_refuses(holder_state, monkeypatch):
    """The kill-side guard is fail-closed: when it cannot establish identity, ``os.kill`` MUST NOT be
    called — a recycled PID pointing at an unrelated process would terminate it. The guard is patched
    to refuse; a stale digest still drives the kill-path branch; a fake ``os.kill`` records any call;
    the assertion proves nothing was signalled."""

    digest = speak.stream_settings_digest(_settings())
    speak._write_stream_holder_pid(os.getpid(), "stale")  # wrong digest -> the kill branch fires

    class P:
        pid = 99999

    killed: list = []
    monkeypatch.setattr(speak.os, "kill", lambda pid, sig: killed.append((pid, sig)))

    def fake_popen(argv, **kw):
        speak._write_stream_holder_pid(P.pid, argv[-1])
        open(speak._STREAM_HOLDER_SOCK, "w").close()
        return P()

    # The kill-side guard refuses: regardless of what /proc would actually read, this pid is not
    # proven to be the holder, so no signal may fire.
    monkeypatch.setattr(speak, "pid_is_stream_holder_to_signal", lambda pid, **kw: False)
    # The warm-reuse guard is a separate call at line 2096 and must not gate the kill decision; it is
    # left at its default — its real /proc read would also refuse on this runner, which is exactly
    # the point of the test.
    assert speak.ensure_stream_holder(_settings(), popen=fake_popen, sleep=lambda _: None, clock=lambda: 0.0) is True
    assert killed == []  # the guard refused; no signal was sent


# --- ensure_stream_holder: warm reuse, stale respawn, spawn failure ------------------------------


@pytest.fixture
def holder_state(monkeypatch, tmp_path):
    """The holder's pidfile and socket live in the test's tmp_path, never the live state dir."""
    monkeypatch.setattr(speak, "_STREAM_HOLDER_PID", str(tmp_path / "holder.pid"))
    monkeypatch.setattr(speak, "_STREAM_HOLDER_SOCK", str(tmp_path / "holder.sock"))
    return tmp_path


@pytest.fixture
def state(monkeypatch, tmp_path):
    """The speak hook's whole state dir lives in tmp_path — the same fixture test_speak.py uses
    for the .coverage file to measure the holder/serve code paths against."""
    monkeypatch.setattr(speak, "_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(speak, "_LOG_PATH", str(tmp_path / "speak.log"))
    return tmp_path


@pytest.fixture
def socket_pair():
    """A connected socket pair (a, b) — the holder's per-turn connection is a, the client
    speaks on b. Closes both when the test finishes."""
    a, b = socket.socketpair()
    try:
        yield a, b
    finally:
        try:
            a.close()
        except OSError:
            pass
        try:
            b.close()
        except OSError:
            pass


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


def test_a_stale_digest_stops_the_old_holder_and_respawns(holder_state, monkeypatch):
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
    monkeypatch.setattr(os, "kill", lambda pid, sig: killed.append((pid, sig)))
    assert speak.ensure_stream_holder(
        _settings(),
        popen=fake_popen,
        sleep=lambda _: None,
        clock=lambda: 0.0,
        holder_guard=lambda pid, **kw: True,
    )
    assert killed == [(4321, speak.signal.SIGTERM)]
    assert spawned and spawned[0][-1] == speak.stream_settings_digest(_settings())


def test_a_spawn_failure_falls_back_to_the_blob_path(holder_state, monkeypatch):
    """A holder that will not start must not stall a turn: ensure returns False and the caller uses
    the blob path for that one turn."""
    speak._write_stream_holder_pid(4321, "stale")  # forces a spawn attempt

    def boom(argv, **kw):
        raise OSError("nope")

    monkeypatch.setattr(speak, "pid_looks_like_stream_holder", lambda pid, **kw: False)
    assert speak.ensure_stream_holder(_settings(), popen=boom, sleep=lambda _: None, clock=lambda: 0.0) is False


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
        # windowsill#270: the cloud override now lives in ``cloud_endpoint`` (rank 1) so the
        # holder's URL builder resolves to the fake loopback server, not the vendor's host.
        s = _settings(cloud_endpoint=server.http_url)
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


# --- coverage for the residual branches on linux -----------------------------------------------
# The branches below each had ONE path exercised by the tests above; the other arm is just as
# load-bearing (a held socket that closed mid-prime, a pidfile that was empty or only one token,
# a unix-socket platform without AF_UNIX, the loop's keepalive / idle-exit / accept-OSError paths).
# They round out the B2 coverage without inventing new behaviour.


def test_prime_swallows_a_drain_error(state, holder_state):
    """A prime whose drain raises (the vendor hung up before the throwaway line finished) is
    non-fatal: the socket is dropped so the next line reconnects, and the holder continues serving.
    The error path lives inside the try/except in prime — send_text must succeed for the error to
    be raised by the drain itself."""
    holder = speak.TtsStreamHolder(
        _ENTRY, _settings(), "key", clock=lambda: 0.0,
    )
    # the FIRST poll returns a close frame, the SECOND is irrelevant — the close ends the drain
    # with TtsStreamClosed, which prime's try/except catches alongside WebSocketError/OSError.
    holder._ws = FakeWs([[(wsclient.OP_CLOSE, b"")]])
    holder._last_send = 0.0
    holder.prime()  # must not raise
    assert holder._ws is None  # dropped — the next ensure_open reconnects
    log = (state / "speak.log").read_text(encoding="utf-8")
    assert "stream holder priming failed" in log


def test_prime_swallows_a_deadline_exceeded_drain(state, holder_state):
    """A prime whose drain finds nothing before its deadline is the same shape: it raises
    TtsStreamClosed, prime logs and drops the socket. The clock returns 0 for the first two
    prime-side reads (so the deadline is the natural 15 s) and then jumps past the deadline
    so the drain's first while check fails immediately."""
    reads = [0]

    def advancing_clock():
        reads[0] += 1
        return 0.0 if reads[0] <= 2 else 1000.0

    holder = speak.TtsStreamHolder(
        _ENTRY, _settings(), "key", clock=advancing_clock,
    )
    holder._ws = FakeWs([[], [], []])  # never answers
    holder._last_send = 0.0
    holder.prime()  # must not raise
    assert holder._ws is None


def test_drain_skips_non_text_opcodes_and_unrecognized_fragments():
    """_drain_until_final is a generator over ws.poll(): non-text opcodes are skipped (binary /
    ping / pong frames don't carry audio), and a fragment the registry cannot parse yields None
    (skipped, not failed). The is_final-only control frame ends the line cleanly."""
    holder = speak.TtsStreamHolder(
        _ENTRY, _settings(), "key", clock=lambda: 0.0,
    )
    holder._ws = FakeWs(
        [
            [(1, b"\x00\x01"), (wsclient.OP_TEXT, json.dumps({"not": "audio shape"}).encode())],
            [(wsclient.OP_TEXT, _audio_msg(b"\x01\x02"))],
            [(wsclient.OP_TEXT, json.dumps({"isFinal": True}).encode())],
        ]
    )
    holder._last_send = 0.0
    assert list(holder.synthesize_line("hi", deadline=999.0)) == [b"\x01\x02"]


def test_keepalive_is_a_noop_when_socket_is_closed_or_last_send_is_none():
    """keepalive_if_due short-circuits when there is no live socket OR no frame has ever been
    sent (a freshly constructed holder before ensure_open has run)."""
    holder = speak.TtsStreamHolder(_ENTRY, _settings(), "key", clock=lambda: 100.0)
    # _ws is None, _last_send is None — both guards fire
    holder.keepalive_if_due()  # must not raise, must not try to send
    # also: a closed socket
    holder._ws = FakeWs([], closed=True)
    holder._last_send = 0.0
    holder.keepalive_if_due()


def test_holder_close_swallows_oserror():
    """A close that raises OSError (the socket is already gone) is tolerated so the holder's
    shutdown path is exception-safe."""
    holder = speak.TtsStreamHolder(_ENTRY, _settings(), "key", clock=lambda: 0.0)

    class BrokenWs(FakeWs):
        def close(self):
            raise OSError("already closed by peer")

    holder._ws = BrokenWs([])
    holder.close()  # must not raise
    assert holder._ws is None


def test_write_stream_holder_pid_is_noop_when_atomic_write_fails(holder_state):
    """_write_stream_holder_pid runs through _atomic_write_text; a denied fs leaves the global
    record untouched so the generation-fence (which compares against _stream_holder_pid_record)
    stays consistent."""
    def deny_replace(src, dst):
        raise OSError("replace denied")

    original_record = speak._stream_holder_pid_record
    speak._stream_holder_pid_record = None
    original_replace = speak.os.replace
    speak.os.replace = deny_replace
    try:
        speak._write_stream_holder_pid(12345, "digest-xyz")
        # the global record was NOT updated — a holder that thinks it wrote the pidfile but didn't
        # would falsely pass the generation fence otherwise
        assert speak._stream_holder_pid_record is None
    finally:
        speak.os.replace = original_replace
        speak._stream_holder_pid_record = original_record


def test_read_stream_holder_pid_handles_unreadable_file(holder_state):
    """A pidfile we cannot open at all is the 'no holder' answer — (None, ''), not an exception."""
    holder_state  # the fixture scopes the state dir
    # remove the fixture's pidfile to force the OSError on read
    if os.path.exists(speak._STREAM_HOLDER_PID):
        os.unlink(speak._STREAM_HOLDER_PID)
    speak._stream_holder_pid_record = None
    # make the read fail by pointing the path at a directory
    os.makedirs(speak._STREAM_HOLDER_PID + ".dir")
    speak._STREAM_HOLDER_PID = speak._STREAM_HOLDER_PID + ".dir"
    try:
        assert speak._read_stream_holder_pid() == (None, "")
    finally:
        speak._STREAM_HOLDER_PID = str(holder_state / "holder.pid")


def test_read_stream_holder_pid_rejects_malformed_records(holder_state):
    """A pidfile that does not parse as `pid digest` (single token, or a non-numeric first token)
    is treated as 'no holder': a stale holder is not trusted on shape alone."""
    with open(speak._STREAM_HOLDER_PID, "w", encoding="utf-8") as fh:
        fh.write("not-a-pid")
    assert speak._read_stream_holder_pid() == (None, "")
    with open(speak._STREAM_HOLDER_PID, "w", encoding="utf-8") as fh:
        fh.write("11111")
    assert speak._read_stream_holder_pid() == (None, "")


def test_clear_stream_holder_pid_swallow_path(holder_state):
    """When the pidfile IS ours (we wrote it) and the unlink itself raises OSError, _clear returns
    False (the cleanup didn't happen) but does not raise. The global record IS still cleared —
    the caller re-installs on next use."""
    original_record = speak._stream_holder_pid_record
    speak._write_stream_holder_pid(54321, "digest-clear")
    assert speak._stream_holder_pid_record is not None

    def deny(path):
        raise OSError("denied")

    original_unlink = speak.os.unlink
    speak.os.unlink = deny
    try:
        result = speak._clear_stream_holder_pid()
        # the global is still cleared — this process's view of "is the pidfile mine" is now false
        assert speak._stream_holder_pid_record is None
    finally:
        speak.os.unlink = original_unlink
        speak._stream_holder_pid_record = original_record
    assert result is False  # the cleanup didn't happen


def test_stream_holder_pid_is_ours_returns_false_when_no_record(holder_state):
    """A fresh process that never wrote a pidfile has _stream_holder_pid_record = None — the
    generation fence is never satisfied for a record that does not exist."""
    original = speak._stream_holder_pid_record
    speak._stream_holder_pid_record = None
    try:
        assert speak._stream_holder_pid_is_ours() is False
    finally:
        speak._stream_holder_pid_record = original


def test_stream_holder_pid_is_ours_swallow_unreadable(holder_state):
    """The pidfile cannot be read at all (an OSError on open) -> the fence returns False rather
    than raising into a holder exit path that has nothing to recover."""
    # point at a path that cannot be opened as a file
    os.makedirs(speak._STREAM_HOLDER_PID + ".dir")
    speak._STREAM_HOLDER_PID = speak._STREAM_HOLDER_PID + ".dir"
    speak._stream_holder_pid_record = "123 digest"  # non-empty, but unreadable
    try:
        assert speak._stream_holder_pid_is_ours() is False
    finally:
        speak._STREAM_HOLDER_PID = str(holder_state / "holder.pid")
        speak._stream_holder_pid_record = None


def test_ensure_stream_holder_handles_a_stale_holder_already_gone(holder_state, monkeypatch):
    """The SIGTERM is best-effort: a holder that died between the pidfile check and the kill
    raises ProcessLookupError, which we swallow."""
    digest = speak.stream_settings_digest(_settings())
    speak._write_stream_holder_pid(os.getpid(), "stale")  # the digest is wrong -> respawn

    def fake_kill(pid, sig):
        raise ProcessLookupError

    killed: list = []
    monkeypatch.setattr(speak.os, "kill", lambda pid, sig: killed.append((pid, sig)) or fake_kill(pid, sig))

    spawned: list = []

    class P:
        pid = 12345

    def fake_popen(argv, **kw):
        spawned.append(argv)
        speak._write_stream_holder_pid(P.pid, argv[-1])
        open(speak._STREAM_HOLDER_SOCK, "w").close()
        return P()

    assert speak.ensure_stream_holder(
        _settings(),
        popen=fake_popen,
        sleep=lambda _: None,
        clock=lambda: 0.0,
        holder_guard=lambda pid, **kw: True,
    )
    assert killed == [(speak.os.getpid(), speak.signal.SIGTERM)]
    assert spawned  # the respawn still happened


def test_ensure_stream_holder_times_out_when_holder_never_binds(holder_state):
    """The holder was spawned but never bound the socket (crashed in prime, lost the pidfile) —
    the wait-loop hits its deadline and the turn uses the blob path."""
    digest = speak.stream_settings_digest(_settings())
    speak._write_stream_holder_pid(os.getpid(), "stale")  # wrong digest -> respawn

    class P:
        pid = 99999

    sleep_calls: list[float] = []

    def fake_popen(argv, **kw):
        return P()

    def fake_sleep(seconds):
        sleep_calls.append(seconds)
        # advance the clock past the deadline by reporting each call
    clocks = [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0]

    def fake_clock():
        return clocks[len(sleep_calls) - 1] if sleep_calls else clocks[0]

    # holder_guard=False disables the kill branch entirely so the wait loop is what we observe here
    result = speak.ensure_stream_holder(_settings(), popen=fake_popen, sleep=fake_sleep, clock=fake_clock, holder_guard=lambda pid, **kw: False)
    assert result is False  # the wait ran out — caller uses the blob path
    assert sleep_calls  # the wait loop actually slept


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="the resident holder is a Unix-domain socket; Windows short-circuits to the blob path")
def test_connect_stream_holder_swallows_socket_error(state, holder_state, monkeypatch, tmp_path):
    """A socket that refuses (or any OSError in connect/sendall/shutdown) is logged and turned
    into None — the same answer as a holder that is not there. The half-opened socket is closed
    best-effort (the inner try/except)."""
    sock_path = str(tmp_path / "nope.sock")
    monkeypatch.setattr(speak, "_STREAM_HOLDER_SOCK", sock_path)
    monkeypatch.setattr(speak, "ensure_stream_holder", lambda s, **kw: True)

    class FlakyConn:
        def settimeout(self, t):
            pass

        def connect(self, path):
            raise OSError("refused")

        def close(self):
            raise OSError("already closed")

    monkeypatch.setattr(speak.socket, "socket", lambda *a, **kw: FlakyConn())
    assert speak._connect_stream_holder("hi", _settings(timeout=1.0)) is None
    assert "could not reach the holder" in (state / "speak.log").read_text(encoding="utf-8")


def test_connect_stream_holder_short_circuits_when_no_af_unix(monkeypatch):
    """The AF_UNIX availability check is FIRST in _connect_stream_holder — without it, Windows
    would never reach ensure_stream_holder, which calls _bind_unix_listener, which raises.
    We patch the imported socket module's AF_UNIX attribute to None and confirm
    ensure_stream_holder is never called (its absence is the short-circuit)."""
    # Remove AF_UNIX from the imported socket module — hasattr(socket, 'AF_UNIX') becomes False
    monkeypatch.delattr(speak.socket, "AF_UNIX", raising=False)

    def must_not_call(*a, **kw):
        raise AssertionError("the AF_UNIX short-circuit must run BEFORE ensure_stream_holder")

    monkeypatch.setattr(speak, "ensure_stream_holder", must_not_call)
    assert speak._connect_stream_holder("hi", _settings()) is None


def test_send_all_swallows_oserror():
    """A send on a closed peer raises OSError; _send_all swallows it so the holder's loop can
    keep going rather than dying on a flaky client."""
    class FlakyConn:
        def sendall(self, data):
            raise OSError("broken pipe")

    speak._send_all(FlakyConn(), b"hello")  # must not raise


def test_serve_stream_connection_handles_empty_request(socket_pair):
    """A line that arrived empty (no text field) emits a single 'end' event with zero chunks —
    no holder.synthesize_line call, no degrade event. The contract is silence, not error."""
    a, b = socket_pair
    holder = speak.TtsStreamHolder(_ENTRY, _settings(), "key", clock=lambda: 0.0)
    holder._ws = FakeWs([[]])  # the holder is never asked to do anything
    holder._last_send = 0.0
    thread = threading.Thread(target=speak._serve_stream_connection, args=(a, holder, _settings()))
    thread.start()
    b.sendall(b'{"text": "   "}\n')
    b.shutdown(socket.SHUT_WR)
    received = b""
    while b"event: end" not in received:
        try:
            chunk = b.recv(65536)
        except socket.timeout:
            break
        if not chunk:
            break
        received += chunk
    thread.join(timeout=3.0)
    assert b"event: end" in received
    assert b'"chunks": 0' in received


def test_serve_stream_connection_handles_unparseable_request(socket_pair):
    """A request that does not parse as JSON is treated as empty: same end-with-zero-chunks shape.
    A request that parses but is not a dict (e.g. an array) is also empty."""
    a, b = socket_pair
    holder = speak.TtsStreamHolder(_ENTRY, _settings(), "key", clock=lambda: 0.0)
    holder._ws = FakeWs([[]])
    holder._last_send = 0.0
    thread = threading.Thread(target=speak._serve_stream_connection, args=(a, holder, _settings()))
    thread.start()
    b.sendall(b"this is not json\n")
    b.shutdown(socket.SHUT_WR)
    received = b""
    while b"event: end" not in received:
        try:
            chunk = b.recv(65536)
        except socket.timeout:
            break
        if not chunk:
            break
        received += chunk
    thread.join(timeout=3.0)
    assert b"event: end" in received


def test_bind_unix_listener_raises_when_af_unix_absent(monkeypatch, tmp_path):
    """A platform without AF_UNIX (Windows) raises OSError so the caller's catch in run_holder
    can log 'could not bind its socket' and exit 1. The pre-bind unlink is also exercised."""
    # patch the attribute speak reads so hasattr(socket, 'AF_UNIX') returns False
    monkeypatch.delattr(speak.socket, "AF_UNIX", raising=False)
    if not hasattr(speak.socket, "AF_UNIX"):  # sanity
        with pytest.raises(OSError, match="Unix-domain sockets"):
            speak._bind_unix_listener(str(tmp_path / "x.sock"))


def test_run_holder_returns_1_when_bind_fails(state, monkeypatch):
    """bind_socket raising OSError is the "could not bind" path: log + clear pidfile + return 1,
    not an exception. The pidfile is cleared so ensure_stream_holder's next attempt can spawn
    fresh."""
    monkeypatch.setattr(speak, "_STREAM_HOLDER_PID", str(state / "h.pid"))
    monkeypatch.setattr(speak, "_STREAM_HOLDER_SOCK", str(state / "h.sock"))
    speak._write_stream_holder_pid(os.getpid(), "deadbeef")
    assert os.path.exists(speak._STREAM_HOLDER_PID)

    def bind_fail(path):
        raise OSError("address already in use")

    rc = speak.run_holder(
        _settings(), _ENTRY, "key", "deadbeef",
        connect=lambda url, headers, timeout: FakeWs([]),
        clock=lambda: 0.0,
        sleep=lambda _: None,
        bind_socket=bind_fail,
    )
    assert rc == 1
    assert not os.path.exists(speak._STREAM_HOLDER_PID)
    log = (state / "speak.log").read_text(encoding="utf-8")
    assert "could not bind its socket" in log


def test_run_holder_exits_when_accept_raises_oserror(monkeypatch, tmp_path):
    """A OSError on accept() (the listener was closed externally) breaks the loop, the finally
    cleans up, and the function returns 0. Prime is skipped so the fake ws cannot hang the test."""
    sock_path = str(tmp_path / "h.sock")
    monkeypatch.setattr(speak, "_STREAM_HOLDER_SOCK", sock_path)
    monkeypatch.setattr(speak, "_STREAM_HOLDER_PID", str(tmp_path / "h.pid"))

    accept_count = [0]

    class ErroringListener:
        def settimeout(self, t):
            pass

        def accept(self):
            accept_count[0] += 1
            raise OSError("listener closed")

        def close(self):
            pass

    monkeypatch.setattr(speak.TtsStreamHolder, "prime", lambda self: None)

    rc = speak.run_holder(
        _settings(), _ENTRY, "key", "deadbeef",
        connect=lambda url, headers, timeout: FakeWs([]),
        clock=lambda: 0.0,
        sleep=lambda _: None,
        bind_socket=lambda path: ErroringListener(),
    )
    assert rc == 0
    assert accept_count[0] >= 1  # the OSError path was exercised


def test_run_holder_idle_exits_when_keepalive_loop_never_quiet(monkeypatch, tmp_path):
    """The idle-exit path (clock - last_activity >= STREAM_HOLDER_IDLE_EXIT). STREAM_HOLDER_IDLE_EXIT
    is 0 so the very first accept-timeout satisfies the condition; prime is skipped to keep the
    test off the websocket drain loop."""
    sock_path = str(tmp_path / "h.sock")
    monkeypatch.setattr(speak, "_STREAM_HOLDER_SOCK", sock_path)
    monkeypatch.setattr(speak, "_STREAM_HOLDER_PID", str(tmp_path / "h.pid"))
    monkeypatch.setattr(speak, "STREAM_HOLDER_IDLE_EXIT", 0.0)
    monkeypatch.setattr(speak, "STREAM_KEEPALIVE_SECONDS", 0.0)

    class TimeoutListener:
        def settimeout(self, t):
            pass

        def accept(self):
            raise socket.timeout

        def close(self):
            pass

    # prime would hang waiting on a fake ws that never answers; skip it so the loop is reachable.
    monkeypatch.setattr(speak.TtsStreamHolder, "prime", lambda self: None)

    rc = speak.run_holder(
        _settings(), _ENTRY, "key", "deadbeef",
        connect=lambda url, headers, timeout: FakeWs([]),
        clock=lambda: 0.0,
        sleep=lambda _: None,
        bind_socket=lambda path: TimeoutListener(),
    )
    assert rc == 0
    assert not os.path.exists(sock_path)


def test_run_holder_keepalive_arm_continues_when_idle_exit_far(monkeypatch, tmp_path):
    """Lines 2238-2239 + 2231->2252: when listener.accept() raises socket.timeout but the
    idle-exit deadline is far away (clock stays flat so clock() - last_activity stays below
    STREAM_HOLDER_IDLE_EXIT), the loop calls holder.keepalive_if_due() and `continue`s. Distinct
    from the idle-exit arm at 2235-2237 which logs and breaks (covered by
    test_run_holder_idle_exits_when_keepalive_loop_never_quiet). The handler we trip from
    keepalive_if_due is the SIGTERM handler the loop just registered — calling it is what sets
    stopping["now"] = True, and the very next while-check takes the 2231->2252 branch (exit by
    CONDITION, not by break)."""
    sock_path = str(tmp_path / "h.sock")
    monkeypatch.setattr(speak, "_STREAM_HOLDER_SOCK", sock_path)
    monkeypatch.setattr(speak, "_STREAM_HOLDER_PID", str(tmp_path / "h.pid"))
    # huge deadline so the idle-exit arm never fires — this test exercises the KEEPALIVE arm.
    monkeypatch.setattr(speak, "STREAM_HOLDER_IDLE_EXIT", 3600.0)
    monkeypatch.setattr(speak, "STREAM_KEEPALIVE_SECONDS", 0.0)

    # Capture the signal handlers run_holder registers, so keepalive_if_due can trip stopping.
    captured_handlers: list = []
    real_signal = speak.signal.signal
    def _capture(sig, handler):
        captured_handlers.append(handler)
        return real_signal(sig, handler)
    monkeypatch.setattr(speak.signal, "signal", _capture)

    class TimeoutListener:
        def settimeout(self, t):
            pass

        def accept(self):
            raise socket.timeout

        def close(self):
            pass

    keepalive_calls = [0]

    def trip_handler_keepalive(self):
        keepalive_calls[0] += 1
        # After keepalive fires once, set stopping["now"] = True via the captured SIGTERM handler
        # so the next while-check takes the exit-by-condition branch (2231->2252).
        captured_handlers[0](speak.signal.SIGTERM, None)

    # prime would hang on a fake ws; skip it so the loop is reachable.
    monkeypatch.setattr(speak.TtsStreamHolder, "prime", lambda self: None)
    monkeypatch.setattr(speak.TtsStreamHolder, "keepalive_if_due", trip_handler_keepalive)

    rc = speak.run_holder(
        _settings(), _ENTRY, "key", "deadbeef",
        connect=lambda url, headers, timeout: FakeWs([]),
        clock=lambda: 0.0,
        sleep=lambda _: None,
        bind_socket=lambda path: TimeoutListener(),
    )
    assert rc == 0
    assert keepalive_calls[0] >= 1, "keepalive_if_due must fire when the idle-exit deadline is far"


def test_run_holder_swallows_per_turn_conn_close_error(monkeypatch, tmp_path):
    """Lines 2248-2249: the inner finally closes the per-turn conn; if conn.close() raises
    OSError (the conn was already torn down by a signal or by the peer), the exception is
    swallowed so the next iteration's accept() still runs and last_activity bookkeeping is
    kept honest."""
    sock_path = str(tmp_path / "h.sock")
    monkeypatch.setattr(speak, "_STREAM_HOLDER_SOCK", sock_path)
    monkeypatch.setattr(speak, "_STREAM_HOLDER_PID", str(tmp_path / "h.pid"))

    class CloseRaisingConn:
        def close(self):
            raise OSError("conn already torn down")

    accept_calls = [0]

    class OneConnThenOseError:
        def settimeout(self, t):
            pass

        def accept(self):
            accept_calls[0] += 1
            if accept_calls[0] == 1:
                return (CloseRaisingConn(), None)
            # Second accept raises OSError — the existing test_run_holder_exits_when_accept_raises_oserror
            # covers the 2240-2241 `break` arm; we use it here as a clean way to exit the loop AFTER
            # the inner finally has already swallowed conn.close()'s OSError.
            raise OSError("listener torn down")

        def close(self):
            pass

    monkeypatch.setattr(speak.TtsStreamHolder, "prime", lambda self: None)
    # _serve_stream_connection's body calls conn.makefile() etc.; our fake conn doesn't have those,
    # so substitute a no-op serve — the inner finally's conn.close() (the line under test) still runs.
    monkeypatch.setattr(speak, "_serve_stream_connection", lambda conn, holder, s, *, clock: None)

    rc = speak.run_holder(
        _settings(), _ENTRY, "key", "deadbeef",
        connect=lambda url, headers, timeout: FakeWs([]),
        clock=lambda: 0.0,
        sleep=lambda _: None,
        bind_socket=lambda path: OneConnThenOseError(),
    )
    assert rc == 0
    assert accept_calls[0] >= 1


def test_run_holder_swallows_listener_close_error_in_outer_finally(monkeypatch, tmp_path):
    """Lines 2255-2256: the outer finally closes the listener; if listener.close() raises
    OSError (e.g., the unix domain socket file was already unlinked), the exception is swallowed
    so _holder_exit_cleanup still runs. The loop itself exits via the existing 2240-2241 accept-
    OSError break arm (covered by test_run_holder_exits_when_accept_raises_oserror) so this test
    does not double up on the keepalive or condition-exit branches."""
    sock_path = str(tmp_path / "h.sock")
    monkeypatch.setattr(speak, "_STREAM_HOLDER_SOCK", sock_path)
    monkeypatch.setattr(speak, "_STREAM_HOLDER_PID", str(tmp_path / "h.pid"))

    class ListenerCloseRaises:
        def settimeout(self, t):
            pass

        def accept(self):
            raise OSError("listener torn down externally")

        def close(self):
            raise OSError("listener close already failed once")

    monkeypatch.setattr(speak.TtsStreamHolder, "prime", lambda self: None)

    rc = speak.run_holder(
        _settings(), _ENTRY, "key", "deadbeef",
        connect=lambda url, headers, timeout: FakeWs([]),
        clock=lambda: 0.0,
        sleep=lambda _: None,
        bind_socket=lambda path: ListenerCloseRaises(),
    )
    assert rc == 0


def test_run_holder_main_refuses_when_clear_text_policy_rejects(state, monkeypatch, tmp_path):
    """The endpoint-policy (#215) check in run_holder_main runs after key+provider, and a
    clear-text credential over a non-local host is refused before the loop starts."""
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({
        "tts": {"backend": "cloud", "cloud": {"provider": "elevenlabs", "streaming": True}}
    }))
    monkeypatch.setenv("VOICE_LOOP_CONFIG", str(cfg))
    monkeypatch.setenv("VOICE_LOOP_TTS_API_KEY", "k")

    # monkeypatch the policy seam to refuse — without it the real resolver would either allow
    # localhost or run into the real network
    monkeypatch.setattr(speak, "_clear_text_refusal", lambda s: "cloud tts refused: http on a remote host")
    assert speak.run_holder_main("deadbeef") == 1
    log = (state / "speak.log").read_text(encoding="utf-8")
    assert "cloud tts refused" in log


def test_run_holder_main_folds_speed_into_voice_settings(state, monkeypatch, tmp_path):
    """The last step before run_holder: speed is folded into voice_settings so the BOS carries
    it on the held socket's first frame. Without this, a config-level speed knob never reaches
    the websocket."""
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"tts": {"backend": "cloud", "cloud": {"provider": "elevenlabs", "streaming": True}}}))
    monkeypatch.setenv("VOICE_LOOP_CONFIG", str(cfg))
    monkeypatch.setenv("VOICE_LOOP_TTS_API_KEY", "k")

    captured: dict = {}

    def fake_run_holder(s, entry, key, digest):
        captured["voice_settings"] = s["voice_settings"]
        captured["digest"] = digest
        return 0

    # stub the policy seam so run_holder_main doesn't try to read the real network
    monkeypatch.setattr(speak, "_clear_text_refusal", lambda s: None)
    monkeypatch.setattr(speak, "run_holder", fake_run_holder)
    assert speak.run_holder_main("deadbeef") == 0
    # speed was folded into voice_settings — the BOS will carry it
    assert captured["voice_settings"] == {"speed": 1.0}


# --- the AF_UNIX path-length bound (windowsill#285) ---------------------------------------------
# The stream holder binds an AF_UNIX socket at `_STREAM_HOLDER_SOCK`. macOS caps ``sun_path`` at
# 104 bytes; Linux at 108; the previous `os.path.join(_STATE_DIR, "speak-stream.sock")` rides on
# the state dir's length, which on a GitHub-hosted macOS pytest `tmp_path` is 80-95 chars — adding
# the basename pushes the bound path past 104 on Darwin while staying inside the Linux one, so
# #276's macOS leg reds with `AF_UNIX path too long` even though the Linux leg stays green. The
# tests below pin the new derived-path shape: a length bound that holds REGARDLESS of how deep
# the state dir is, the single-constant property the holder and client share, per-instance
# isolation across distinct state dirs, and the XDG_RUNTIME_DIR selection/fallback contract.

_DARWIN_SUN_PATH_LIMIT = 104  # the documented macOS sockaddr_un.sun_path ceiling


def _make_deep_state_dir(parent: Path, length: int) -> Path:
    """Build a state dir whose absolute path is at least `length` bytes. Uses multiple nested
    subdirs (each up to ~120 chars, well under the 255-char-per-component cap) rather than one
    long name — Linux's NAME_MAX is 255 and the absolute-path limit is 4096, so a 400-char state
    dir is constructed as parent / subA / subB rather than parent / 'x'*400, which would itself
    overflow NAME_MAX. The function does NOT need the dir to actually exist on disk for the
    test that exercises the length bound — but it does need ``os.path.abspath`` to resolve the
    same path both here and inside ``_stream_holder_sock_path``, which it does whether the dir
    exists or not."""
    parent_str = str(parent)
    if len(parent_str) >= length:
        return parent  # the parent already satisfies the requested length
    # Fill the gap with as many 120-char nested subdirs as we need.
    gap = length - len(parent_str)
    cur = parent
    while len(str(cur)) < length:
        remaining = length - len(str(cur)) - 1  # -1 for the separator
        chunk = "x" * min(120, max(1, remaining))
        cur = cur / chunk
    return cur


def _short_runtime_dir(monkeypatch) -> Path:
    """A SHORT writable directory the bound-shape tests can use as XDG_RUNTIME_DIR. Pytest's own
    ``tmp_path`` is routinely 70+ chars deep on a Linux runner (``/tmp/pytest-of-<user>/pytest-NN/
    test_<name>``), which itself crosses the Darwin 104-byte limit when a 34-char basename is
    appended — so testing the bound against ``tmp_path`` would exercise the directory length,
    not the derivation. This builds a fresh directory under ``tempfile.gettempdir()`` (which is
    ``/tmp`` on Linux, ``$TMPDIR`` on macOS) with a unique short name; the parent path is
    deliberately SHORT so the 34-char basename + the parent + the separator still fits under
    the Darwin sun_path cap."""
    base = Path(tempfile.gettempdir())
    short = tempfile.mkdtemp(prefix="vlrt-", dir=str(base))
    return Path(short)


def test_stream_holder_sock_path_stays_inside_the_darwin_sun_path_limit(monkeypatch, tmp_path):
    """The Darwin ``sun_path`` ceiling is 104 bytes; the OLD `os.path.join(state_dir, "speak-stream.sock")`
    rode on the state dir's length and crossed 104 on macOS for a deep pytest tmp_path. The
    derived path puts the socket in a short runtime/temp directory and bounds the basename
    itself, so the result is inside the limit REGARDLESS of how deep the state dir is. This
    test would FAIL on the OLD shape (the bound `os.path.join(state_dir, "speak-stream.sock")`
    is `len(state_dir) + 1 + len("speak-stream.sock")`, and a 120-byte state dir already exceeds
    104+18 = 122 chars) and PASSES after the change."""
    # Use a SHORT runtime dir as XDG_RUNTIME_DIR — pytest's own tmp_path is 70+ chars deep on a
    # Linux runner and would itself cross 104 when a 34-char basename is appended, masking the
    # derivation's bound in directory noise. A short sibling isolates the test to the
    # derivation's invariant: basename 34 chars + directory ≤ 69 chars ≤ 104 total.
    runtime = _short_runtime_dir(monkeypatch)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    state_dir = _make_deep_state_dir(tmp_path, length=120)  # 120-byte absolute state dir
    sock = speak._stream_holder_sock_path(str(state_dir))
    assert len(sock.encode("utf-8")) <= _DARWIN_SUN_PATH_LIMIT, (
        f"derived sock path {sock!r} is {len(sock)} bytes — Darwin sun_path caps at 104; "
        "the path-shape fix failed to bound the result"
    )
    # The basename is the bounded part of the shape — must not echo the state dir verbatim.
    assert os.path.dirname(sock) != str(state_dir)
    # The basename mixes the state dir's hash so two deep dirs still resolve to the same
    # directory but distinct paths.
    assert "speak-stream-" in os.path.basename(sock)
    assert sock.endswith(".sock")


def test_stream_holder_sock_path_basename_is_bounded_regardless_of_state_dir_length(
    monkeypatch, tmp_path,
):
    """Whatever the state dir's length, the basename is `speak-stream-` + 16 hex + `.sock`
    (34 chars by construction). The bound carries the entire path inside the Darwin limit
    because the directory itself is short. The OLD shape's basename was a fixed 17 chars but
    sat under an UNBOUNDED directory — this test pins the new bounded-basename invariant."""
    runtime = _short_runtime_dir(monkeypatch)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    for length in (80, 120, 200, 400):
        state_dir = _make_deep_state_dir(tmp_path, length=length)
        assert len(str(state_dir)) >= length, (
            f"_make_deep_state_dir could not reach length {length}; got {str(state_dir)!r} "
            f"({len(str(state_dir))} chars)"
        )
        sock = speak._stream_holder_sock_path(str(state_dir))
        basename = os.path.basename(sock)
        assert len(basename) == 34, (
            f"basename {basename!r} is {len(basename)} chars at state_dir length {length}; "
            "the bounded shape must hold regardless of state_dir depth"
        )
        # The 16-hex digest must come from the state dir, not be hardcoded — different state
        # dirs at the same length still get distinct basenames.
        digest_in_basename = basename[len("speak-stream-"):-len(".sock")]
        expected_digest = hashlib.sha256(os.path.abspath(str(state_dir)).encode("utf-8")).hexdigest()[:16]
        assert digest_in_basename == expected_digest


def test_stream_holder_sock_path_is_stable_for_one_state_dir(monkeypatch, tmp_path):
    """The single-constant property: the holder binds and the client connects, and the two must
    resolve the SAME path for the SAME state dir. Drift between bind and connect would be a
    silent socket-not-found failure rather than a loud error. Calling the function twice with
    the same state dir pins that the derivation is deterministic — there is no place for bind
    and connect to disagree except by both calling this function with the same arg."""
    runtime = _short_runtime_dir(monkeypatch)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    state_dir = str(tmp_path / "instance-A")
    first = speak._stream_holder_sock_path(state_dir)
    second = speak._stream_holder_sock_path(state_dir)
    assert first == second
    # The module-level constant derives from the import-time _STATE_DIR; it must also match
    # what a fresh call with the same state dir returns. (This is the drift guard: if a future
    # refactor puts a different path in `_STREAM_HOLDER_SOCK` than the function computes, this
    # catches it.)
    monkeypatch.setattr(speak, "_STATE_DIR", state_dir)
    monkeypatch.setattr(speak, "_STREAM_HOLDER_SOCK", speak._stream_holder_sock_path(state_dir))
    assert speak._STREAM_HOLDER_SOCK == first


def test_stream_holder_sock_path_differs_per_state_dir(monkeypatch, tmp_path):
    """Per-instance isolation: every test gets its own tmp_path and therefore its own socket,
    so two concurrent instances of the hook (two users on the same machine, two pytest runs in
    parallel) do not collide. The OLD shape's `speak-stream.sock` was a fixed name and would
    have collided across instances; the NEW shape hashes the state dir into the basename so
    distinct state dirs always resolve to distinct paths."""
    runtime = _short_runtime_dir(monkeypatch)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    a = speak._stream_holder_sock_path(str(tmp_path / "instance-A"))
    b = speak._stream_holder_sock_path(str(tmp_path / "instance-B"))
    assert a != b
    # Sanity: each path still ends in the same bounded basename shape.
    for path in (a, b):
        basename = os.path.basename(path)
        assert basename.startswith("speak-stream-") and basename.endswith(".sock")


def test_stream_holder_sock_path_uses_xdg_runtime_dir_when_writable(monkeypatch, tmp_path):
    """When XDG_RUNTIME_DIR is set and writable, the socket lives there — the per-user runtime
    directory the spec mandates. On a typical Linux desktop this is /run/user/<uid>, which is
    short and where a per-user socket belongs."""
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_dir))
    sock = speak._stream_holder_sock_path(str(tmp_path / "state"))
    assert os.path.dirname(sock) == str(runtime_dir)


def test_stream_holder_sock_path_falls_back_to_tempfile_gettempdir_when_xdg_runtime_dir_unwritable(
    monkeypatch, tmp_path,
):
    """When XDG_RUNTIME_DIR is set but unwritable (a stale mount, a chmod 000 directory), the
    function falls back to ``tempfile.gettempdir()`` rather than failing the bind. On macOS
    this resolves to the per-user ``$TMPDIR`` under ``/var/folders/.../T/`` — where the socket
    belongs anyway, and which is the EXPECTED fallback on a machine that does not set
    ``$XDG_RUNTIME_DIR`` at all. The unwritable check uses ``os.access(W_OK)``, which returns
    True for root regardless of mode bits — so the test runs as non-root (the default for this
    repo's suite); running as root would silently let the bad path through, which is why the
    bound-shape invariant above is the load-bearing test."""
    # ``chmod 000`` only bites for non-root; skip if we cannot exercise the fallback.
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("os.access returns True for root regardless of mode bits; the XDG_RUNTIME_DIR "
                    "fallback cannot be exercised as root — the bounded-shape test above pins the "
                    "real invariant")
    runtime_dir = tmp_path / "unwritable"
    runtime_dir.mkdir()
    try:
        os.chmod(runtime_dir, 0o000)
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_dir))
        sock = speak._stream_holder_sock_path(str(tmp_path / "state"))
        assert os.path.dirname(sock) == tempfile.gettempdir(), (
            f"XDG_RUNTIME_DIR is unwritable; the function must fall back to "
            f"tempfile.gettempdir(), got {os.path.dirname(sock)!r}"
        )
    finally:
        os.chmod(runtime_dir, 0o755)


def test_stream_holder_sock_path_falls_back_when_xdg_runtime_dir_is_unset(monkeypatch, tmp_path):
    """macOS does not set XDG_RUNTIME_DIR by default; the function must still produce a path,
    and the directory must be a real temp directory (``tempfile.gettempdir()``, which on macOS
    is the per-user $TMPDIR). This is the path the macOS CI leg #276 adds will exercise."""
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    sock = speak._stream_holder_sock_path(str(tmp_path / "state"))
    assert os.path.dirname(sock) == tempfile.gettempdir()
    # And the result still satisfies the Darwin bound.
    assert len(sock.encode("utf-8")) <= _DARWIN_SUN_PATH_LIMIT


def test_module_level_stream_holder_sock_uses_the_derived_path(monkeypatch, tmp_path):
    """The module-level `_STREAM_HOLDER_SOCK` is the SINGLE constant the holder binds and the
    client connects to; it must be the function's output for the import-time `_STATE_DIR`, not
    the OLD `os.path.join(_STATE_DIR, "speak-stream.sock")`. This pins the single-constant
    property at the module level rather than just at the function level — a regression that
    restored the OLD shape at the constant would not be caught by the function-level tests."""
    # Reset to a known state dir and clear XDG_RUNTIME_DIR so the path lands in the tempdir.
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    state_dir = str(tmp_path / "module-level-state")
    monkeypatch.setattr(speak, "_STATE_DIR", state_dir)
    # The module constant was already computed at import time using the ORIGINAL state dir.
    # Compute what it MUST equal for the new state dir and assert the constant was derived
    # through the function, not the OLD `os.path.join` shape.
    expected = speak._stream_holder_sock_path(state_dir)
    # The OLD shape would be `os.path.join(state_dir, "speak-stream.sock")` — that path
    # contains the literal state dir as a prefix component, and its basename is the fixed
    # "speak-stream.sock" (17 chars, no hex digest). The new shape's basename is 34 chars and
    # mixes the state's hash. The module constant must look like the new shape.
    assert os.path.basename(speak._STREAM_HOLDER_SOCK).startswith("speak-stream-")
    assert not speak._STREAM_HOLDER_SOCK.endswith("speak-stream.sock"), (
        "module-level _STREAM_HOLDER_SOCK still has the OLD basename — the single-constant "
        "drift guard failed"
    )
    # And the same derivation applied to a fresh state dir gives the path the module would
    # produce after the import-time monkeypatch — i.e. the function is the only place the
    # path is computed.
    assert speak._stream_holder_sock_path(state_dir) == expected
