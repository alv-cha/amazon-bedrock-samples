"""End-to-end tests of the FastAPI gateway against a mocked bedrock-mantle.

Covers the full request path: JWT auth -> reserve -> forward -> settle,
including streaming, 429s, auto-provisioning, and upstream failures.
JWTs are HS256-signed with the shared secret set in conftest.
"""

import json
import os
import time

import httpx
import jwt as pyjwt
import pytest
from fastapi.testclient import TestClient

os.environ["MANTLE_API_KEY"] = "test-upstream-key"  # skip token generator

import app.main as gateway  # noqa: E402
from app.quota import QuotaStore  # noqa: E402
from app.upstream import MantleClient  # noqa: E402

MODEL = "openai.gpt-oss-120b"
SECRET = "test-jwt-secret"


def make_jwt(sub: str, **extra) -> str:
    return pyjwt.encode({"sub": sub, "exp": int(time.time()) + 3600, **extra},
                        SECRET, algorithm="HS256")


class UpstreamRecorder:
    """Programmable fake mantle endpoint (httpx MockTransport handler)."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.response_factory = self.default_response

    def default_response(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "resp_123", "object": "response",
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "hi"}]}],
                "usage": {"input_tokens": 10, "output_tokens": 20},
            },
        )

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.response_factory(request)


@pytest.fixture
def upstream():
    return UpstreamRecorder()


@pytest.fixture
def client(fake_dynamodb, upstream, monkeypatch):
    store = QuotaStore(dynamodb=fake_dynamodb)
    monkeypatch.setattr(gateway, "_store", store)
    monkeypatch.setattr(gateway, "_mantle", MantleClient(
        base_url="https://fake-mantle.test",
        transport=httpx.MockTransport(upstream),
    ))
    monkeypatch.setattr(gateway, "_admin_key", "admin-secret")
    return TestClient(gateway.app)


@pytest.fixture
def alice(fake_dynamodb):
    """Pre-provisioned user with known limits; returns her JWT."""
    QuotaStore(dynamodb=fake_dynamodb).put_user(
        "alice", "Alice",
        daily_usd=1.0, daily_input_tokens=100_000, daily_output_tokens=20_000)
    return make_jwt("alice")


def _post(client, token, body, path="/v1/responses"):
    return client.post(path, json=body, headers={"Authorization": f"Bearer {token}"})


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def test_rejects_missing_token(client):
    r = client.post("/v1/responses", json={"model": MODEL, "input": "hello"})
    assert r.status_code == 401


def test_rejects_garbage_token(client):
    r = _post(client, "not-a-jwt", {"model": MODEL, "input": "hello"})
    assert r.status_code == 401
    assert "invalid token" in r.json()["error"]["message"]


def test_dedicated_token_header_coexists_with_sigv4_authorization(
        client, alice, upstream):
    r = client.post(
        "/v1/responses",
        json={"model": MODEL, "input": "hello", "max_output_tokens": 50},
        headers={
            "Authorization": "AWS4-HMAC-SHA256 Credential=example",
            "X-Quota-User-Token": alice,
        },
    )
    assert r.status_code == 200
    assert len(upstream.requests) == 1


def test_rejects_expired_token(client):
    expired = pyjwt.encode({"sub": "alice", "exp": int(time.time()) - 100},
                           SECRET, algorithm="HS256")
    r = _post(client, expired, {"model": MODEL, "input": "hello"})
    assert r.status_code == 401
    assert "expired" in r.json()["error"]["message"]


def test_auto_provisions_unknown_user(client, fake_dynamodb, upstream):
    token = make_jwt("brand-new-user", email="new@example.com")
    r = _post(client, token, {"model": MODEL, "input": "hello", "max_output_tokens": 50})
    assert r.status_code == 200

    user = QuotaStore(dynamodb=fake_dynamodb).get_user("brand-new-user")
    assert user is not None
    assert user.name == "new@example.com"  # display name from claims
    assert user.daily_usd_micro > 0        # default limits applied


def test_auto_provision_can_be_disabled(client, monkeypatch):
    import app.main as m
    from app.config import Settings
    monkeypatch.setenv("AUTO_PROVISION_USERS", "false")
    monkeypatch.setattr(m, "settings", Settings())
    r = _post(client, make_jwt("stranger"), {"model": MODEL, "input": "hello"})
    assert r.status_code == 401
    assert "not provisioned" in r.json()["error"]["message"]


def test_missing_model_is_400(client, alice):
    r = _post(client, alice, {"input": "hello"})
    assert r.status_code == 400


def test_multi_tenant_budget_keys_on_tenant_claim(client, monkeypatch, fake_dynamodb):
    """Multi-tenant: budgeting on a tenant claim (not sub) shares one budget
    across a tenant's users and isolates spend between tenants."""
    import app.auth as auth_module
    import app.main as m
    from app.config import Settings
    monkeypatch.setenv("JWT_USER_CLAIM", "custom:tenant_id")
    fresh = Settings()
    # The identity claim is read by the JWT verifier (app.auth) at verify time;
    # main also reads settings, so patch both singletons.
    monkeypatch.setattr(auth_module, "settings", fresh)
    monkeypatch.setattr(m, "settings", fresh)

    store = QuotaStore(dynamodb=fake_dynamodb)
    # Tenant acme has a tiny budget; tenant globex has room.
    store.put_user("tenant-acme", "ACME", daily_usd=0.000001,
                   daily_input_tokens=10, daily_output_tokens=10)
    store.put_user("tenant-globex", "Globex", daily_usd=1.0,
                   daily_input_tokens=100_000, daily_output_tokens=20_000)

    # Two DIFFERENT users of tenant acme both resolve to the same budget.
    for user_sub in ("alice", "bob"):
        tok = make_jwt(user_sub, **{"custom:tenant_id": "tenant-acme"})
        r = _post(client, tok, {"model": MODEL, "input": "hello"})
        assert r.status_code == 429, f"{user_sub} should hit acme's exhausted budget"

    # A user of globex is unaffected by acme's exhaustion.
    r = _post(client, make_jwt("carol", **{"custom:tenant_id": "tenant-globex"}),
              {"model": MODEL, "input": "hello", "max_output_tokens": 50})
    assert r.status_code == 200
    assert QuotaStore(dynamodb=fake_dynamodb).get_window_usage("tenant-globex")["requests"] == 1
    # acme's own usage row never recorded a successful request.
    assert QuotaStore(dynamodb=fake_dynamodb).get_window_usage("tenant-acme")["requests"] == 0


# ---------------------------------------------------------------------------
# Proxy + quota
# ---------------------------------------------------------------------------

def test_proxies_and_settles_non_streaming(client, alice, upstream, fake_dynamodb):
    r = _post(client, alice, {"model": MODEL, "input": "hello", "max_output_tokens": 100})
    assert r.status_code == 200
    assert r.json()["usage"]["output_tokens"] == 20
    assert "X-Quota-Remaining-USD" in r.headers

    # Upstream got the gateway's Bedrock token, never the user's JWT.
    auth = upstream.requests[0].headers["authorization"]
    assert auth == "Bearer test-upstream-key"

    usage = QuotaStore(dynamodb=fake_dynamodb).get_window_usage("alice")
    assert usage["input_tokens"] == 10
    assert usage["output_tokens"] == 20
    assert usage["requests"] == 1


def test_429_when_budget_exhausted(client, fake_dynamodb, upstream):
    QuotaStore(dynamodb=fake_dynamodb).put_user(
        "cheap", "Cheap", daily_usd=0.000001,
        daily_input_tokens=10, daily_output_tokens=10)
    r = _post(client, make_jwt("cheap"), {"model": MODEL, "input": "hello"})
    assert r.status_code == 429
    assert r.json()["error"]["type"] == "quota_exceeded"
    assert upstream.requests == []  # never reached mantle


def test_upstream_error_releases_reservation(client, alice, upstream, fake_dynamodb):
    upstream.response_factory = lambda req: httpx.Response(500, json={"error": "boom"})
    r = _post(client, alice, {"model": MODEL, "input": "hello"})
    assert r.status_code == 500
    usage = QuotaStore(dynamodb=fake_dynamodb).get_window_usage("alice")
    assert usage["cost_usd"] == 0
    assert usage["errors"] == 1


def test_streaming_settles_from_sse(client, alice, upstream, fake_dynamodb):
    sse = (
        b'data: {"type": "response.output_text.delta", "delta": "h"}\n\n'
        b'data: {"type": "response.completed", "response": {"usage": '
        b'{"input_tokens": 7, "output_tokens": 13}}}\n\n'
        b"data: [DONE]\n\n"
    )
    upstream.response_factory = lambda req: httpx.Response(
        200, content=sse, headers={"content-type": "text/event-stream"})

    r = _post(client, alice,
              {"model": MODEL, "input": "hello", "stream": True, "max_output_tokens": 100})
    assert r.status_code == 200
    assert b"response.completed" in r.content  # stream passed through intact

    usage = QuotaStore(dynamodb=fake_dynamodb).get_window_usage("alice")
    assert usage["input_tokens"] == 7
    assert usage["output_tokens"] == 13


def test_chat_stream_injects_include_usage(client, alice, upstream):
    upstream.response_factory = lambda req: httpx.Response(
        200, content=b"data: [DONE]\n\n", headers={"content-type": "text/event-stream"})
    _post(client, alice,
          {"model": MODEL, "messages": [{"role": "user", "content": "hi"}], "stream": True},
          path="/v1/chat/completions")
    sent = json.loads(upstream.requests[0].content)
    assert sent["stream_options"]["include_usage"] is True


def test_anthropic_messages_route(client, alice, upstream):
    upstream.response_factory = lambda req: httpx.Response(
        200, json={"id": "msg_1", "content": [{"type": "text", "text": "hi"}],
                   "usage": {"input_tokens": 3, "output_tokens": 5}})
    r = _post(client, alice,
              {"model": "anthropic.claude-opus-4-7", "max_tokens": 100,
               "messages": [{"role": "user", "content": "hi"}]},
              path="/anthropic/v1/messages")
    assert r.status_code == 200
    sent = upstream.requests[0]
    # Forwarded to mantle's Anthropic surface with x-api-key auth.
    assert sent.url.path == "/anthropic/v1/messages"
    assert sent.headers["x-api-key"] == "test-upstream-key"
    assert sent.headers["anthropic-version"] == "2023-06-01"
    assert "authorization" not in sent.headers


def test_anthropic_sdk_style_x_api_key_auth(client, alice, upstream):
    """The Anthropic SDK sends the token via x-api-key, not Authorization."""
    upstream.response_factory = lambda req: httpx.Response(
        200, json={"id": "msg_1", "content": [],
                   "usage": {"input_tokens": 1, "output_tokens": 1}})
    r = client.post("/anthropic/v1/messages",
                    json={"model": "anthropic.claude-opus-4-7", "max_tokens": 10,
                          "messages": [{"role": "user", "content": "hi"}]},
                    headers={"x-api-key": alice})
    assert r.status_code == 200


def test_claude_code_style_cached_stream_is_metered(client, alice, upstream, fake_dynamodb):
    """Coding-agent traffic: /v1/messages, streamed, mostly cache reads."""
    sse = (
        b'data: {"type": "message_start", "message": {"usage": '
        b'{"input_tokens": 10, "output_tokens": 1, '
        b'"cache_creation_input_tokens": 500, "cache_read_input_tokens": 20000}}}\n\n'
        b'data: {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hi"}}\n\n'
        b'data: {"type": "message_delta", "usage": {"output_tokens": 42}}\n\n'
    )
    upstream.response_factory = lambda req: httpx.Response(
        200, content=sse, headers={"content-type": "text/event-stream"})

    r = client.post("/v1/messages",
                    json={"model": "anthropic.claude-opus-4-7", "max_tokens": 100,
                          "stream": True,
                          "messages": [{"role": "user", "content": "hi"}]},
                    headers={"Authorization": f"Bearer {alice}"})
    assert r.status_code == 200

    usage = QuotaStore(dynamodb=fake_dynamodb).get_window_usage("alice")
    assert usage["input_tokens"] == 10 + 500 + 20000  # cache tokens counted
    assert usage["output_tokens"] == 42
    assert usage["cost_usd"] > 0


def test_count_tokens_passthrough_no_quota(client, alice, upstream, fake_dynamodb):
    """Claude Code polls count_tokens constantly — must be free and proxied."""
    upstream.response_factory = lambda req: httpx.Response(200, json={"input_tokens": 123})
    r = client.post("/v1/messages/count_tokens",
                    json={"model": "anthropic.claude-opus-4-7",
                          "messages": [{"role": "user", "content": "hi"}]},
                    headers={"Authorization": f"Bearer {alice}"})
    assert r.status_code == 200
    assert r.json() == {"input_tokens": 123}
    assert upstream.requests[0].url.path == "/anthropic/v1/messages/count_tokens"

    usage = QuotaStore(dynamodb=fake_dynamodb).get_window_usage("alice")
    assert usage["requests"] == 0  # no reservation, no charge
    assert usage["cost_usd"] == 0


def test_v1_messages_alias_maps_to_anthropic_path(client, alice, upstream):
    upstream.response_factory = lambda req: httpx.Response(
        200, json={"id": "msg_1", "content": [],
                   "usage": {"input_tokens": 1, "output_tokens": 1}})
    r = _post(client, alice,
              {"model": "anthropic.claude-opus-4-7", "max_tokens": 10,
               "messages": [{"role": "user", "content": "hi"}]},
              path="/v1/messages")
    assert r.status_code == 200
    assert upstream.requests[0].url.path == "/anthropic/v1/messages"


# ---------------------------------------------------------------------------
# Admin API
# ---------------------------------------------------------------------------

def test_admin_requires_key(client):
    r = client.post("/admin/users", json={"user_id": "x"})
    assert r.status_code == 403


def test_dedicated_admin_header_coexists_with_sigv4_authorization(client):
    r = client.post(
        "/admin/users",
        json={"user_id": "signed-admin"},
        headers={
            "Authorization": "AWS4-HMAC-SHA256 Credential=example",
            "X-Quota-Admin-Key": "admin-secret",
        },
    )
    assert r.status_code == 200


def test_admin_preprovision_with_custom_limits(client, upstream, fake_dynamodb):
    r = client.post("/admin/users",
                    json={"user_id": "carol", "daily_usd": 2.5},
                    headers={"Authorization": "Bearer admin-secret"})
    assert r.status_code == 200
    assert r.json() == {"user_id": "carol", "provisioned": True}

    user = QuotaStore(dynamodb=fake_dynamodb).get_user("carol")
    assert user.daily_usd_micro == 2_500_000

    # Carol calls with her JWT; her pre-set limits apply (not defaults).
    r2 = _post(client, make_jwt("carol"), {"model": MODEL, "input": "hello"})
    assert r2.status_code == 200

    r3 = client.get("/admin/users/carol/usage",
                    headers={"Authorization": "Bearer admin-secret"})
    assert r3.json()["requests"] == 1


def test_admin_create_requires_user_id(client):
    r = client.post("/admin/users", json={"daily_usd": 1},
                    headers={"Authorization": "Bearer admin-secret"})
    assert r.status_code == 400


def test_admin_block_user(client, alice):
    r = client.put("/admin/users/alice/status", json={"status": "blocked"},
                   headers={"Authorization": "Bearer admin-secret"})
    assert r.status_code == 200
    r2 = _post(client, alice, {"model": MODEL, "input": "hello"})
    assert r2.status_code == 429
