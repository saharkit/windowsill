"""The stdlib websocket client (windowsill#99), against a REAL socket.

Nothing here reaches the network: every test stands up a listening socket on 127.0.0.1 and speaks
the protocol to it by hand. That is deliberate rather than convenient — this module exists because
the stdlib has no websocket client, so a fake of "a websocket" would be a fake of the very thing
under test. The server side is written out in bytes in this file (its own frame decoder, its own
handshake response), so a client bug cannot cancel out against a server built from the same code.

The two halves that matter most are the ones a protocol bug would ship quietly: a handshake that is
VERIFIED (an endpoint answering 101 without the accept token is not a websocket server, and framing
audio into it would be a dictation nobody ever receives), and the untrusted-input bounds on the read
path — an oversized declared length, a masked server frame, a reserved bit.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import socket
import ssl
import struct
import threading
from pathlib import Path

import pytest

_WSCLIENT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "wsclient.py"
_spec = importlib.util.spec_from_file_location("wsclient", _WSCLIENT_PATH)
wsclient = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wsclient)


# --- the server side, written out by hand ---------------------------------------------------------


def read_http_head(conn) -> bytes:
    """The request line and headers, exactly as the client sent them."""
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = conn.recv(4096)
        if not chunk:
            break
        buf += chunk
    return buf


def accept_for(head: bytes) -> str:
    """The RFC's answer to the client's key, computed here rather than imported, so a bug in the
    client's own token maths cannot pass this test by symmetry."""
    key = ""
    for line in head.decode("utf-8", "replace").split("\r\n"):
        name, sep, value = line.partition(":")
        if sep and name.strip().lower() == "sec-websocket-key":
            key = value.strip()
    digest = hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
    return base64.b64encode(digest).decode("ascii")


def server_frame(opcode: int, payload: bytes) -> bytes:
    """One unmasked server→client frame, framed by hand (the server direction of the RFC)."""
    head = bytearray([0x80 | opcode])
    if len(payload) < 126:
        head.append(len(payload))
    elif len(payload) < (1 << 16):
        head.append(126)
        head += struct.pack("!H", len(payload))
    else:
        head.append(127)
        head += struct.pack("!Q", len(payload))
    return bytes(head) + payload


def parse_client_frame(buf: bytearray):
    """The client's own frames, decoded independently: (opcode, payload) or None while partial.

    A client frame MUST be masked, and this decoder asserts that rather than tolerating it — the
    masking bug this catches is invisible to every server that unmasks anyway."""
    if len(buf) < 2:
        return None
    first, second = buf[0], buf[1]
    opcode = first & 0x0F
    assert second & 0x80, "a client frame arrived unmasked, which the RFC forbids"
    length = second & 0x7F
    offset = 2
    if length == 126:
        length = struct.unpack_from("!H", buf, 2)[0]
        offset = 4
    elif length == 127:
        length = struct.unpack_from("!Q", buf, 2)[0]
        offset = 10
    if len(buf) < offset + 4 + length:
        return None
    mask = buf[offset : offset + 4]
    body = buf[offset + 4 : offset + 4 + length]
    del buf[: offset + 4 + length]
    return opcode, bytes(byte ^ mask[index % 4] for index, byte in enumerate(body))


class Server:
    """A listening socket on loopback, driven by one handler function in its own thread."""

    def __init__(self, handler) -> None:
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.port = self.listener.getsockname()[1]
        self.frames: list[tuple[int, bytes]] = []
        self.failure: BaseException | None = None
        self._handler = handler
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/v1/listen?model=nova-2"

    def _serve(self) -> None:
        try:
            conn, _ = self.listener.accept()
            with conn:
                conn.settimeout(5.0)
                self._handler(self, conn)
        except BaseException as err:  # noqa: BLE001 — reported to the test, never swallowed
            self.failure = err
        finally:
            self.listener.close()

    def read_frames(self, conn, count: int) -> list[tuple[int, bytes]]:
        """Block until ``count`` complete client frames have arrived."""
        buf = bytearray()
        while len(self.frames) < count:
            chunk = conn.recv(65536)
            if not chunk:
                break
            buf += chunk
            while True:
                frame = parse_client_frame(buf)
                if frame is None:
                    break
                self.frames.append(frame)
        return self.frames

    def stop(self) -> None:
        self.thread.join(timeout=5.0)


@pytest.fixture
def upgrade():
    """A handler prelude: complete the handshake, then hand the connection back."""

    def handshake(conn, accept: str | None = None) -> None:
        head = read_http_head(conn)
        token = accept_for(head) if accept is None else accept
        conn.sendall(
            b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
            b"Connection: Upgrade\r\nSec-WebSocket-Accept: " + token.encode("ascii") + b"\r\n\r\n"
        )

    return handshake


class TestFraming:
    """The wire format, before any socket is involved."""

    def test_a_client_frame_is_masked_and_unmasks_to_its_payload(self):
        frame = wsclient.build_frame(wsclient.OP_TEXT, b"hello", b"\x01\x02\x03\x04")
        assert frame[0] == 0x81  # FIN + text
        assert frame[1] == 0x80 | 5  # masked, five bytes
        assert frame[2:6] == b"\x01\x02\x03\x04"
        assert parse_client_frame(bytearray(frame)) == (wsclient.OP_TEXT, b"hello")

    @pytest.mark.parametrize("size", [125, 126, 65535, 65536])
    def test_every_length_encoding_round_trips(self, size):
        """126 and 65536 are the two boundaries where the length grows a field — and a stream of
        250 ms PCM chunks lives right in the middle of the 16-bit one."""
        payload = b"\x7f" * size
        frame = wsclient.build_frame(wsclient.OP_BINARY, payload, b"\xaa\xbb\xcc\xdd")
        assert parse_client_frame(bytearray(frame)) == (wsclient.OP_BINARY, payload)

    def test_the_accept_token_is_the_rfc_s(self):
        # the RFC's own worked example, so the maths is pinned to the spec rather than to itself
        assert wsclient._accept_token("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


class TestHandshake:
    """The opening exchange, over a real socket."""

    def test_a_good_upgrade_opens_the_connection(self, upgrade):
        def handler(server, conn):
            upgrade(conn)
            conn.sendall(server_frame(wsclient.OP_TEXT, b'{"type":"Metadata"}'))

        server = Server(handler)
        ws = wsclient.connect(server.url, {"Authorization": "Token secret"})
        assert ws.poll(5.0) == [(wsclient.OP_TEXT, b'{"type":"Metadata"}')]
        ws.close()
        server.stop()
        assert server.failure is None

    def test_the_request_carries_the_auth_header_and_the_query(self, upgrade):
        seen = {}

        def handler(server, conn):
            seen["head"] = read_http_head(conn)
            upgrade_head = seen["head"]
            conn.sendall(
                b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                b"Sec-WebSocket-Accept: " + accept_for(upgrade_head).encode() + b"\r\n\r\n"
            )

        server = Server(handler)
        wsclient.connect(server.url, {"Authorization": "Token secret"}).close()
        server.stop()
        head = seen["head"].decode()
        assert head.startswith("GET /v1/listen?model=nova-2 HTTP/1.1\r\n")
        assert "Authorization: Token secret\r\n" in head
        assert "Sec-WebSocket-Version: 13\r\n" in head

    def test_a_refusal_is_named_by_its_status_and_nothing_more(self):
        """A 401 body from a speech API is where the account detail lives — and where a request
        echo could carry the key. The status line is the diagnosis; the body never travels."""

        def handler(server, conn):
            read_http_head(conn)
            conn.sendall(
                b"HTTP/1.1 401 Unauthorized\r\nContent-Length: 41\r\n\r\n"
                b'{"err_msg": "Token sk-live-never-log-me"}'
            )

        server = Server(handler)
        with pytest.raises(wsclient.WebSocketError) as caught:
            wsclient.connect(server.url, {})
        server.stop()
        assert "401 Unauthorized" in str(caught.value)
        assert "sk-live" not in str(caught.value)

    def test_a_wrong_accept_token_is_not_a_websocket_server(self, upgrade):
        """101 alone proves nothing: a cache or a proxy can answer it. Framing binary audio into
        whatever this actually is would be a dictation that silently reaches nobody."""

        def handler(server, conn):
            upgrade(conn, accept="not-the-answer-to-our-key")

        server = Server(handler)
        with pytest.raises(wsclient.WebSocketError, match="Sec-WebSocket-Accept"):
            wsclient.connect(server.url, {})
        server.stop()

    def test_a_peer_that_never_ends_its_headers_is_refused(self):
        def handler(server, conn):
            read_http_head(conn)
            while True:
                conn.sendall(b"X-Padding: " + b"y" * 1000 + b"\r\n")

        server = Server(handler)
        with pytest.raises(wsclient.WebSocketError, match="never ended"):
            wsclient.connect(server.url, {})

    def test_a_url_that_is_not_a_websocket_url_never_opens_a_socket(self):
        def never(address, timeout):
            raise AssertionError("a non-websocket URL must be refused before any connect")

        for url in ("https://api.example/v1/listen", "not a url at all"):
            with pytest.raises(wsclient.WebSocketError):
                wsclient.connect(url, {}, connector=never)

    def test_a_host_that_refuses_the_connection_is_one_error_type(self):
        with pytest.raises(wsclient.WebSocketError, match="could not reach"):
            # port 0 is never listening; the point is the ERROR TYPE, so one except degrades
            wsclient.connect("ws://127.0.0.1:1/v1/listen", {}, timeout=1.0)

    def test_a_websocket_url_with_no_host_is_refused(self):
        with pytest.raises(wsclient.WebSocketError, match="names no host"):
            wsclient.connect("ws:///v1/listen", {})


class TestFrames:
    """Reading, over a real socket: fragments, control frames, and the untrusted-input bounds."""

    def test_binary_frames_arrive_at_the_server_intact_and_in_order(self, upgrade):
        def handler(server, conn):
            upgrade(conn)
            server.read_frames(conn, 3)

        server = Server(handler)
        ws = wsclient.connect(server.url, {})
        ws.send_binary(b"\x01\x02" * 100)
        ws.send_binary(b"\x03\x04" * 100)
        ws.send_text('{"type":"CloseStream"}')
        server.stop()
        ws.close()
        assert server.frames == [
            (wsclient.OP_BINARY, b"\x01\x02" * 100),
            (wsclient.OP_BINARY, b"\x03\x04" * 100),
            (wsclient.OP_TEXT, b'{"type":"CloseStream"}'),
        ]

    def test_a_fragmented_message_is_one_message(self, upgrade):
        def handler(server, conn):
            upgrade(conn)
            conn.sendall(bytes([wsclient.OP_TEXT, 5]) + b'{"a":')  # FIN clear: a fragment
            conn.sendall(bytes([0x80 | wsclient.OP_CONT, 2]) + b"1}")

        server = Server(handler)
        ws = wsclient.connect(server.url, {})
        frames: list[tuple[int, bytes]] = []
        while not frames:
            frames = ws.poll(5.0)
        assert frames == [(wsclient.OP_TEXT, b'{"a":1}')]
        ws.close()
        server.stop()

    def test_a_ping_is_answered_with_a_pong_and_never_surfaces(self, upgrade):
        def handler(server, conn):
            upgrade(conn)
            conn.sendall(server_frame(wsclient.OP_PING, b"still there?"))
            server.read_frames(conn, 1)
            conn.sendall(server_frame(wsclient.OP_PONG, b"unsolicited"))
            conn.sendall(server_frame(wsclient.OP_TEXT, b"after"))

        server = Server(handler)
        ws = wsclient.connect(server.url, {})
        seen: list[tuple[int, bytes]] = []
        while (wsclient.OP_TEXT, b"after") not in seen:
            seen += ws.poll(5.0)
        assert seen == [(wsclient.OP_TEXT, b"after")]  # neither control frame reached the caller
        server.stop()
        assert server.frames == [(wsclient.OP_PONG, b"still there?")]
        ws.close()

    def test_a_close_frame_is_reported_and_ends_the_connection(self, upgrade):
        def handler(server, conn):
            upgrade(conn)
            conn.sendall(server_frame(wsclient.OP_TEXT, b"last words"))
            conn.sendall(server_frame(wsclient.OP_CLOSE, struct.pack("!H", 1000)))

        server = Server(handler)
        ws = wsclient.connect(server.url, {})
        seen: list[tuple[int, bytes]] = []
        while not any(opcode == wsclient.OP_CLOSE for opcode, _ in seen):
            seen += ws.poll(5.0)
        assert seen[0] == (wsclient.OP_TEXT, b"last words")  # what arrived before it still counts
        assert ws.closed is True
        with pytest.raises(wsclient.WebSocketError, match="closed"):
            ws.send_binary(b"too late")
        server.stop()

    def test_a_quiet_peer_polls_to_an_empty_list_rather_than_blocking(self, upgrade):
        """The property the whole audio loop rests on: reading must not stall the microphone."""

        def handler(server, conn):
            upgrade(conn)
            server.read_frames(conn, 1)

        server = Server(handler)
        ws = wsclient.connect(server.url, {})
        assert ws.poll(0.05) == []
        ws.send_binary(b"still sending while nothing comes back")
        server.stop()
        ws.close()

    def test_a_peer_that_vanishes_is_an_error_not_a_silence(self, upgrade):
        def handler(server, conn):
            upgrade(conn)
            conn.close()

        server = Server(handler)
        ws = wsclient.connect(server.url, {})
        with pytest.raises(wsclient.WebSocketError, match="closed the connection"):
            for _ in range(50):
                ws.poll(0.1)
        server.stop()

    def test_a_declared_length_past_the_ceiling_is_refused_before_it_is_read(self, upgrade):
        """The allocation bound. The frame header CLAIMS four gigabytes; not one byte of it is
        waited for, because a bound applied after the read is not a bound."""

        def handler(server, conn):
            upgrade(conn)
            conn.sendall(bytes([0x80 | wsclient.OP_BINARY, 127]) + struct.pack("!Q", 1 << 32))

        server = Server(handler)
        ws = wsclient.connect(server.url, {})
        with pytest.raises(wsclient.WebSocketError, match="ceiling"):
            for _ in range(50):
                ws.poll(0.1)
        server.stop()

    def test_a_fragmented_message_past_the_ceiling_is_refused_mid_reassembly(self, upgrade):
        """The bound MAX_FRAME_BYTES cannot give. It limits ONE frame; a message may be split over
        as many continuation frames as a peer cares to send, so without a total the accumulator is
        the whole memory of the desktop — megabyte fragments for as long as the dictation lasts."""

        def handler(server, conn):
            upgrade(conn)
            half = wsclient.MAX_MESSAGE_BYTES // 2 + 1024
            head = bytearray([wsclient.OP_TEXT, 127]) + struct.pack("!Q", half)  # FIN clear: a fragment
            conn.sendall(bytes(head) + b"a" * half)
            head = bytearray([wsclient.OP_CONT, 127]) + struct.pack("!Q", half)  # and the rest of it
            conn.sendall(bytes(head) + b"b" * half)

        server = Server(handler)
        ws = wsclient.connect(server.url, {})
        with pytest.raises(wsclient.WebSocketError, match="ceiling"):
            for _ in range(200):
                ws.poll(0.1)
        server.stop()

    def test_a_reserved_opcode_fails_the_connection_instead_of_being_guessed(self, upgrade):
        """RFC 6455 §5.2. Read as a data frame — which is what an `else` branch does — a reserved
        opcode opens a message that no FIN of ours ever closes."""

        def handler(server, conn):
            upgrade(conn)
            conn.sendall(server_frame(0x3, b"reserved"))

        server = Server(handler)
        ws = wsclient.connect(server.url, {})
        with pytest.raises(wsclient.WebSocketError, match="reserved"):
            for _ in range(50):
                ws.poll(0.1)
        server.stop()

    @pytest.mark.parametrize("frame", ["oversized", "fragmented"])
    def test_a_control_frame_that_breaks_its_own_rules_is_refused(self, upgrade, frame):
        """RFC 6455 §5.5: a control frame is at most 125 bytes and never fragmented. The teeth are
        the ping path — a 1 MiB "ping" is echoed back as a pong on the very thread pumping the
        microphone, so a peer that sends them chooses how much of our uplink to burn. Refused from
        the HEADER, so the payload is never even read."""

        def handler(server, conn):
            upgrade(conn)
            if frame == "oversized":
                body = b"x" * 200
                conn.sendall(bytes([0x80 | wsclient.OP_PING, 126]) + struct.pack("!H", len(body)) + body)
            else:
                conn.sendall(bytes([wsclient.OP_PING, 4]) + b"ping")  # FIN clear

        server = Server(handler)
        ws = wsclient.connect(server.url, {})
        with pytest.raises(wsclient.WebSocketError, match="control frame"):
            for _ in range(50):
                ws.poll(0.1)
        server.stop()
        assert server.frames == []  # and no pong was ever sent back

    def test_a_masked_server_frame_is_refused(self, upgrade):
        def handler(server, conn):
            upgrade(conn)
            conn.sendall(wsclient.build_frame(wsclient.OP_TEXT, b"masked", b"\x01\x02\x03\x04"))

        server = Server(handler)
        ws = wsclient.connect(server.url, {})
        with pytest.raises(wsclient.WebSocketError, match="masked"):
            for _ in range(50):
                ws.poll(0.1)
        server.stop()

    def test_a_reserved_bit_is_refused_because_we_negotiated_no_extensions(self, upgrade):
        def handler(server, conn):
            upgrade(conn)
            conn.sendall(bytes([0xC0 | wsclient.OP_TEXT, 2]) + b"hi")  # RSV1 set

        server = Server(handler)
        ws = wsclient.connect(server.url, {})
        with pytest.raises(wsclient.WebSocketError, match="reserved bit"):
            for _ in range(50):
                ws.poll(0.1)
        server.stop()

    def test_a_continuation_with_nothing_to_continue_is_refused(self, upgrade):
        def handler(server, conn):
            upgrade(conn)
            conn.sendall(server_frame(wsclient.OP_CONT, b"orphan"))

        server = Server(handler)
        ws = wsclient.connect(server.url, {})
        with pytest.raises(wsclient.WebSocketError, match="continuation"):
            for _ in range(50):
                ws.poll(0.1)
        server.stop()

    def test_a_new_data_frame_while_one_is_unfinished_is_refused(self, upgrade):
        def handler(server, conn):
            upgrade(conn)
            conn.sendall(bytes([wsclient.OP_TEXT, 2]) + b"ab")  # FIN clear
            conn.sendall(server_frame(wsclient.OP_TEXT, b"interleaved"))

        server = Server(handler)
        ws = wsclient.connect(server.url, {})
        with pytest.raises(wsclient.WebSocketError, match="unfinished"):
            for _ in range(50):
                ws.poll(0.1)
        server.stop()

    def test_closing_twice_and_closing_a_dead_socket_never_raise(self, upgrade):
        """close() is what a caller runs on the way OUT of a failure; a second failure there helps
        nobody, so it is the one method in the module that cannot raise."""

        def handler(server, conn):
            upgrade(conn)
            server.read_frames(conn, 1)

        server = Server(handler)
        ws = wsclient.connect(server.url, {})
        ws.close()
        ws.close()
        server.stop()


class TestTheMessagesAProviderSends:
    """One shape-level check that the client hands back what a parser can read — the seam where
    this module ends and the provider registry begins."""

    def test_a_json_results_message_survives_the_socket_verbatim(self, upgrade):
        message = {
            "type": "Results",
            "is_final": True,
            "channel": {"alternatives": [{"transcript": "привет, это диктовка"}]},
        }

        def handler(server, conn):
            upgrade(conn)
            conn.sendall(server_frame(wsclient.OP_TEXT, json.dumps(message).encode("utf-8")))

        server = Server(handler)
        ws = wsclient.connect(server.url, {})
        frames: list[tuple[int, bytes]] = []
        while not frames:
            frames = ws.poll(5.0)
        assert json.loads(frames[0][1]) == message
        ws.close()
        server.stop()


class TestTls:
    """What a `wss` connection is wrapped in. No socket here — the context IS the assertion."""

    def test_the_default_context_floors_at_tls_1_2(self):
        """CodeQL's py/insecure-default-protocol, answered rather than exempted (windowsill#99
        review): a default context's protocol range follows the linked OpenSSL build and its
        system policy, so "we use the defaults" is not the same statement as "we never negotiate
        TLS 1.0". Dictation audio is exactly the payload that must not travel over one."""
        context = wsclient.tls_context()
        assert context.minimum_version >= ssl.TLSVersion.TLSv1_2

    def test_verification_and_hostname_checking_are_on_and_have_no_switch(self):
        context = wsclient.tls_context()
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.check_hostname is True
        source = _WSCLIENT_PATH.read_text(encoding="utf-8")
        for escape in ("CERT_NONE", "check_hostname = False", "_create_unverified_context"):
            assert escape not in source, f"{escape} is a way out of TLS that must not exist here"

    def test_a_wss_url_goes_through_that_context(self, monkeypatch):
        """The wiring, without a certificate authority: the connect path must reach tls_context()
        for a wss URL, and a caller-supplied context (the test seam) must be honoured as given."""
        wrapped: list[str] = []

        class _Ctx:
            def wrap_socket(self, sock, server_hostname):
                wrapped.append(server_hostname)
                raise ssl.SSLError("no real handshake in a unit test")

        def connector(address, timeout):
            return socket.socket()

        with pytest.raises(wsclient.WebSocketError, match="TLS failed"):
            wsclient.connect("wss://api.example/v1/listen", {}, connector=connector, context=_Ctx())
        assert wrapped == ["api.example"]

        real = wsclient.tls_context
        made: list[ssl.SSLContext] = []

        def spy() -> ssl.SSLContext:
            made.append(real())
            return made[-1]

        monkeypatch.setattr(wsclient, "tls_context", spy)
        with pytest.raises(wsclient.WebSocketError):
            wsclient.connect("wss://127.0.0.1:1/v1/listen", {}, connector=connector, timeout=1.0)
        assert made, "a wss URL did not reach tls_context()"
        assert made[0].minimum_version >= ssl.TLSVersion.TLSv1_2  # and it is the floored one
