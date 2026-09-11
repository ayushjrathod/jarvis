"""Minimal DevTools-protocol client over stdlib sockets.

Reading page text needs Runtime.evaluate, which only speaks WebSocket —
and the stack has no WS client (httpx does plain HTTP). Rather than a
dependency for one call, this is the ~100 lines that matter: TCP connect,
HTTP upgrade handshake, masked client text frames, unmasked server frames
(up to 2^63 with 16/64-bit lengths, close frames, ping/pong answered),
request/response matched by id. Blocking, with a deadline; the service
calls it in a thread like every other blocking I/O here.

Server masking is not required by the RFC and not implemented; extensions
are not negotiated. Good enough for Runtime.evaluate against a local
chromium, which is the only peer this ever talks to.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import select
import socket
import struct
import time

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
DEFAULT_TIMEOUT_S = 15.0


class CDPError(Exception):
    pass


def _recv_exact(sock: socket.socket, n: int, deadline: float) -> bytes:
    out = bytearray()
    while len(out) < n:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CDPError("timed out reading from browser")
        r, _, _ = select.select([sock], [], [], remaining)
        if not r:
            raise CDPError("timed out reading from browser")
        chunk = sock.recv(n - len(out))
        if not chunk:
            raise CDPError("browser closed the connection")
        out += chunk
    return bytes(out)


class CDPClient:
    """One WebSocket conversation: connect, evaluate, close."""

    def __init__(self, timeout_s: float = DEFAULT_TIMEOUT_S):
        self.timeout_s = timeout_s
        self.sock: socket.socket | None = None
        self._next_id = 0

    def connect(self, ws_url: str):
        """ws_url like ws://127.0.0.1:9333/devtools/page/<id>."""
        from urllib.parse import urlsplit
        parts = urlsplit(ws_url)
        if parts.scheme not in ("ws", "wss"):
            raise CDPError(f"not a websocket URL: {ws_url[:40]}")
        if parts.scheme == "wss":
            raise CDPError("local browsers speak plain ws")
        host, port = (parts.hostname or "127.0.0.1"), parts.port or 80
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise CDPError("local browsers only")
        key = base64.b64encode(os.urandom(16)).decode()
        sock = socket.create_connection((host, port), timeout=self.timeout_s)
        try:
            path = parts.path or "/"
            if parts.query:
                path += "?" + parts.query
            sock.sendall(
                f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
                .encode())
            head = b""
            deadline = time.monotonic() + self.timeout_s
            while b"\r\n\r\n" not in head:
                head += _recv_exact(sock, 1, deadline)
            status = head.split(b"\r\n", 1)[0]
            if b" 101 " not in status:
                raise CDPError(f"upgrade refused: {status[:60]!r}")
            accept = hashlib.sha1((key + GUID).encode()).digest()
            if base64.b64encode(accept).decode() not in head.decode("latin1"):
                raise CDPError("bad Sec-WebSocket-Accept")
        except BaseException:
            sock.close()
            raise
        self.sock = sock
        return self

    def close(self):
        sock, self.sock = self.sock, None
        if sock is not None:
            try:
                # close frame, masked like every client frame
                mask = os.urandom(4)
                sock.sendall(b"\x88\x80" + mask)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _send_text(self, payload: str):
        data = payload.encode()
        mask = os.urandom(4)
        head = bytes([0x81])
        n = len(data)
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", n)
        self.sock.sendall(head + mask + bytes(b ^ mask[i % 4]
                                              for i, b in enumerate(data)))

    def _recv_text(self, deadline: float) -> str:
        while True:
            hdr = _recv_exact(self.sock, 2, deadline)
            fin, masked, ln = hdr[0] & 0x80, hdr[1] & 0x80, hdr[1] & 0x7F
            if ln == 126:
                ln = struct.unpack(">H", _recv_exact(self.sock, 2, deadline))[0]
            elif ln == 127:
                ln = struct.unpack(">Q", _recv_exact(self.sock, 8, deadline))[0]
            if masked:
                mask = _recv_exact(self.sock, 4, deadline)
            data = _recv_exact(self.sock, ln, deadline)
            if masked:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            opcode = hdr[0] & 0x0F
            if opcode == 0x8:  # close
                raise CDPError("browser closed the conversation")
            if opcode == 0x9:  # ping -> pong
                self.sock.sendall(b"\x8A\x00")
                continue
            if opcode == 0xA:  # pong
                continue
            if opcode != 0x1:  # text only; no continuation support
                raise CDPError(f"unexpected websocket opcode {opcode:#x}")
            if not fin:
                raise CDPError("fragmented messages unsupported")
            return data.decode("utf-8", "replace")

    def call(self, method: str, params: dict | None = None) -> dict:
        """One CDP round-trip. Raises CDPError on protocol or method errors."""
        if self.sock is None:
            raise CDPError("not connected")
        self._next_id += 1
        rid = self._next_id
        deadline = time.monotonic() + self.timeout_s
        self._send_text(json.dumps({"id": rid, "method": method,
                                    "params": params or {}}))
        while True:
            msg = json.loads(self._recv_text(deadline))
            if msg.get("id") != rid:
                continue  # session events land here; the call owns the line
            if "error" in msg:
                raise CDPError(f"{method}: {msg['error'].get('message', msg['error'])}")
            return msg.get("result", {})

    def evaluate(self, expression: str) -> str:
        """JS expression -> its JSON value rendered as text."""
        res = self.call("Runtime.evaluate",
                        {"expression": expression, "returnByValue": True})
        if res.get("exceptionDetails"):
            raise CDPError("page threw while evaluating")
        value = (res.get("result") or {}).get("value")
        if value is None:
            return ""
        return value if isinstance(value, str) else json.dumps(value)
