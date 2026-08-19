"""Minimal RFC 6455 WebSocket server implementation (stdlib only).

Covers exactly what the DSH Web tunnel needs: the server-side handshake,
masked client frames, fragmented text/binary messages, ping/pong, close
handshake, and a payload size bound. It deliberately does not implement
compression, extensions, or subprotocol negotiation.
"""

from __future__ import annotations

import base64
import hashlib
import struct
import threading
from typing import Any

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

MAX_FRAME_PAYLOAD = 16 * 1024 * 1024  # 16 MiB


class WebSocketProtocolError(Exception):
    """Malformed or out-of-contract WebSocket traffic."""


def accept_key(key: str) -> str:
    """RFC 6455 Sec-WebSocket-Accept for a client-provided key."""
    digest = hashlib.sha1((key + WS_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


class WebSocket:
    """Server-side WebSocket over file-like reader/writer streams.

    The reader/writer are the HTTP handler's rfile/wfile so that any bytes the
    client pipelined behind the handshake remain available.
    """

    def __init__(
        self,
        reader: Any,
        writer: Any,
        *,
        max_payload: int = MAX_FRAME_PAYLOAD,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._max_payload = max_payload
        self._closed = False
        # Browser and device pumps may write concurrently; preserve frame
        # boundaries and prevent a close racing a response frame.
        self._write_lock = threading.RLock()

    # -- outgoing ---------------------------------------------------------

    def send_text(self, text: str) -> None:
        self._send_frame(0x1, text.encode("utf-8"))

    def send_bytes(self, data: bytes) -> None:
        self._send_frame(0x2, data)

    def send_json(self, payload: dict[str, Any]) -> None:
        import json

        self.send_text(json.dumps(payload, ensure_ascii=False))

    def ping(self) -> None:
        self._send_frame(0x9, b"")

    def close(self, code: int = 1000, reason: bytes = b"") -> None:
        if self._closed:
            return
        try:
            payload = struct.pack("!H", code) + reason[:123]
            with self._write_lock:
                self._send_frame_unchecked(0x8, payload)
        except OSError:
            pass
        self._closed = True
        try:
            self._writer.flush()
        except OSError:
            pass

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        if self._closed:
            raise WebSocketProtocolError("connection closed")
        with self._write_lock:
            self._send_frame_unchecked(opcode, payload)

    def _send_frame_unchecked(self, opcode: int, payload: bytes) -> None:
        if self._closed:
            raise WebSocketProtocolError("connection closed")
        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header.append(length)
        elif length < 65536:
            header.append(126)
            header += struct.pack("!H", length)
        else:
            header.append(127)
            header += struct.pack("!Q", length)
        self._writer.write(bytes(header) + payload)
        self._writer.flush()

    # -- incoming ---------------------------------------------------------

    def recv_message(self) -> tuple[int, bytes]:
        """Return (opcode, payload) of the next complete message.

        Control frames (ping/pong/close) are handled internally: pings are
        answered with pongs, pongs are dropped, and a close frame raises
        WebSocketProtocolError after replying. Blocking behavior follows the
        underlying reader (the caller controls socket timeouts).
        """
        if self._closed:
            raise WebSocketProtocolError("connection closed")
        opcode: int | None = None
        message = bytearray()
        while True:
            first, second = self._read_exact(2)
            fin = bool(first & 0x80)
            op = first & 0x0F
            masked = bool(second & 0x80)
            if not masked:
                raise WebSocketProtocolError("client frame must be masked")
            length = second & 0x7F
            if op in (0x8, 0x9, 0xA):
                if not fin:
                    raise WebSocketProtocolError("control frame must not be fragmented")
                if length > 125:
                    raise WebSocketProtocolError("control frame payload too large")
            if length == 126:
                length = struct.unpack("!H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read_exact(8))[0]
            if length > self._max_payload:
                raise WebSocketProtocolError("frame payload too large")
            mask = self._read_exact(4) if masked else None
            payload = bytearray(self._read_exact(length))
            if mask is not None:
                for index in range(length):
                    payload[index] ^= mask[index % 4]
            if op == 0x8:  # close
                code = 1000
                if len(payload) >= 2:
                    code = struct.unpack("!H", bytes(payload[:2]))[0]
                self.close(code)
                raise WebSocketProtocolError(f"closed by peer {code}")
            if op == 0x9:  # ping -> pong
                self._send_frame(0xA, bytes(payload))
                continue
            if op == 0xA:  # pong
                continue
            if op in (0x0, 0x1, 0x2):
                if op != 0:
                    opcode = op
                message += payload
                if fin:
                    if opcode is None:
                        raise WebSocketProtocolError("fragmented message without opcode")
                    return opcode, bytes(message)
            else:
                raise WebSocketProtocolError(f"unsupported opcode {op}")

    def _read_exact(self, count: int) -> bytes:
        data = self._reader.read(count)
        if len(data) != count:
            raise WebSocketProtocolError("unexpected end of stream")
        return data
