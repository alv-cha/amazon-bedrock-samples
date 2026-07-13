import json

from app.metering import (
    SseUsageExtractor,
    estimate_input_tokens,
    extract_usage_json,
    requested_max_output_tokens,
)

# ---------------------------------------------------------------------------
# Pre-flight estimation
# ---------------------------------------------------------------------------

def test_estimate_responses_string_input():
    body = {"model": "m", "input": "x" * 400}
    assert estimate_input_tokens(body) == 100


def test_estimate_chat_messages():
    body = {"messages": [{"role": "user", "content": "y" * 200},
                         {"role": "system", "content": "z" * 200}]}
    est = estimate_input_tokens(body)
    assert est >= 100  # 400+ chars of content (role strings also count)


def test_estimate_anthropic_system_plus_messages():
    body = {"system": "s" * 100, "messages": [{"role": "user", "content": "u" * 300}]}
    assert estimate_input_tokens(body) >= 100


def test_estimate_never_zero():
    assert estimate_input_tokens({"model": "m"}) == 1


def test_max_output_tokens_priority_and_fallback():
    assert requested_max_output_tokens({"max_output_tokens": 10, "max_tokens": 99}, 4096) == 10
    assert requested_max_output_tokens({"max_completion_tokens": 7}, 4096) == 7
    assert requested_max_output_tokens({"max_tokens": 55}, 4096) == 55
    assert requested_max_output_tokens({}, 4096) == 4096


# ---------------------------------------------------------------------------
# Non-streaming usage
# ---------------------------------------------------------------------------

def test_usage_openai_responses_shape():
    u = extract_usage_json({"usage": {"input_tokens": 12, "output_tokens": 34}})
    assert (u.input_tokens, u.output_tokens, u.found) == (12, 34, True)


def test_usage_chat_completions_shape():
    u = extract_usage_json({"usage": {"prompt_tokens": 5, "completion_tokens": 9}})
    assert (u.input_tokens, u.output_tokens, u.found) == (5, 9, True)


def test_usage_missing():
    u = extract_usage_json({"choices": []})
    assert not u.found


def test_usage_anthropic_prompt_caching_fields():
    """Claude Code-style usage: most input arrives as cache reads/writes."""
    u = extract_usage_json({"usage": {
        "input_tokens": 12, "output_tokens": 300,
        "cache_creation_input_tokens": 4000, "cache_read_input_tokens": 25000,
    }})
    assert u.input_tokens == 12
    assert u.cache_write_tokens == 4000
    assert u.cache_read_tokens == 25000


# ---------------------------------------------------------------------------
# Streaming (SSE) usage
# ---------------------------------------------------------------------------

def _sse(payload: dict) -> bytes:
    return b"data: " + json.dumps(payload).encode() + b"\n\n"


def test_sse_openai_responses_completed_event():
    ex = SseUsageExtractor()
    ex.feed(_sse({"type": "response.output_text.delta", "delta": "hi"}))
    ex.feed(_sse({"type": "response.completed",
                  "response": {"usage": {"input_tokens": 40, "output_tokens": 60}}}))
    ex.feed(b"data: [DONE]\n\n")
    ex.close()
    assert (ex.usage.input_tokens, ex.usage.output_tokens, ex.usage.found) == (40, 60, True)


def test_sse_chat_completions_include_usage_final_chunk():
    ex = SseUsageExtractor()
    ex.feed(_sse({"choices": [{"delta": {"content": "a"}}]}))
    ex.feed(_sse({"choices": [], "usage": {"prompt_tokens": 11, "completion_tokens": 22}}))
    ex.close()
    assert (ex.usage.input_tokens, ex.usage.output_tokens) == (11, 22)


def test_sse_anthropic_message_start_and_delta():
    ex = SseUsageExtractor()
    ex.feed(b"event: message_start\n")
    ex.feed(_sse({"type": "message_start",
                  "message": {"usage": {"input_tokens": 17, "output_tokens": 1,
                                        "cache_creation_input_tokens": 900,
                                        "cache_read_input_tokens": 8000}}}))
    ex.feed(b"event: message_delta\n")
    ex.feed(_sse({"type": "message_delta", "usage": {"output_tokens": 88}}))
    ex.close()
    assert (ex.usage.input_tokens, ex.usage.output_tokens) == (17, 88)
    assert (ex.usage.cache_write_tokens, ex.usage.cache_read_tokens) == (900, 8000)


def test_sse_survives_chunk_boundaries_mid_json():
    full = _sse({"type": "response.completed",
                 "response": {"usage": {"input_tokens": 3, "output_tokens": 4}}})
    ex = SseUsageExtractor()
    for i in range(0, len(full), 7):  # feed in awkward 7-byte chunks
        ex.feed(full[i:i + 7])
    ex.close()
    assert (ex.usage.input_tokens, ex.usage.output_tokens) == (3, 4)


def test_sse_ignores_garbage():
    ex = SseUsageExtractor()
    ex.feed(b"data: {not json}\n\n")
    ex.feed(b": keepalive comment\n\n")
    ex.close()
    assert not ex.usage.found
