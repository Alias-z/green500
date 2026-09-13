import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

import pytest

from green500.processing.chat_completion import ChatCompletionClient, completion_text


class _ChatHandler(BaseHTTPRequestHandler):
    requests: ClassVar[list[dict]] = []

    def do_POST(self):
        length = int(self.headers["Content-Length"])
        payload = json.loads(self.rfile.read(length))
        self.requests.append(
            {
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "payload": payload,
            }
        )
        response = json.dumps(
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": '{"ok":true}', "refusal": None},
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 4,
                    "total_tokens": 14,
                },
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, *_):
        return


def test_openai_compatible_client_reuses_one_http_client():
    _ChatHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ChatHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    async def run():
        client = ChatCompletionClient(
            f"http://127.0.0.1:{server.server_port}/v1",
            "test-secret",
            "test-model",
            extra_body={"max_tokens": 100},
        )
        first = await client.complete([{"role": "user", "content": "one"}])
        pooled_client = client._client
        second = await client.complete([{"role": "user", "content": "two"}])
        assert client._client is pooled_client
        assert completion_text(first) == '{"ok":true}'
        assert completion_text(second) == '{"ok":true}'
        await client.aclose()
        assert pooled_client is not None and pooled_client.is_closed

    try:
        asyncio.run(run())
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

    assert [request["path"] for request in _ChatHandler.requests] == [
        "/v1/chat/completions",
        "/v1/chat/completions",
    ]
    assert all(
        request["authorization"] == "Bearer test-secret"
        for request in _ChatHandler.requests
    )
    assert _ChatHandler.requests[0]["payload"] == {
        "model": "test-model",
        "messages": [{"role": "user", "content": "one"}],
        "stream": False,
        "max_tokens": 100,
    }


@pytest.mark.parametrize(
    "choice",
    [
        {"finish_reason": "length", "message": {"content": "{}"}},
        {
            "finish_reason": "stop",
            "message": {"content": "{}", "refusal": "refused"},
        },
        {"finish_reason": "stop", "message": {"content": ""}},
    ],
)
def test_completion_text_rejects_unusable_provider_output(choice):
    with pytest.raises(ValueError):
        completion_text({"choices": [choice]})
