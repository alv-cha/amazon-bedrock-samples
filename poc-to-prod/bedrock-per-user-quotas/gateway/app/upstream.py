"""Forwarder to the bedrock-mantle endpoint.

Upstream auth uses a short-term Amazon Bedrock API key generated from the
gateway's own IAM role via aws-bedrock-token-generator (the library caches
and auto-refreshes). No long-term secrets are stored anywhere: end users
hold only gateway keys, and the gateway holds only its IAM role.

Set MANTLE_API_KEY to override with a static key (useful for local dev).
"""

import os

import httpx

from .config import settings

# Headers we forward from client -> mantle. Authorization is always
# replaced with the gateway's own upstream token.
_FORWARD_REQUEST_HEADERS = {
    "content-type",
    "accept",
    "openai-project",
    "openai-beta",
    "anthropic-version",
    "anthropic-beta",
}
# Hop-by-hop / infrastructure headers we strip from mantle -> client.
_SKIP_RESPONSE_HEADERS = {
    "content-length", "transfer-encoding", "connection", "keep-alive", "date", "server",
}


def upstream_token() -> str:
    static = os.environ.get("MANTLE_API_KEY")
    if static:
        return static
    from aws_bedrock_token_generator import provide_token
    return provide_token(region=settings.aws_region)


class MantleClient:
    def __init__(self, base_url: str | None = None, transport: httpx.AsyncBaseTransport | None = None):
        self._client = httpx.AsyncClient(
            base_url=base_url or settings.base_url,
            timeout=httpx.Timeout(settings.request_timeout_seconds, connect=10.0),
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def _headers(self, incoming: dict[str, str], path: str = "") -> dict[str, str]:
        headers = {
            k: v for k, v in incoming.items()
            if k.lower() in _FORWARD_REQUEST_HEADERS
        }
        token = upstream_token()
        if path.startswith("/anthropic/"):
            # The Anthropic-compatible surface authenticates with x-api-key
            # and requires an anthropic-version header.
            headers["x-api-key"] = token
            headers.setdefault("anthropic-version", "2023-06-01")
        else:
            headers["Authorization"] = f"Bearer {token}"
        headers.setdefault("Content-Type", "application/json")
        return headers

    async def post_json(self, path: str, body: bytes, incoming_headers: dict[str, str]) -> httpx.Response:
        """Non-streaming request: returns the full upstream response."""
        return await self._client.post(path, content=body, headers=self._headers(incoming_headers, path))

    async def get(self, path: str, incoming_headers: dict[str, str]) -> httpx.Response:
        headers = self._headers(incoming_headers, path)
        headers.pop("Content-Type", None)
        return await self._client.get(path, headers=headers)

    async def delete(self, path: str, incoming_headers: dict[str, str]) -> httpx.Response:
        headers = self._headers(incoming_headers, path)
        headers.pop("Content-Type", None)
        return await self._client.delete(path, headers=headers)

    async def post_stream(self, path: str, body: bytes, incoming_headers: dict[str, str]):
        """Streaming request: returns an opened httpx response (SSE).

        Caller is responsible for closing it (or iterating to the end).
        """
        request = self._client.build_request(
            "POST", path, content=body, headers=self._headers(incoming_headers, path)
        )
        return await self._client.send(request, stream=True)


def response_headers(upstream: httpx.Response) -> dict[str, str]:
    return {
        k: v for k, v in upstream.headers.items()
        if k.lower() not in _SKIP_RESPONSE_HEADERS
    }
