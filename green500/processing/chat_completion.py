"""Provider-neutral OpenAI-compatible Chat Completions transport."""

import re
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlsplit

import httpx


class CompletionClient(Protocol):
    """Minimum transport contract used by every extraction batch."""

    base_url: str
    model: str

    async def complete(self, messages: list[dict], *, operation: str) -> dict: ...


@dataclass(frozen=True)
class ChatCompletionClient:
    """Call a configured endpoint without requiring a particular provider or plan."""

    base_url: str
    api_key: str = field(repr=False)
    model: str
    timeout_seconds: float = 180
    extra_body: dict = field(default_factory=dict)
    _client: httpx.AsyncClient | None = field(
        default=None, init=False, repr=False, compare=False
    )

    def __post_init__(self):
        """Reject malformed endpoints and overrides of the core request contract."""
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Chat Completions base_url must be an HTTP(S) URL.")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Keep credentials in api_key, outside the endpoint URL.")
        if not self.model or not self.api_key or self.timeout_seconds <= 0:
            raise ValueError("A model, API key and positive timeout are required.")
        if set(self.extra_body) & {"model", "messages", "stream"}:
            raise ValueError("extra_body cannot replace model, messages or stream.")

    async def complete(
        self, messages: list[dict], *, operation: str = "extraction"
    ) -> dict:
        """Return the complete response envelope; never silently retry paid calls."""
        client = self._client
        if client is None or client.is_closed:
            client = httpx.AsyncClient(
                timeout=self.timeout_seconds, follow_redirects=False
            )
            object.__setattr__(self, "_client", client)
        response = await client.post(
            self.base_url.rstrip("/") + "/chat/completions",
            headers={"Authorization": "Bearer " + self.api_key},
            json={
                "model": self.model,
                "messages": messages,
                "stream": False,
                **self.extra_body,
            },
        )
        try:
            envelope = response.json()
        except ValueError:
            envelope = {"response_text": response.text}
        if not isinstance(envelope, dict):
            envelope = {"response_body": envelope}
        return {**envelope, "_http_status": response.status_code}

    async def aclose(self) -> None:
        """Close pooled HTTP connections after a batch finishes."""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.aclose()


def completion_text(envelope: dict) -> str:
    """Reject truncated, refused or empty completions before schema validation."""
    if envelope.get("_http_status", 200) != 200:
        raise RuntimeError(
            f"Chat Completions returned HTTP {envelope['_http_status']}; inspect the saved response."
        )
    choice = envelope["choices"][0]
    message = choice["message"]
    if choice.get("finish_reason") != "stop" or message.get("refusal"):
        raise ValueError("The model response was incomplete or refused.")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("The model response contained no text.")
    content = content.strip()
    fenced = re.fullmatch(
        r"```(?:json)?\s*\n(.*)\n```", content, re.DOTALL | re.IGNORECASE
    )
    return fenced[1].strip() if fenced else content
