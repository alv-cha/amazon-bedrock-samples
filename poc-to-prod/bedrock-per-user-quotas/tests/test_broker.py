"""Tests for the credential broker: vend path + budget/blocked gating.

The broker hands short-lived AWS creds to in-budget users (per-user via
RoleSessionName + SourceIdentity = JWT sub) instead of proxying inference.
STS is faked; we assert the vend arguments and the enforcement responses.
"""

import re
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

    # STS was called with per-user identity stamping. RoleSessionName,
    # SourceIdentity and the session tag all carry the SAME sanitized identity
    # (they share STS charset limits); the full sub lives in the reverse map.
    call = sts.calls[0]
    assert call["RoleArn"] == ROLE_ARN
    assert call["RoleSessionName"] == session_name_for(sub)
    assert call["SourceIdentity"] == session_name_for(sub)
    assert call["Tags"] == [{"Key": "quota-user", "Value": session_name_for(sub)}]
    assert call["DurationSeconds"] == 900

    # session -> user mapping persisted for the reconciler.
    assert store.resolve_session(session_name_for(sub)) == sub


# The three STS fields have DIFFERENT charsets; session_name_for must satisfy
# ALL of them (their intersection) or AssumeRole rejects the whole vend call:
#   RoleSessionName / SourceIdentity: [\w+=,.@-], 2-64 chars
#   session-tag VALUE:                no comma (','), so the safe set excludes it
# We assert the stricter intersection (no comma) and the 2-char minimum.
STS_NAME_SAFE = re.compile(r"\A[\w+=,.@-]{2,64}\Z", re.ASCII)          # name/SourceIdentity
STS_TAG_VALUE_SAFE = re.compile(r"\A[\w=.:/+@ -]{1,256}\Z", re.ASCII)  # tag value (no comma)


@pytest.mark.parametrize("sub", [
    "auth0|5f4dcc3b4e9b0d2c1a3f8e91",          # Auth0: pipe
    "google-oauth2|108451234567890123456",     # Google federated: pipe
    "c:0(.t|dev)test-user@contoso.onmicrosoft.com",  # Entra external: pipe + parens
    "josé.münchen",                            # non-ASCII word chars
    "x" * 200,                                 # far exceeds the 64-char limit
    "CN=alice,OU=eng,DC=corp",                 # LDAP/X.500 DN: comma (tag-value-illegal)
    "Acme, Inc. (EU)",                         # tenant display name: comma + space + parens
    "!!!",                                     # all-punctuation -> must fall back, not empty
])
def test_vend_sanitizes_all_sts_identity_fields(client, sub):
    """Regression: subs with '|', non-ASCII, or >64 chars must still vend.

    Before the fix, SourceIdentity and the session tag received the raw sub,
    so any of these would make sts:AssumeRole raise ValidationError -> 500.
    """
    api, store, sts = client
    resp = api.post("/v1/credentials", headers={"Authorization": f"Bearer {make_jwt(sub)}"})
    assert resp.status_code == 200, resp.text

    call = sts.calls[0]
    for field in ("RoleSessionName", "SourceIdentity"):
        assert STS_NAME_SAFE.match(call[field]), f"{field}={call[field]!r} invalid for STS"
    # Tag value has a stricter charset (no comma) — the sanitizer must satisfy it too.
    tag_value = call["Tags"][0]["Value"]
    assert STS_TAG_VALUE_SAFE.match(tag_value), f"tag value {tag_value!r} invalid"
    assert "," not in tag_value  # explicit: comma is the field that used to slip through
    # The full, unmodified sub is still recoverable for metering attribution.
    assert store.resolve_session(call["RoleSessionName"]) == sub


def test_session_name_collision_resistance_and_min_length():
    """session_name_for: distinct subs -> distinct names even when they share
    a long common prefix, and the name is always a valid STS length (>=2)."""
    # Two subs identical for the first 100 chars, differing only at the end.
    a = "https://issuer.example.com/tenants/acme/users/" + "0" * 60 + "A"
    b = "https://issuer.example.com/tenants/acme/users/" + "0" * 60 + "B"
    na, nb = session_name_for(a), session_name_for(b)
    assert na != nb, "distinct subs must not collide onto one session name"
    assert 2 <= len(na) <= 64 and 2 <= len(nb) <= 64
    # An all-punctuation sub can't sanitize to empty (would be an invalid name).
    n_punct = session_name_for("|||")
    assert 2 <= len(n_punct) <= 64
    assert n_punct.startswith("user-")  # fell back to the "user" prefix
    # Deterministic: same sub -> same name (needed for reverse-map lookups).
    assert session_name_for(a) == na


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
