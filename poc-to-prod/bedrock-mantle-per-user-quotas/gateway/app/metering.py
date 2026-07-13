"""Usage metering across the three protocols served by bedrock-mantle.

Handles:
- pre-flight estimation (input tokens from the request body, output-token
  ceiling from max_tokens / max_output_tokens / max_completion_tokens)
- usage extraction from non-streaming JSON responses
- usage extraction from SSE streams (OpenAI Responses events, OpenAI Chat
  Completions chunks, Anthropic Messages events)

Everything here is pure-python and independently unit-testable.
"""

import json
from dataclasses import dataclass


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    # Anthropic prompt caching reports these SEPARATELY from input_tokens.
    # Coding agents (Claude Code, etc.) cache aggressively, so ignoring them
    # would badly undercount their usage.
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    found: bool = False  # whether real usage data was seen in the response


# ---------------------------------------------------------------------------
# Pre-flight estimation
# ---------------------------------------------------------------------------

def _collect_text(node) -> int:
    """Recursively sum the length of every string under *node*.

    Works for all three request shapes without needing schema knowledge:
    strings inside `input`, `messages`, `system`, tool definitions, etc.
    """
    if isinstance(node, str):
        return len(node)
    if isinstance(node, dict):
        return sum(_collect_text(v) for v in node.values())
    if isinstance(node, list):
        return sum(_collect_text(v) for v in node)
    return 0


def estimate_input_tokens(body: dict, chars_per_token: float = 4.0) -> int:
    """Heuristic estimate of input tokens for a request body.

    This is only used for the pre-flight quota reservation; the counters
    are settled with real usage from the response afterwards. chars/4 is
    a widely used approximation for English text.
    """
    chars = 0
    for key in ("input", "messages", "system", "instructions", "prompt", "tools"):
        if key in body:
            chars += _collect_text(body[key])
    return max(int(chars / chars_per_token), 1)


def requested_max_output_tokens(body: dict, fallback: int) -> int:
    for key in ("max_output_tokens", "max_completion_tokens", "max_tokens"):
        value = body.get(key)
        if isinstance(value, int) and value > 0:
            return value
    return fallback


# ---------------------------------------------------------------------------
# Non-streaming usage extraction
# ---------------------------------------------------------------------------

def _usage_from_dict(u: dict) -> Usage:
    return Usage(
        # OpenAI Responses + Anthropic use input_tokens/output_tokens;
        # Chat Completions uses prompt_tokens/completion_tokens.
        input_tokens=int(u.get("input_tokens", u.get("prompt_tokens", 0)) or 0),
        output_tokens=int(u.get("output_tokens", u.get("completion_tokens", 0)) or 0),
        # Anthropic prompt caching (separate from input_tokens). OpenAI's
        # cached tokens are already included in input_tokens, so they are
        # intentionally not double-counted here.
        cache_write_tokens=int(u.get("cache_creation_input_tokens", 0) or 0),
        cache_read_tokens=int(u.get("cache_read_input_tokens", 0) or 0),
        found=True,
    )


def extract_usage_json(payload: dict) -> Usage:
    usage = payload.get("usage")
    if isinstance(usage, dict):
        return _usage_from_dict(usage)
    # Responses API sometimes nests it under "response" (e.g. retrievals).
    response = payload.get("response")
    if isinstance(response, dict) and isinstance(response.get("usage"), dict):
        return _usage_from_dict(response["usage"])
    return Usage()


# ---------------------------------------------------------------------------
# Streaming (SSE) usage extraction
# ---------------------------------------------------------------------------

class SseUsageExtractor:
    """Incrementally parse an SSE byte stream and capture usage.

    Feed it raw chunks as they pass through the proxy; it maintains a line
    buffer and inspects every `data: {...}` JSON payload. Protocol notes:

    - OpenAI Responses: the `response.completed` event carries
      `response.usage.{input_tokens,output_tokens}`.
    - OpenAI Chat Completions: the final chunk carries `usage` when the
      request includes `stream_options: {"include_usage": true}` (the
      gateway injects this).
    - Anthropic Messages: `message_start` carries input tokens,
      `message_delta` carries cumulative output tokens.
    """

    def __init__(self) -> None:
        self.usage = Usage()
        self._buffer = b""

    def feed(self, chunk: bytes) -> None:
        self._buffer += chunk
        while b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            self._feed_line(line.strip())

    def close(self) -> None:
        if self._buffer:
            self._feed_line(self._buffer.strip())
            self._buffer = b""

    def _feed_line(self, line: bytes) -> None:
        if not line.startswith(b"data:"):
            return
        data = line[len(b"data:"):].strip()
        if not data or data == b"[DONE]":
            return
        try:
            payload = json.loads(data)
        except (ValueError, UnicodeDecodeError):
            return
        if not isinstance(payload, dict):
            return
        self._inspect(payload)

    def _inspect(self, payload: dict) -> None:
        # OpenAI Responses events: {"type": "response.completed", "response": {"usage": {...}}}
        response = payload.get("response")
        if isinstance(response, dict) and isinstance(response.get("usage"), dict):
            self._merge(response["usage"])
        # Chat Completions final chunk / Anthropic events: top-level usage
        if isinstance(payload.get("usage"), dict):
            self._merge(payload["usage"])
        # Anthropic message_start: {"type": "message_start", "message": {"usage": {...}}}
        message = payload.get("message")
        if isinstance(message, dict) and isinstance(message.get("usage"), dict):
            self._merge(message["usage"])

    def _merge(self, u: dict) -> None:
        new = _usage_from_dict(u)
        # Streams report usage cumulatively; keep the max of what we saw.
        self.usage.input_tokens = max(self.usage.input_tokens, new.input_tokens)
        self.usage.output_tokens = max(self.usage.output_tokens, new.output_tokens)
        self.usage.cache_write_tokens = max(self.usage.cache_write_tokens, new.cache_write_tokens)
        self.usage.cache_read_tokens = max(self.usage.cache_read_tokens, new.cache_read_tokens)
        self.usage.found = True
