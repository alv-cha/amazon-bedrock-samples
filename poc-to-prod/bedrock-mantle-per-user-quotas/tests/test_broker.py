"""Tests for the credential broker: vend path + budget/blocked gating.

The broker hands short-lived AWS creds to in-budget users (per-user via
RoleSessionName + SourceIdentity = JWT sub) instead of proxying inference.
STS is faked; we assert the vend arguments and the enforcement responses.
"""

import time

import jwt as pyjwt
import pytest
from fastapi.testclient import TestClient

import app.main as gateway
from app.broker import CredentialBroker, session_name_for
from app.quota import QuotaStore

SECRET = "test-jwt-secret"
ROLE_ARN = "arn:aws:iam::111122223333:role/BedrockUserRole"


def make_jwt(sub: str, **extra) -> str:
    return pyjwt.encode({"sub": sub, "exp": int(time.time()) + 3600, **extra},
                        SECRET, algorithm="HS256")


class FakeSTS:
    def __init__(self):
        self.calls = []

    def assume_role(self, **kwargs):
        self.calls.append(kwargs)

        class _Dt:
            def isoformat(self_inner):
                return "2026-07-12T00:15:00+00:00"

        return {"Credentials": {
            "AccessKeyId": "ASIAFAKE", "SecretAccessKey": "secret",
            "SessionToken": "token", "Expiration": _Dt(),
        }}


@pytest.fixture
def sts():
    return FakeSTS()


@pytest.fixture
def client(fake_dynamodb, sts, monkeypatch):
    store = QuotaStore(dynamodb=fake_dynamodb)
    monkeypatch.setattr(gateway, "_store", store)
    monkeypatch.setattr(gateway, "_broker",
                        CredentialBroker(sts_client=sts, role_arn=ROLE_ARN, ttl_seconds=900))
    return TestClient(gateway.app), store, sts


def test_vend_credentials_for_in_budget_user(client):
    api, store, sts = client
    sub = "alice@corp"
    resp = api.post("/v1/credentials", headers={"Authorization": f"Bearer {make_jwt(sub)}"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["aws_access_key_id"] == "ASIAFAKE"
    assert body["aws_session_token"] == "token"
    assert body["user_id"] == sub

    # STS was called with per-user identity stamping.
    call = sts.calls[0]
    assert call["RoleArn"] == ROLE_ARN
    assert call["RoleSessionName"] == session_name_for(sub)
    assert call["SourceIdentity"] == sub
    assert call["DurationSeconds"] == 900

    # session -> user mapping persisted for the reconciler.
    assert store.resolve_session(session_name_for(sub)) == sub


def test_blocked_user_gets_403_no_creds(client):
    api, store, sts = client
    sub = "blocked@corp"
    store.get_or_provision_user(sub)
    store.set_user_status(sub, "blocked", "manual")

    resp = api.post("/v1/credentials", headers={"Authorization": f"Bearer {make_jwt(sub)}"})
    assert resp.status_code == 403
    assert sts.calls == []  # never reached STS


def test_over_budget_user_gets_429_and_is_blocked(client):
    from datetime import datetime, timezone

    api, store, sts = client
    sub = "spender@corp"
    store.put_user(sub, name=sub, daily_usd=0.001,
                   daily_input_tokens=0, daily_output_tokens=0)
    # Simulate the reconciler having written over-budget usage for today.
    window = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    store._usage.put_item(Item={  # noqa: SLF001 (test reaches into store)
        "user_id": sub, "window": window,
        "cost_micro": 5000, "input_tokens": 0, "output_tokens": 0, "requests": 3,
    })

    resp = api.post("/v1/credentials", headers={"Authorization": f"Bearer {make_jwt(sub)}"})
    assert resp.status_code == 429
    assert sts.calls == []
    # vend-time gate flips status to blocked so alerts/reconciler agree.
    assert store.get_user(sub).status == "blocked"


def test_missing_token_is_401(client):
    api, _, sts = client
    resp = api.post("/v1/credentials")
    assert resp.status_code == 401
    assert sts.calls == []
