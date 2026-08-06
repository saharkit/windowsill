#!/usr/bin/env python3
"""voice-loop — a minimal RFC 6455 websocket client, stdlib only.

Why this file exists at all. Everything under ``scripts/`` is **stdlib-only Python 3.10+** by
design: the hook and hotkey half of this plugin installs by being copied, with no pip step, no
virtualenv and no version solving — that property is what makes ``/voice-setup`` a skill rather
than a page of instructions. Streaming dictation (windowsill#99) needs a websocket, and the stdlib
has no client for one. Taking a dependency would have broken the install story for every user to
give one optional provider a socket; so the socket is written here instead, scoped to exactly the
subset of RFC 6455 a client needs to talk to a speech API:

* the opening handshake over ``socket``/``ssl``, with ``Sec-WebSocket-Accept`` VERIFIED (a server
  that does not answer the challenge is not a websocket server, and continuing would frame binary
  audio into whatever it actually is);
* client→server frames, always MASKED with a fresh ``os.urandom`` key, as the RFC requires;
* server→client frames, which must NOT be masked and are refused if they are;
* fragmentation (continuation frames), ping (answered with a pong), pong (ignored), close;
* nothing else. No extensions, no permessage-deflate, no autobahn-grade edge cases, no server side.

**The frame stream is an untrusted input boundary** and is read like one: the payload length is
checked against ``MAX_FRAME_BYTES`` BEFORE a byte is buffered for it, the handshake response is
bounded by ``MAX_HANDSHAKE_BYTES``, reserved bits and masked server frames are refused outright,
and every failure — protocol, transport, TLS, DNS — arrives as one exception type
(``WebSocketError``) so a caller degrades on a single ``except`` instead of enumerating the
socket module.

Nothing here knows what a transcript is. Sockets and frames only; the provider registry
(``providers.py``) owns the URL, the auth header and the message shapes, and ``dictate.py`` owns
the decision to use any of it.
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import ssl
import struct
import urllib.parse

# The RFC's magic string: the server echoes sha1(key + GUID) so a client can tell a real websocket
# handshake from a cache, a proxy, or an HTTP endpoint that happens to answer 101.
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

DEFAULT_PORTS = {"ws": 80, "wss": 443}

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

# A speech API's messages are small JSON documents; a megabyte is already three orders of magnitude
# more than any of them and small enough that a hostile (or broken) peer cannot make us allocate our
# way out of memory. The length is refused from the HEADER, before the payload is read.
MAX_FRAME_BYTES = 1 << 20

# The handshake response is a few hundred bytes of headers. A peer that keeps sending them without
# ever ending them is a peer we stop reading from.
MAX_HANDSHAKE_BYTES = 16384

# One recv() call's ceiling — plain buffering, not a limit on anything the caller can observe.
_READ_SIZE = 65536


class WebSocketError(OSError):
    """Every way this client can fail, under one name.

    A caller's response to all of them is identical — log the reason and degrade to the path that
    does not need a socket — so distinguishing "DNS did not resolve" from "the peer masked a
    frame" at the type level would buy nobody anything. The MESSAGE carries the distinction.
    """


def _accept_token(key: str) -> str:
    """The value the server must answer ``Sec-WebSocket-Key`` with."""
    return base64.b64encode(hashlib.sha1(f"{key}{GUID}".encode()).digest()).decode("ascii")


def _mask(payload: bytes, key: bytes) -> bytes:
    """XOR a payload with the 4-byte masking key — the same operation both directions of the RFC
    define, kept as one function so the client's mask and a test's unmask cannot drift apart."""
    return bytes(byte ^ key[index % 4] for index, byte in enumerate(payload))


def build_frame(opcode: int, payload: bytes, mask_key: bytes | None) -> bytes:
    """One complete, unfragmented frame. ``mask_key`` is required for client→server frames and is
    None only for the server direction (which this module builds nowhere but tests do)."""
    head = bytearray([0x80 | opcode])
    length = len(payload)
    flag = 0x80 if mask_key else 0x00
    if length < 126:
        head.append(flag | length)
    elif length < (1 << 16):
        head.append(flag | 126)
        head += struct.pack("!H", length)
    else:
        head.append(flag | 127)
        head += struct.pack("!Q", length)
    if mask_key:
        head += mask_key
        payload = _mask(payload, mask_key)
    return bytes(head) + payload


class WebSocket:
    """One open connection: send frames, poll for whole ones, close.

    The reader is buffered rather than blocking-per-frame on purpose. Audio goes OUT on the same
    socket that results come IN on, from one thread, so a read that blocks until a frame is
    complete would stall the microphone behind the network. ``poll`` therefore reads whatever has
    arrived, keeps a partial frame in the buffer, and returns only the frames that are whole.
    """

    def __init__(self, sock, leftover: bytes = b"") -> None:
        self._sock = sock
        self._buf = bytearray(leftover)
        self._fragment: tuple[int, bytearray] | None = None
        self.closed = False

    # --- sending ---------------------------------------------------------------------------

    def send_binary(self, payload: bytes) -> None:
        """One binary frame — the audio direction."""
        self._send(OP_BINARY, payload)

    def send_text(self, text: str) -> None:
        """One text frame — the control-message direction (CloseStream, KeepAlive)."""
        self._send(OP_TEXT, text.encode("utf-8"))

    def _send(self, opcode: int, payload: bytes) -> None:
        if self.closed:
            raise WebSocketError("the connection is closed")
        try:
            self._sock.sendall(build_frame(opcode, payload, os.urandom(4)))
        except OSError as err:
            self.closed = True
            raise WebSocketError(f"send failed: {err}") from err

    # --- receiving -------------------------------------------------------------------------

    def poll(self, timeout: float) -> list[tuple[int, bytes]]:
        """Every COMPLETE frame available within ``timeout``, as ``(opcode, payload)`` pairs.

        An empty list means "nothing whole arrived in that window", which is the normal answer
        while the speaker is still speaking. Pings are answered here and never returned; pongs are
        dropped; a close frame IS returned (and marks the connection closed) because the caller
        wants to know the peer said goodbye rather than the socket dying under it.
        """
        frames = self._drain_buffer()
        if frames:
            return frames
        if self._fill(timeout):
            return self._drain_buffer()
        return []

    def _fill(self, timeout: float) -> bool:
        """One read into the buffer. False when nothing was waiting — the peer is simply quiet.

        A timeout of 0.0 is a genuine non-blocking peek (that is what ``settimeout(0)`` means), and
        the audio loop uses it on every pass that had a chunk to send: "is there anything for me?"
        must never be allowed to stall the microphone. Its "nothing yet" arrives as
        BlockingIOError rather than as a timeout, and both mean exactly the same thing here.
        """
        if self.closed:
            return False
        try:
            self._sock.settimeout(max(0.0, timeout))
            chunk = self._sock.recv(_READ_SIZE)
        except (socket.timeout, TimeoutError, BlockingIOError, ssl.SSLWantReadError):
            return False
        except OSError as err:
            self.closed = True
            raise WebSocketError(f"read failed: {err}") from err
        if not chunk:
            self.closed = True
            raise WebSocketError("the peer closed the connection without a close frame")
        self._buf += chunk
        return True

    def _drain_buffer(self) -> list[tuple[int, bytes]]:
        out: list[tuple[int, bytes]] = []
        while True:
            frame = self._take_frame()
            if frame is None:
                return out
            fin, opcode, payload = frame
            if opcode == OP_PING:
                self._send(OP_PONG, payload)
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                self.closed = True
                out.append((OP_CLOSE, payload))
                continue
            if opcode == OP_CONT:
                if self._fragment is None:
                    raise WebSocketError("a continuation frame with nothing to continue")
                self._fragment[1].extend(payload)
            else:
                if self._fragment is not None:
                    raise WebSocketError("a new data frame arrived while another was unfinished")
                self._fragment = (opcode, bytearray(payload))
            if fin:
                done_opcode, body = self._fragment
                self._fragment = None
                out.append((done_opcode, bytes(body)))

    def _take_frame(self) -> tuple[bool, int, bytes] | None:
        """The next whole frame in the buffer, or None while it is still arriving."""
        buf = self._buf
        if len(buf) < 2:
            return None
        first, second = buf[0], buf[1]
        if first & 0x70:
            raise WebSocketError("a frame set a reserved bit — this client negotiates no extensions")
        fin = bool(first & 0x80)
        opcode = first & 0x0F
        if second & 0x80:
            raise WebSocketError("the server masked a frame, which the RFC forbids")
        length = second & 0x7F
        offset = 2
        if length == 126:
            if len(buf) < 4:
                return None
            length = struct.unpack_from("!H", buf, 2)[0]
            offset = 4
        elif length == 127:
            if len(buf) < 10:
                return None
            length = struct.unpack_from("!Q", buf, 2)[0]
            offset = 10
        # Checked from the HEADER, before a byte of the payload is waited for or kept: this is the
        # allocation bound, and a bound applied after the read would not be one.
        if length > MAX_FRAME_BYTES:
            raise WebSocketError(f"a {length}-byte frame is past the {MAX_FRAME_BYTES}-byte ceiling")
        if len(buf) < offset + length:
            return None
        payload = bytes(buf[offset : offset + length])
        del buf[: offset + length]
        return fin, opcode, payload

    # --- closing ---------------------------------------------------------------------------

    def close(self) -> None:
        """Say goodbye if we still can, then drop the socket. Never raises: closing is what a
        caller does on the way out of a failure, and a second failure there helps nobody."""
        try:
            if not self.closed:
                self._sock.sendall(build_frame(OP_CLOSE, struct.pack("!H", 1000), os.urandom(4)))
        except OSError:
            pass
        self.closed = True
        try:
            self._sock.close()
        except OSError:
            pass


def _read_handshake(sock, timeout: float) -> tuple[bytes, bytes]:
    """The response headers and whatever frame bytes arrived glued to them."""
    buf = bytearray()
    sock.settimeout(timeout)
    while b"\r\n\r\n" not in buf:
        if len(buf) > MAX_HANDSHAKE_BYTES:
            raise WebSocketError("the handshake response never ended")
        try:
            chunk = sock.recv(_READ_SIZE)
        except (socket.timeout, TimeoutError) as err:
            raise WebSocketError("the handshake timed out") from err
        except OSError as err:
            raise WebSocketError(f"the handshake failed: {err}") from err
        if not chunk:
            raise WebSocketError("the peer closed the connection during the handshake")
        buf += chunk
    head, _, rest = bytes(buf).partition(b"\r\n\r\n")
    return head, rest


def _check_handshake(head: bytes, key: str) -> None:
    """101 with the right accept token, or a refusal that names what came back instead."""
    lines = head.decode("utf-8", "replace").split("\r\n")
    status = lines[0] if lines else ""
    if " 101" not in status:
        # The status line only. A 4xx body from a speech API is where the account/quota detail
        # lives, but it is also where a request echo could carry a key, and this string reaches a
        # log — so the refusal is named by its status and nothing more.
        raise WebSocketError(f"the server refused the upgrade: {status.strip()[:120]}")
    headers = {}
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if sep:
            headers[name.strip().lower()] = value.strip()
    if headers.get("upgrade", "").lower() != "websocket":
        raise WebSocketError("the 101 response did not upgrade to websocket")
    if headers.get("sec-websocket-accept", "") != _accept_token(key):
        raise WebSocketError("the server's Sec-WebSocket-Accept did not answer our key")


def connect(
    url: str,
    headers: dict[str, str] | None = None,
    *,
    timeout: float = 10.0,
    connector=socket.create_connection,
    context: ssl.SSLContext | None = None,
) -> WebSocket:
    """Open a websocket. Raises WebSocketError, and nothing else, on every failure.

    ``connector`` is the one seam a test needs (it hands back a connected socket), and ``wss``
    wraps whatever it returns in TLS with ``tls_context()`` — certificate verification and
    hostname checking on, a TLS 1.2 floor, and no switch anywhere to turn any of it off.
    """
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in DEFAULT_PORTS:
        raise WebSocketError(f"not a websocket URL: {parts.scheme or url[:40]!r}")
    host = parts.hostname or ""
    if not host:
        raise WebSocketError(f"the websocket URL names no host: {url[:60]!r}")
    port = parts.port or DEFAULT_PORTS[parts.scheme]
    target = parts.path or "/"
    if parts.query:
        target = f"{target}?{parts.query}"

    try:
        sock = connector((host, port), timeout)
    except OSError as err:
        raise WebSocketError(f"could not reach {host}:{port}: {err}") from err
    try:
        if parts.scheme == "wss":
            sock = (context or tls_context()).wrap_socket(sock, server_hostname=host)
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = [
            f"GET {target} HTTP/1.1",
            f"Host: {host}:{port}" if parts.port else f"Host: {host}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
        ]
        request += [f"{name}: {value}" for name, value in (headers or {}).items()]
        sock.sendall(("\r\n".join(request) + "\r\n\r\n").encode("utf-8"))
        head, leftover = _read_handshake(sock, timeout)
        _check_handshake(head, key)
    except WebSocketError:
        _quietly_close(sock)  # already named its own reason — do not re-wrap it
        raise
    except ssl.SSLError as err:
        _quietly_close(sock)
        raise WebSocketError(f"TLS failed for {host}: {err}") from err
    except OSError as err:
        _quietly_close(sock)
        raise WebSocketError(f"the handshake failed: {err}") from err
    return WebSocket(sock, leftover)


def _quietly_close(sock) -> None:
    try:
        sock.close()
    except OSError:
        pass


def tls_context() -> ssl.SSLContext:
    """The TLS context a ``wss`` connection gets: verification and hostname checking on (that is
    ``create_default_context``'s doing, and there is deliberately no switch here to turn either
    off), plus an EXPLICIT floor at TLS 1.2.

    The floor is stated rather than inherited. A default context's protocol range follows the
    linked OpenSSL build and its system policy — which is how a client that "uses the defaults"
    can still negotiate TLS 1.0 on an older or laxer host — and dictation audio is exactly the
    payload that must not travel over a protocol with known-broken ciphers. CodeQL's
    py/insecure-default-protocol reads the same line the same way (windowsill#99 review), and the
    honest answer to it is the floor, not an exemption.
    """
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context
