"""CDP client against a socketpair fake — no browser, no network."""

import base64
import hashlib
import json
import socket
import struct
import threading
import unittest

from dispatcher import cdp


def _frame_server(text: str) -> bytes:
    data = text.encode()
    return bytes([0x81, len(data)]) + data


class FakeCDP(threading.Thread):
    """Speaks upgrade + one Runtime.evaluate round-trip, records the call."""

    def __init__(self, result="page text here", error=None):
        super().__init__(daemon=True)
        self.result, self.error = result, error
        self.srv, self.cli = socket.socketpair()
        self.seen_method = None
        self.exc = None

    def run(self):
        try:
            req = b""
            while b"\r\n\r\n" not in req:
                chunk = self.srv.recv(4096)
                if not chunk:
                    return
                req += chunk
            key = [l for l in req.decode("latin1").split("\r\n")
                   if l.lower().startswith("sec-websocket-key:")][0].split(":", 1)[1].strip()
            accept = base64.b64encode(
                hashlib.sha1((key + cdp.GUID).encode()).digest()).decode()
            self.srv.sendall(
                f"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n"
                .encode())
            # read the masked call frame (short form is all the client sends
            # for these sizes in practice; parse generally)
            hdr = self._recvn(2)
            ln = hdr[1] & 0x7F
            if ln == 126:
                ln = struct.unpack(">H", self._recvn(2))[0]
            mask = self._recvn(4)
            raw = self._recvn(ln)
            call = json.loads(bytes(b ^ mask[i % 4]
                                    for i, b in enumerate(raw)).decode())
            self.seen_method = call["method"]
            if self.error:
                reply = {"id": call["id"], "error": {"message": self.error}}
            else:
                reply = {"id": call["id"],
                         "result": {"result": {"value": self.result}}}
            self.srv.sendall(_frame_server(json.dumps(reply)))
        except Exception as e:  # noqa: BLE001 — surfaced below
            self.exc = e

    def _recvn(self, n):
        out = bytearray()
        while len(out) < n:
            chunk = self.srv.recv(n - len(out))
            if not chunk:
                raise ConnectionError("client went away")
            out += chunk
        return bytes(out)


class TestCDPClient(unittest.TestCase):
    def _client(self, fake):
        c = cdp.CDPClient(timeout_s=5)
        # bypass TCP: hand over the paired socket post-handshake shape
        c.sock = fake.cli
        return c

    def test_evaluate_round_trip(self):
        fake = FakeCDP(result="hello page")
        fake.start()
        import time
        # wait for the fake to be listening on its end is unnecessary with
        # socketpair; run the client side of the handshake inline:
        c = self._client(fake)
        # perform handshake over the paired socket
        import os
        key = base64.b64encode(os.urandom(16)).decode()
        fake.cli.sendall(
            f"GET /devtools/page/x HTTP/1.1\r\nHost: h\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n\r\n".encode())
        head = b""
        while b"\r\n\r\n" not in head:
            head += fake.cli.recv(1)
        self.assertIn(b" 101 ", head)
        self.assertEqual(c.evaluate("document.title"), "hello page")
        self.assertEqual(fake.seen_method, "Runtime.evaluate")
        fake.join(timeout=5)
        self.assertIsNone(fake.exc)
        c.close()

    def test_method_error_raises(self):
        fake = FakeCDP(error="No such target")
        fake.start()
        c = self._client(fake)
        import os
        key = base64.b64encode(os.urandom(16)).decode()
        fake.cli.sendall(
            f"GET /x HTTP/1.1\r\nHost: h\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n\r\n".encode())
        head = b""
        while b"\r\n\r\n" not in head:
            head += fake.cli.recv(1)
        with self.assertRaises(cdp.CDPError):
            c.evaluate("x")
        fake.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
