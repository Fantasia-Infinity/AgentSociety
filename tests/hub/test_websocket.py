from __future__ import annotations

import os
import socket
import struct
import unittest

from agent_hub.websocket import WebSocket, WebSocketProtocolError, accept_key


def masked_frame(opcode: int, payload: bytes, fin: bool = True) -> bytes:
    mask = b"\x11\x22\x33\x44"
    header = bytearray([(0x80 if fin else 0) | opcode, 0x80 | len(payload)])
    body = bytes(
        payload[index] ^ mask[index % 4] for index in range(len(payload))
    )
    return bytes(header) + mask + body


class AcceptKeyTests(unittest.TestCase):
    def test_rfc6455_example(self) -> None:
        # RFC 6455 section 1.3 example
        self.assertEqual(
            accept_key("dGhlIHNhbXBsZSBub25jZQ=="),
            "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=",
        )


class WebSocketTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server_sock, self.client_sock = socket.socketpair()
        self.server = WebSocket(self.server_sock.makefile("rb"), self.server_sock.makefile("wb"))
        self.client = self.client_sock

    def tearDown(self) -> None:
        for sock in (self.server_sock, self.client_sock):
            try:
                sock.close()
            except OSError:
                pass

    def _send_client_frame(self, opcode: int, payload: bytes, fin: bool = True) -> None:
        self.client.sendall(masked_frame(opcode, payload, fin))

    def test_receives_masked_text_frame(self) -> None:
        self._send_client_frame(0x1, b"hello")
        opcode, payload = self.server.recv_message()
        self.assertEqual(opcode, 0x1)
        self.assertEqual(payload, b"hello")

    def test_reassembles_fragmented_message(self) -> None:
        self._send_client_frame(0x1, b"hel", fin=False)
        self._send_client_frame(0x0, b"lo")
        opcode, payload = self.server.recv_message()
        self.assertEqual(opcode, 0x1)
        self.assertEqual(payload, b"hello")

    def test_rejects_unmasked_client_frame(self) -> None:
        self.client.sendall(b"\x81\x01x")
        with self.assertRaises(WebSocketProtocolError):
            self.server.recv_message()

    def test_rejects_fragmented_control_frame(self) -> None:
        self._send_client_frame(0x9, b"x", fin=False)
        with self.assertRaises(WebSocketProtocolError):
            self.server.recv_message()

    def test_rejects_oversized_control_frame(self) -> None:
        self._send_client_frame(0x9, b"x" * 126)
        with self.assertRaises(WebSocketProtocolError):
            self.server.recv_message()

    def test_answers_ping_with_pong(self) -> None:
        import threading

        result: list[tuple[int, bytes]] = []

        def server_loop() -> None:
            try:
                result.append(self.server.recv_message())
            except Exception as exc:  # noqa: BLE001
                result.append(exc)  # type: ignore[arg-type]

        thread = threading.Thread(target=server_loop, daemon=True)
        thread.start()
        self._send_client_frame(0x9, b"probe")
        first = self.client.recv(2)
        self.assertEqual(first[0] & 0x0F, 0xA)  # pong opcode
        length = first[1] & 0x7F
        payload = self.client.recv(length)
        self.assertEqual(payload, b"probe")
        # Let the server loop finish by sending a text message.
        self._send_client_frame(0x1, b"done")
        thread.join(timeout=5)
        self.assertEqual(result, [(0x1, b"done")])

    def test_close_frame_raises_after_reply(self) -> None:
        self._send_client_frame(0x8, struct.pack("!H", 1000))
        with self.assertRaises(WebSocketProtocolError):
            self.server.recv_message()
        first = self.client.recv(2)
        self.assertEqual(first[0] & 0x0F, 0x8)  # close echoed

    def test_send_text_uses_unmasked_frame(self) -> None:
        self.server.send_text("hi")
        first = self.client.recv(2)
        self.assertEqual(first[0] & 0x0F, 0x1)
        self.assertEqual(first[1] & 0x80, 0)  # server frames are not masked
        length = first[1] & 0x7F
        self.assertEqual(self.client.recv(length), b"hi")

    def test_oversized_frame_rejected(self) -> None:
        small = WebSocket(
            self.server_sock.makefile("rb"), self.server_sock.makefile("wb"), max_payload=4
        )
        self._send_client_frame(0x1, b"toolarge")
        with self.assertRaises(WebSocketProtocolError):
            small.recv_message()


if __name__ == "__main__":
    unittest.main()
