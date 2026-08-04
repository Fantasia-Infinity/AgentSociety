from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread
import unittest

from wechat_core.domain import ModelMessage, ModelRequest
from wechat_core.openai_compatible import OpenAICompatibleProvider


class LocalEndpointHandler(BaseHTTPRequestHandler):
    requests: list[dict] = []

    def do_GET(self) -> None:
        if self.path != "/health":
            self.send_error(404)
            return
        self._json({"status": "ok"})

    def do_POST(self) -> None:
        if self.path != "/v1/completions":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        self.requests.append(payload)
        self._json(
            {
                "model": "rwkv-local",
                "choices": [{"text": "local reply"}],
            }
        )

    def log_message(self, format: str, *args) -> None:
        pass

    def _json(self, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class LocalEndpointIntegrationTests(unittest.TestCase):
    def test_real_http_transport_health_and_completion(self) -> None:
        LocalEndpointHandler.requests = []
        try:
            server = ThreadingHTTPServer(("127.0.0.1", 0), LocalEndpointHandler)
        except PermissionError:
            self.skipTest("sandbox does not permit binding a loopback test socket")
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        host, port = server.server_address
        try:
            provider = OpenAICompatibleProvider(
                base_url=f"http://{host}:{port}/v1",
                api_key="",
                model="rwkv-local",
                backend_name="local_rwkv",
                health_url=f"http://{host}:{port}/health",
                top_p=0.5,
                repeat_penalty=1.2,
                request_format="rwkv_completion",
            )

            self.assertEqual(provider.health()["status"], "ready")
            response = provider.complete(
                ModelRequest(
                    conversation_id="conversation-1",
                    messages=(ModelMessage(role="user", content="hello"),),
                )
            )

            self.assertEqual(response.text, "local reply")
            self.assertEqual(LocalEndpointHandler.requests[0]["top_p"], 0.5)
            self.assertEqual(
                LocalEndpointHandler.requests[0]["repeat_penalty"],
                1.2,
            )
            self.assertEqual(
                LocalEndpointHandler.requests[0]["prompt"],
                "User: hello\n\nAssistant:",
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=1)


if __name__ == "__main__":
    unittest.main()
