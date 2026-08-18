"""Control-plane API tests for the runtime-only credential broker."""

import time

import jwt as pyjwt
import pytest
from fastapi.testclient import TestClient

import app.main as gateway
from app.broker import VendedCredentials, session_name_for
from app.quota import MICRO, QuotaStore, current_window

SECRET = "test-jwt-secret"
ADMIN = {"Authorization": "Bearer admin-secret"}


def make_jwt(sub: str, **extra) -> str:
    return pyjwt.encode(
        {"sub": sub, "exp": int(time.time()) + 3600, **extra},
        SECRET,
        algorithm="HS256",
    )


class FakeBroker:
    def __init__(self):
        self.users: list[str] = []

    def vend(self, identity):
        self.users.append(identity.user_id)
        return VendedCredentials(
            access_key_id="ASIAFAKE",
            secret_access_key="secret",
            session_token="token",
            expiration="2026-08-18T12:15:00+00:00",
            user_id=identity.user_id,
            session_name=session_name_for(identity.user_id),
        )


@pytest.fixture
def client(fake_dynamodb, monkeypatch):
    store = QuotaStore(dynamodb=fake_dynamodb)
    fake_broker = FakeBroker()
    monkeypatch.setattr(gateway, "_store", store)
    monkeypatch.setattr(gateway, "_broker", fake_broker)
    monkeypatch.setattr(gateway, "_admin_key", "admin-secret")
    return TestClient(gateway.app), store, fake_broker


def _vend(api, token: str):
    return api.post(
        "/v1/credentials",
        headers={"Authorization": f"Bearer {token}"},
    )


def test_health_describes_runtime_only_event_driven_architecture(client):
    api, _, _ = client
    assert api.get("/healthz").json() == {
        "status": "ok",
        "inference_endpoint": "bedrock-runtime",
        "metering": "cloudwatch-logs-subscription",
    }
    assert api.post("/v1/responses").status_code == 404
    assert api.post("/anthropic/v1/messages").status_code == 404


def test_vend_requires_valid_jwt_and_auto_provisions(client):
    api, store, broker = client
    assert api.post("/v1/credentials").status_code == 401
    assert _vend(api, "not-a-jwt").status_code == 401

    response = _vend(api, make_jwt("alice", email="alice@example.com"))
    assert response.status_code == 200
    assert response.json()["endpoint"].startswith(
        "https://bedrock-runtime."
    )
    assert response.json()["user_id"] == "alice"
    assert broker.users == ["alice"]
    assert store.get_user("alice").name == "alice@example.com"
    assert store.resolve_session(session_name_for("alice")) == "alice"


def test_dedicated_user_header_coexists_with_sigv4(client):
    api, _, _ = client
    response = api.post(
        "/v1/credentials",
        headers={
            "Authorization": "AWS4-HMAC-SHA256 Credential=example",
            "X-Quota-User-Token": make_jwt("alice"),
        },
    )
    assert response.status_code == 200


def test_auto_provision_can_be_disabled(client, monkeypatch):
    import app.auth as auth_module
    from app.config import Settings

    api, _, _ = client
    monkeypatch.setenv("AUTO_PROVISION_USERS", "false")
    fresh = Settings()
    monkeypatch.setattr(gateway, "settings", fresh)
    monkeypatch.setattr(auth_module, "settings", fresh)
    assert _vend(api, make_jwt("unknown")).status_code == 401


def test_quota_identity_can_be_a_tenant_claim(client, monkeypatch):
    import app.auth as auth_module
    from app.config import Settings

    api, store, broker = client
    monkeypatch.setenv("JWT_USER_CLAIM", "tenant_id")
    fresh = Settings()
    monkeypatch.setattr(gateway, "settings", fresh)
    monkeypatch.setattr(auth_module, "settings", fresh)

    response = _vend(
        api, make_jwt("human-user", tenant_id="tenant-acme")
    )
    assert response.status_code == 200
    assert broker.users == ["tenant-acme"]
    assert store.get_user("tenant-acme") is not None


def test_manual_and_usage_blocks_prevent_vending(client):
    api, store, broker = client
    store.put_user("manual", "Manual", 1, 100, 50)
    store.set_user_status("manual", "blocked", "admin API")
    assert _vend(api, make_jwt("manual")).status_code == 403

    store.put_user("spent", "Spent", 1, 100, 50)
    store._usage.put_item(  # noqa: SLF001
        Item={
            "user_id": "spent",
            "window": current_window(),
            "cost_micro": MICRO,
            "input_tokens": 0,
            "output_tokens": 0,
            "requests": 1,
        }
    )
    response = _vend(api, make_jwt("spent"))
    assert response.status_code == 429
    assert store.get_user("spent").status == "blocked"
    assert broker.users == []


def test_previous_auto_block_is_reactivated_at_next_vend(client):
    api, store, broker = client
    store.put_user("fresh", "Fresh", 1, 100, 50)
    store.set_user_status("fresh", "blocked", "auto: previous window")
    assert _vend(api, make_jwt("fresh")).status_code == 200
    assert store.get_user("fresh").active
    assert broker.users == ["fresh"]


def test_admin_create_list_update_block_and_usage(client):
    api, store, _ = client
    created = api.post(
        "/admin/users",
        json={
            "user_id": "tenant/acme",
            "name": "ACME",
            "daily_usd": 25,
            "daily_input_tokens": 1_000_000,
            "daily_output_tokens": 200_000,
        },
        headers=ADMIN,
    )
    assert created.status_code == 200
    assert created.json()["limits"]["daily_output_tokens"] == 200_000

    updated = api.put(
        "/admin/users/tenant%2Facme/limits",
        json={
            "daily_usd": 30,
            "daily_input_tokens": 2_000_000,
            "daily_output_tokens": 400_000,
        },
        headers=ADMIN,
    )
    assert updated.status_code == 200
    assert updated.json()["limits"] == {
        "daily_usd": 30.0,
        "daily_input_tokens": 2_000_000,
        "daily_output_tokens": 400_000,
    }

    blocked = api.put(
        "/admin/users/tenant%2Facme/status",
        json={"status": "blocked", "reason": "admin test"},
        headers=ADMIN,
    )
    assert blocked.status_code == 200
    assert store.get_user("tenant/acme").status_reason == "admin test"

    listing = api.get("/admin/users", headers=ADMIN).json()
    assert listing["users"][0]["user_id"] == "tenant/acme"
    assert "mantle_project_id" not in listing["users"][0]
    usage = api.get(
        "/admin/users/tenant%2Facme/usage", headers=ADMIN
    ).json()
    assert usage["requests"] == 0


def test_admin_summary_exposes_single_guarantee(client):
    api, store, _ = client
    store.put_user("alice", "Alice", 1, 100, 50)
    body = api.get("/admin/summary", headers=ADMIN).json()
    assert body["enforcement"]["mode"] == "bounded_overspend"
    assert body["enforcement"]["credential_ttl_seconds"] == 900
    assert body["observability"] == {
        "source": "bedrock_model_invocation_logs",
        "delivery": "cloudwatch_logs_subscription",
        "metrics_namespace": "BedrockQuotaGateway",
    }
    assert "reconciler_interval_minutes" not in body


def test_admin_requires_authorization_and_valid_payloads(client):
    api, store, _ = client
    assert api.get("/admin/users").status_code == 403
    assert api.post(
        "/admin/users", content="{", headers={**ADMIN, "Content-Type": "application/json"}
    ).status_code == 400
    assert api.post(
        "/admin/users", json={"daily_usd": 1}, headers=ADMIN
    ).status_code == 400

    store.put_user("alice", "Alice", 1, 100, 50)
    for payload in (
        {},
        {"daily_usd": -1},
        {"daily_input_tokens": 1.5},
        {"daily_output_tokens": False},
    ):
        assert api.put(
            "/admin/users/alice/limits", json=payload, headers=ADMIN
        ).status_code == 400
    assert api.put(
        "/admin/users/alice/status",
        json={"status": "invalid"},
        headers=ADMIN,
    ).status_code == 400


def _enable_admin_jwt(monkeypatch):
    import app.auth as auth_module
    from app.config import Settings

    monkeypatch.setenv("ADMIN_JWT_CLAIM", "groups")
    monkeypatch.setenv("ADMIN_JWT_VALUE", "quota-admins")
    fresh = Settings()
    monkeypatch.setattr(gateway, "settings", fresh)
    monkeypatch.setattr(auth_module, "settings", fresh)


def test_admin_jwt_group_is_supported(client, monkeypatch):
    api, _, _ = client
    _enable_admin_jwt(monkeypatch)
    accepted = make_jwt("admin", groups=["quota-admins"])
    rejected = make_jwt("user", groups=["developers"])
    assert api.get(
        "/admin/summary",
        headers={"Authorization": f"Bearer {accepted}"},
    ).status_code == 200
    assert api.get(
        "/admin/summary",
        headers={"Authorization": f"Bearer {rejected}"},
    ).status_code == 403
