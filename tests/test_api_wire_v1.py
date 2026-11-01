"""Exercise the real SDK against a local OpenAI-format HTTP service."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from fishkernel.config import config_from_values
from fishkernel.llm import OpenAIClient, LLMError


@pytest.mark.parametrize("status", [200, 400])
def test_generic_model_and_thinking_mapping_on_wire(status):
    received = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            body = ({"id":"test", "object":"chat.completion", "created":1, "model":"vendor/custom:2026",
                     "choices":[{"index":0, "message":{"role":"assistant","content":"ok","reasoning_content":"full reasoning"},"finish_reason":"stop"}],
                     "usage":{"prompt_tokens":8,"completion_tokens":4,"total_tokens":12}}
                    if status == 200 else {"error":{"message":"unsupported parameter","type":"invalid_request_error"}})
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config = config_from_values({"model":"vendor/custom:2026", "api_key":"test-key", "thinking":"on",
                                 "api_base":f"http://127.0.0.1:{server.server_port}/v1"})
    config.options = {"send_temperature":False, "output_parameter":"max_completion_tokens",
                      "thinking_modes":{"on":{"reasoning_effort":"custom-effort", "extra_body":{"thinking":{"enabled":True}}}}}
    client = OpenAIClient(config)
    try:
        if status == 200:
            result = client.complete([{"role":"user", "content":"hello"}])
            assert result.text == "ok" and result.reasoning == "full reasoning"
            assert result.usage["prompt_tokens"] == 8
        else:
            with pytest.raises(LLMError) as error:
                client.complete([{"role":"user", "content":"hello"}])
            assert error.value.retryable is False
        assert len(received) == 1
        path, body = received[0]
        assert path == "/v1/chat/completions"
        assert body["model"] == "vendor/custom:2026"
        assert body["reasoning_effort"] == "custom-effort"
        assert body["thinking"] == {"enabled":True}
        assert "max_completion_tokens" in body and "max_tokens" not in body
        assert "temperature" not in body
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        if client._client:
            client._client.close()
