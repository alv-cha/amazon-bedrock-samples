"""Control-plane API tests for the runtime-only credential broker."""

import json
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import jwt as pyjwt
import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

import app.main as gateway
from app.broker import BrokerError, VendedCredentials, session_name_for
from app.quota import MICRO, QuotaStore, current_window

SECRET = "test-jwt-secret"
ADMIN = {"Authorization": "Bearer admin-secret"}
EMERGENCY = {"X-Quota-Emergency-Key": "emergency-secret"}


def make_jwt(sub: str, **extra) -> str:
    return pyjwt.encode(
        {"sub": sub, "exp": int(time.time()) + 3600, **extra},
        SECRET,
        algorithm="HS256",
    )


class FakeBroker:
    def __init__(self):
        self.users: list[str] = []
        self.claims: list[dict] = []
        self.permission_deadlines = []

    def vend(self, identity, *, permission_deadline=None):
        self.users.append(identity.user_id)
        self.claims.append(identity.claims)
        self.permission_deadlines.append(permission_deadline)
        effective_expiration = (
            permission_deadline.isoformat()
            if permission_deadline is not None
            else "2026-08-18T12:15:00+00:00"
        )
        return VendedCredentials(
            access_key_id="ASIAFAKE",
            secret_access_key="secret",
            session_token="token",
            expiration=effective_expiration,
            sts_expiration="2026-08-18T12:15:00+00:00",
            user_id=identity.user_id,
            session_name=session_name_for(identity.user_id),
        )


class FakeCloudWatch:
    def __init__(self, *, results=None, alarms=None, error=None):
        self.results = results or []
        self.alarms = alarms or []
        self.error = error
        self.metric_calls = []
        self.alarm_calls = []

    def get_metric_data(self, **kwargs):
        self.metric_calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return {"MetricDataResults": self.results}

    def describe_alarms(self, **kwargs):
        self.alarm_calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return {"MetricAlarms": self.alarms}


@pytest.fixture
def client(fake_dynamodb, monkeypatch):
    store = QuotaStore(dynamodb=fake_dynamodb)
    fake_broker = FakeBroker()
    monkeypatch.setattr(gateway, "_store", store)
    monkeypatch.setattr(gateway, "_broker", fake_broker)
    monkeypatch.setattr(gateway, "_admin_key", "admin-secret")
    monkeypatch.setattr(gateway, "_emergency_key", "emergency-secret")
    monkeypatch.setattr(gateway, "_cloudwatch", None)
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
    assert broker.claims[0]["email"] == "alice@example.com"
    assert "exp" in broker.claims[0]
    assert store.get_user("alice").name == "alice@example.com"
    assert store.resolve_session(session_name_for("alice")) == "alice"


def test_reserved_internal_identity_prefixes_are_rejected(client):
    api, store, broker = client

    response = _vend(api, make_jwt("CONFIG#EMERGENCY_STOP"))

    assert response.status_code == 401
    assert "reserved internal prefix" in response.json()["error"]["message"]
    assert not store.emergency_stop_active()
    assert broker.users == []

    created = api.post(
        "/admin/users",
        json={
            "user_id": "REVOCATION#alice",
            "daily_usd": 1,
            "daily_input_tokens": 10,
            "daily_output_tokens": 10,
        },
        headers=ADMIN,
    )
    assert created.status_code == 400


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


def test_existing_credentials_are_not_revoked_by_current_status_block(client):
    api, store, broker = client

    issued = _vend(api, make_jwt("alice"))
    assert issued.status_code == 200
    assert issued.json()["expiration"] == "2026-08-18T12:15:00+00:00"

    store.set_user_status("alice", "blocked", "admin baseline")
    rejected = _vend(api, make_jwt("alice"))

    assert rejected.status_code == 403
    assert broker.users == ["alice"]
    # The control plane has no handle that mutates the already-returned set.
    assert issued.json()["aws_access_key_id"] == "ASIAFAKE"


def test_current_broker_allows_overlapping_credential_vends(client):
    api, _, broker = client

    first = _vend(api, make_jwt("alice"))
    second = _vend(api, make_jwt("alice"))

    assert first.status_code == second.status_code == 200
    assert broker.users == ["alice", "alice"]


def test_lease_mode_retries_fixed_lease_and_rejects_premature_new_id(
    client, monkeypatch
):
    api, _, broker = client
    monkeypatch.setattr(
        gateway,
        "settings",
        replace(
            gateway.settings,
            credential_enforcement_mode="lease",
            permission_lease_seconds=300,
        ),
    )
    token = make_jwt("alice")

    first = api.post(
        "/v1/credentials",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Quota-Lease-Id": "lease-a",
        },
    )
    retry = api.post(
        "/v1/credentials",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Quota-Lease-Id": "lease-a",
        },
    )
    premature = api.post(
        "/v1/credentials",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Quota-Lease-Id": "lease-b",
        },
    )

    assert first.status_code == retry.status_code == 200
    assert first.json()["lease_id"] == retry.json()["lease_id"] == "lease-a"
    assert first.json()["expiration"] == retry.json()["expiration"]
    assert first.json()["refresh_after"] == retry.json()["refresh_after"]
    assert first.json()["sts_expiration"] == "2026-08-18T12:15:00+00:00"
    assert broker.permission_deadlines[0] == broker.permission_deadlines[1]
    assert premature.status_code == 429
    assert premature.json()["error"]["type"] == "lease_not_refreshable"
    assert "X-Quota-Refresh-After" in premature.headers


def test_failed_sts_attempt_keeps_fixed_lease_for_same_id_retry(
    client, monkeypatch
):
    api, store, _ = client
    monkeypatch.setattr(
        gateway,
        "settings",
        replace(gateway.settings, credential_enforcement_mode="lease"),
    )

    class FailingOnceBroker(FakeBroker):
        def __init__(self):
            super().__init__()
            self.failed = False

        def vend(self, identity, *, permission_deadline=None):
            if not self.failed:
                self.failed = True
                raise BrokerError(500, "simulated STS failure")
            return super().vend(
                identity, permission_deadline=permission_deadline
            )

    broker = FailingOnceBroker()
    monkeypatch.setattr(gateway, "_broker", broker)
    token = make_jwt("alice")

    failed = api.post(
        "/v1/credentials",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Quota-Lease-Id": "lease-a",
        },
    )
    premature = api.post(
        "/v1/credentials",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Quota-Lease-Id": "lease-b",
        },
    )
    retry = api.post(
        "/v1/credentials",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Quota-Lease-Id": "lease-a",
        },
    )

    assert failed.status_code == 500
    assert premature.status_code == 429
    assert retry.status_code == 200
    assert retry.json()["lease_id"] == "lease-a"
    assert store.get_active_lease("alice").lease_id == "lease-a"


def test_post_reservation_gate_recheck_catches_concurrent_block(
    client, monkeypatch
):
    api, store, broker = client
    monkeypatch.setattr(
        gateway,
        "settings",
        replace(gateway.settings, credential_enforcement_mode="lease"),
    )
    original_reserve = store.reserve_lease

    def reserve_then_block(*args, **kwargs):
        reservation = original_reserve(*args, **kwargs)
        store.set_user_status("alice", "blocked", "race test")
        return reservation

    monkeypatch.setattr(store, "reserve_lease", reserve_then_block)
    response = api.post(
        "/v1/credentials",
        headers={
            "Authorization": f"Bearer {make_jwt('alice')}",
            "X-Quota-Lease-Id": "lease-a",
        },
    )

    assert response.status_code == 403
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
    assert body["enforcement"]["permission_lease_seconds"] == 300
    assert body["enforcement"]["refresh_overlap_seconds"] == 10
    assert body["enforcement"]["refresh_jitter_seconds"] == 5
    assert body["enforcement"]["vend_rate_limit_per_minute"] == 6
    assert body["enforcement"]["revocation_policy_shards"] == 19
    assert body["enforcement"]["revocation_reconcile_minutes"] == 5
    assert body["observability"] == {
        "source": "bedrock_model_invocation_logs",
        "delivery": "cloudwatch_logs_subscription",
        "metrics_namespace": "BedrockQuotaGateway",
        "detection_lag_metric": "DetectionLagMilliseconds",
    }
    assert "reconciler_interval_minutes" not in body


def test_operations_is_read_only_safe_and_reports_revocation_health(
    client, monkeypatch
):
    api, store, _ = client
    now = datetime.now(timezone.utc)
    alarm_names = {
        "emergency_failure": "emergency-failure-alarm",
        "emergency_dlq": "emergency-dlq-alarm",
        "revocation_failure": "revocation-failure-alarm",
        "revocation_overflow": "revocation-overflow-alarm",
        "revocation_dlq": "revocation-dlq-alarm",
        "revocation_iterator_age": "revocation-age-alarm",
    }
    monkeypatch.setattr(
        gateway,
        "settings",
        replace(
            gateway.settings,
            credential_enforcement_mode="revocation",
            vended_credential_ttl_seconds=3600,
            operations_alarm_names_json=json.dumps(alarm_names),
        ),
    )
    requested = store.set_emergency_desired(
        active=True,
        actor="emergency-shared-key",
        reason="sensitive incident detail",
        now=now - timedelta(minutes=2),
    )
    store.mark_emergency_applied(
        active=True,
        generation=requested["generation"],
        now=now - timedelta(minutes=1),
    )
    cloudwatch = FakeCloudWatch(
        results=[
            {
                "Id": "detectionlag",
                "StatusCode": "Complete",
                "Values": [1250.0],
                "Timestamps": [now - timedelta(minutes=1)],
            },
            {
                "Id": "revsyncsuccess",
                "StatusCode": "Complete",
                "Values": [1.0],
                "Timestamps": [now - timedelta(minutes=1)],
            },
            {
                "Id": "revsyncfailure",
                "StatusCode": "Complete",
                "Values": [],
                "Timestamps": [],
            },
            {
                "Id": "revoverflow",
                "StatusCode": "Complete",
                "Values": [],
                "Timestamps": [],
            },
            {
                "Id": "revokeddesired",
                "StatusCode": "Complete",
                "Values": [7.0],
                "Timestamps": [now - timedelta(minutes=1)],
            },
            {
                "Id": "emergencyfailure",
                "StatusCode": "Complete",
                "Values": [],
                "Timestamps": [],
            },
        ],
        alarms=[
            {
                "AlarmName": "emergency-failure-alarm",
                "StateValue": "OK",
                "StateUpdatedTimestamp": now - timedelta(minutes=1),
            },
            {
                "AlarmName": "emergency-dlq-alarm",
                "StateValue": "INSUFFICIENT_DATA",
                "StateUpdatedTimestamp": now - timedelta(minutes=3),
            },
            {
                "AlarmName": "revocation-failure-alarm",
                "StateValue": "OK",
                "StateUpdatedTimestamp": now - timedelta(minutes=1),
            },
            {
                "AlarmName": "revocation-overflow-alarm",
                "StateValue": "OK",
                "StateUpdatedTimestamp": now - timedelta(minutes=1),
            },
            {
                "AlarmName": "revocation-dlq-alarm",
                "StateValue": "OK",
                "StateUpdatedTimestamp": now - timedelta(minutes=1),
            },
            {
                "AlarmName": "revocation-age-alarm",
                "StateValue": "OK",
                "StateUpdatedTimestamp": now - timedelta(minutes=1),
            },
        ],
    )
    monkeypatch.setattr(gateway, "_cloudwatch", cloudwatch)

    assert api.get("/admin/operations").status_code == 403
    response = api.get("/admin/operations", headers=ADMIN)

    assert response.status_code == 200
    body = response.json()
    assert body["configuration"] == {
        "mode": "active_session_revocation",
        "credential_ttl_seconds": 3600,
        "permission_lease_seconds": 300,
        "permission_lease_enabled": False,
        "effective_permission_lease_seconds": None,
        "post_detection_fallback_seconds": 3600,
        "refresh_overlap_seconds": 10,
        "refresh_jitter_seconds": 5,
        "vend_rate_limit_per_minute": 6,
        "revocation_enabled": True,
        "revocation_policy_shards": 19,
        "revocation_policy_max_characters": 6144,
        "revocation_reconcile_minutes": 5,
    }
    assert body["emergency"]["state"] == "active"
    assert body["emergency"]["converged"] is True
    assert body["emergency"]["generation"] == 1
    assert body["metrics"]["detection_lag_p95_ms"] == 1250.0
    assert body["metrics"]["telemetry_status"] == "complete"
    assert body["metrics"]["revoked_identities_desired"] == 7
    assert body["metrics"]["reconciliation_status"] == "current"
    assert body["qualification"]["status"] == (
        "experimental_pending_propagation_isolation_probe"
    )
    assert body["cloudwatch"]["status"] == "available"
    assert {alarm["key"] for alarm in body["alarms"]} == set(alarm_names)
    assert next(
        alarm for alarm in body["alarms"] if alarm["key"] == "emergency_dlq"
    )["state"] == "INSUFFICIENT_DATA"
    serialized = json.dumps(body).lower()
    assert "sensitive incident detail" not in serialized
    assert "emergency-shared-key" not in serialized
    assert "secret" not in serialized
    assert cloudwatch.metric_calls and cloudwatch.alarm_calls


def test_operations_marks_partial_cloudwatch_evidence_unknown(
    client, monkeypatch
):
    api, _, _ = client
    now = datetime.now(timezone.utc)
    alarm_names = {
        "revocation_failure": "revocation-failure-alarm",
        "revocation_overflow": "revocation-overflow-alarm",
        "revocation_dlq": "revocation-dlq-alarm",
        "revocation_iterator_age": "revocation-age-alarm",
    }
    monkeypatch.setattr(
        gateway,
        "settings",
        replace(
            gateway.settings,
            credential_enforcement_mode="revocation",
            vended_credential_ttl_seconds=3600,
            operations_alarm_names_json=json.dumps(alarm_names),
        ),
    )
    results = [
        {
            "Id": query_id,
            "StatusCode": (
                "PartialData" if query_id == "revsyncfailure" else "Complete"
            ),
            "Values": [1.0] if query_id == "revsyncsuccess" else [],
            "Timestamps": (
                [now - timedelta(minutes=1)]
                if query_id == "revsyncsuccess"
                else []
            ),
        }
        for query_id in (
            "detectionlag",
            "revsyncsuccess",
            "revsyncfailure",
            "revoverflow",
            "revokeddesired",
            "emergencyfailure",
        )
    ]
    alarms = [
        {
            "AlarmName": alarm_name,
            "StateValue": "OK",
            "StateUpdatedTimestamp": now,
        }
        for alarm_name in alarm_names.values()
    ]
    monkeypatch.setattr(
        gateway,
        "_cloudwatch",
        FakeCloudWatch(results=results, alarms=alarms),
    )

    body = api.get("/admin/operations", headers=ADMIN).json()

    assert body["cloudwatch"]["status"] == "partial"
    assert body["metrics"]["reconciliation_status"] == "unknown"


def test_operations_gracefully_reports_unavailable_cloudwatch(
    client, monkeypatch
):
    api, _, _ = client
    error = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "denied"}},
        "GetMetricData",
    )
    monkeypatch.setattr(gateway, "_cloudwatch", FakeCloudWatch(error=error))

    response = api.get("/admin/operations", headers=ADMIN)

    assert response.status_code == 200
    body = response.json()
    assert body["cloudwatch"] == {
        "status": "unavailable",
        "error_code": "AccessDenied",
    }
    assert body["configuration"]["mode"] == "bounded_overspend"
    assert body["configuration"]["permission_lease_enabled"] is False
    assert body["configuration"]["effective_permission_lease_seconds"] is None
    assert body["emergency"]["state"] == "inactive"
    assert body["metrics"]["detection_lag_p95_ms"] is None
    assert body["metrics"]["reconciliation_status"] == "not_applicable"
    assert all(alarm["state"] == "UNAVAILABLE" for alarm in body["alarms"])


def test_admin_summary_distinguishes_permission_lease_mode(
    client, monkeypatch
):
    api, _, _ = client
    monkeypatch.setattr(
        gateway,
        "settings",
        replace(
            gateway.settings,
            credential_enforcement_mode="lease",
            permission_lease_seconds=60,
        ),
    )

    enforcement = api.get("/admin/summary", headers=ADMIN).json()[
        "enforcement"
    ]

    assert enforcement["mode"] == "permission_lease"
    assert enforcement["post_detection_fallback_seconds"] == 60

    monkeypatch.setattr(
        gateway,
        "_cloudwatch",
        FakeCloudWatch(
            error=ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "denied"}},
                "GetMetricData",
            )
        ),
    )
    operations = api.get("/admin/operations", headers=ADMIN).json()
    assert operations["configuration"]["permission_lease_enabled"] is True
    assert operations["configuration"][
        "effective_permission_lease_seconds"
    ] == 60


def test_emergency_stop_requires_break_glass_confirmation_and_gates_recovery(
    client,
):
    api, store, broker = client
    routine_admin = api.post(
        "/admin/emergency-stop",
        json={
            "action": "activate",
            "confirmation": "STOP_ALL_BEDROCK_SESSIONS",
            "reason": "exercise",
        },
        headers=ADMIN,
    )
    assert routine_admin.status_code == 403

    wrong = api.post(
        "/admin/emergency-stop",
        json={
            "action": "activate",
            "confirmation": "yes",
            "reason": "exercise",
        },
        headers=EMERGENCY,
    )
    assert wrong.status_code == 400
    assert wrong.json()["error"]["type"] == "confirmation_required"

    activated = api.post(
        "/admin/emergency-stop",
        json={
            "action": "activate",
            "confirmation": "STOP_ALL_BEDROCK_SESSIONS",
            "reason": "security exercise",
        },
        headers=EMERGENCY,
    )
    assert activated.status_code == 202
    assert activated.json()["state"] == "activating"
    assert activated.json()["actor"] == "emergency-shared-key"
    assert activated.json()["generation"] == 1
    assert _vend(api, make_jwt("alice")).status_code == 503
    assert broker.users == []

    repeated = api.post(
        "/admin/emergency-stop",
        json={
            "action": "activate",
            "confirmation": "STOP_ALL_BEDROCK_SESSIONS",
            "reason": "security exercise",
        },
        headers=EMERGENCY,
    )
    assert repeated.json()["idempotent"] is False
    assert repeated.json()["retry"] is True
    assert repeated.json()["generation"] == 2

    recovering = api.post(
        "/admin/emergency-stop",
        json={
            "action": "recover",
            "confirmation": "RESTORE_ALL_BEDROCK_SESSIONS",
            "reason": "exercise complete",
        },
        headers=EMERGENCY,
    )
    assert recovering.status_code == 202
    assert recovering.json()["state"] == "recovering"
    assert recovering.json()["generation"] == 3
    assert _vend(api, make_jwt("alice")).status_code == 503

    store.mark_emergency_applied(active=False)
    assert _vend(api, make_jwt("alice")).status_code == 200
    assert broker.users == ["alice"]


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
        {"daily_usd": 2, "reason": 123},
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


def test_duplicate_admin_create_preserves_existing_user(client):
    api, store, _ = client
    store.put_user("alice", "Original", 7, 700, 70)
    store.set_user_status("alice", "blocked", "auto: existing state")
    before = store.get_user("alice")

    response = api.post(
        "/admin/users",
        json={
            "user_id": "alice",
            "name": "Replacement",
            "daily_usd": 1,
            "daily_input_tokens": 1,
            "daily_output_tokens": 1,
        },
        headers={**ADMIN, "Idempotency-Key": "duplicate-create"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["type"] == "user_already_exists"
    assert store.get_user("alice") == before


def test_legacy_user_reads_as_version_zero_and_upgrades_on_first_mutation(client):
    api, store, _ = client
    store._users.put_item(  # noqa: SLF001 - seed an authentic legacy row
        Item={
            "user_id": "legacy",
            "name": "Legacy",
            "status": "active",
            "status_reason": "",
            "daily_usd_micro": MICRO,
            "daily_input_tokens": 100,
            "daily_output_tokens": 50,
        }
    )

    detail = api.get("/admin/users/legacy", headers=ADMIN)
    assert detail.status_code == 200
    assert detail.headers["etag"] == '"0"'
    assert detail.json()["user"]["limits"] == {
        "daily_usd": 1.0,
        "daily_input_tokens": 100,
        "daily_output_tokens": 50,
    }
    assert detail.json()["user"]["version"] == 0
    assert detail.json()["user"]["created_at"] is None
    assert detail.json()["user"]["updated_at"] is None
    assert detail.json()["user"]["status_origin"] == "legacy"

    updated = api.put(
        "/admin/users/legacy/limits",
        json={"daily_usd": 2},
        headers={**ADMIN, "Idempotency-Key": "legacy-upgrade"},
    )
    assert updated.status_code == 200
    assert updated.headers["etag"] == '"1"'
    assert updated.json()["user"]["version"] == 1
    assert updated.json()["user"]["created_at"] is None
    assert updated.json()["user"]["updated_at"]
    assert updated.json()["user"]["status_origin"] == "legacy"


def test_admin_mutations_enforce_version_and_durable_idempotency(client):
    api, store, _ = client
    created = api.post(
        "/admin/users",
        json={"user_id": "alice", "daily_usd": 1},
        headers={**ADMIN, "Idempotency-Key": "create-alice"},
    )
    assert created.status_code == 200
    assert created.headers["etag"] == '"1"'
    create_replay = api.post(
        "/admin/users",
        json={"user_id": "alice", "daily_usd": 1},
        headers={**ADMIN, "Idempotency-Key": "create-alice"},
    )
    assert create_replay.status_code == 200
    assert create_replay.json() == created.json()
    create_mismatch = api.post(
        "/admin/users",
        json={"user_id": "alice", "daily_usd": 9},
        headers={**ADMIN, "Idempotency-Key": "create-alice"},
    )
    assert create_mismatch.status_code == 409
    assert create_mismatch.json()["error"]["type"] == (
        "idempotency_conflict"
    )

    request_headers = {
        **ADMIN,
        "If-Match": '"1"',
        "Idempotency-Key": "limits-alice",
    }
    first = api.put(
        "/admin/users/alice/limits",
        json={"daily_usd": 2, "reason": "  Quarterly increase  "},
        headers=request_headers,
    )
    replay = api.put(
        "/admin/users/alice/limits",
        json={"daily_usd": 2, "reason": "Quarterly increase"},
        headers=request_headers,
    )
    assert first.status_code == replay.status_code == 200
    assert replay.json() == first.json()
    assert replay.headers["etag"] == first.headers["etag"] == '"2"'
    assert store.get_user("alice").version == 2
    events, _ = store.list_admin_audit_page(
        user_id="alice", limit=10
    )
    assert events[0]["reason"] == "Quarterly increase"

    mismatch = api.put(
        "/admin/users/alice/limits",
        json={"daily_usd": 2, "reason": "Different justification"},
        headers=request_headers,
    )
    assert mismatch.status_code == 409
    assert mismatch.json()["error"]["type"] == "idempotency_conflict"

    stale = api.put(
        "/admin/users/alice/limits",
        json={"daily_usd": 4},
        headers={
            **ADMIN,
            "If-Match": '"1"',
            "Idempotency-Key": "stale-limits",
        },
    )
    assert stale.status_code == 409
    assert stale.headers["etag"] == '"2"'
    assert stale.json()["error"]["type"] == "version_conflict"
    assert stale.json()["error"]["details"]["current_user"]["version"] == 2
    assert store.get_user("alice").daily_usd_micro == 2 * MICRO


@pytest.mark.parametrize(
    "reason_payload",
    ({}, {"reason": "   "}),
    ids=("omitted", "blank"),
)
def test_limit_reason_is_optional_and_uses_legacy_audit_fallback(
    client, reason_payload
):
    api, store, _ = client
    store.put_user("legacy", "Legacy", 1, 100, 50)

    response = api.put(
        "/admin/users/legacy/limits",
        json={"daily_output_tokens": 75, **reason_payload},
        headers={
            **ADMIN,
            "If-Match": '"1"',
            "Idempotency-Key": (
                f"legacy-limit-reason-{len(reason_payload)}"
            ),
        },
    )

    assert response.status_code == 200
    assert set(response.json()) == {"user_id", "updated", "limits", "user"}
    events, _ = store.list_admin_audit_page(
        user_id="legacy", limit=10
    )
    assert events[0]["reason"] == (
        "legacy: reason omitted by compatible admin client"
    )


def test_status_transaction_updates_user_revocation_audit_and_rolls_back_conflict(
    client,
):
    api, store, _ = client
    created = api.post(
        "/admin/users",
        json={"user_id": "alice"},
        headers={**ADMIN, "Idempotency-Key": "status-create"},
    )
    assert created.status_code == 200

    blocked = api.put(
        "/admin/users/alice/status",
        json={"status": "blocked", "reason": "operator request"},
        headers={
            **ADMIN,
            "If-Match": '"1"',
            "Idempotency-Key": "status-block",
        },
    )
    assert blocked.status_code == 200
    assert blocked.json()["user"]["version"] == 2
    assert blocked.json()["user"]["status_origin"] == "admin"
    revocation = store._users.get_item(  # noqa: SLF001
        Key={"user_id": "REVOCATION#alice"}
    )["Item"]
    assert revocation["desired_status"] == "blocked"
    events, _ = store.list_admin_audit_page(
        user_id="alice", limit=10
    )
    assert [event["event_type"] for event in events] == [
        "user.status.updated",
        "user.created",
    ]

    before_revocation = dict(revocation)
    before_events = list(events)
    stale = api.put(
        "/admin/users/alice/status",
        json={"status": "active", "reason": "stale request"},
        headers={
            **ADMIN,
            "If-Match": '"1"',
            "Idempotency-Key": "status-stale",
        },
    )
    assert stale.status_code == 409
    assert store._users.get_item(  # noqa: SLF001
        Key={"user_id": "REVOCATION#alice"}
    )["Item"] == before_revocation
    assert store.list_admin_audit_page(
        user_id="alice", limit=10
    )[0] == before_events


@pytest.mark.parametrize(
    "reason_payload",
    ({}, {"reason": "   "}),
    ids=("omitted", "blank"),
)
def test_status_reason_fallback_is_normalized_before_store_and_audit(
    client, reason_payload
):
    api, store, _ = client
    store.put_user("legacy", "Legacy", 1, 100, 50)

    response = api.put(
        "/admin/user/status",
        params={"user_id": "legacy"},
        json={"status": "blocked", **reason_payload},
        headers={
            **ADMIN,
            "If-Match": '"1"',
            "Idempotency-Key": (
                f"legacy-status-reason-{len(reason_payload)}"
            ),
        },
    )

    fallback = "legacy: reason omitted by compatible admin client"
    assert response.status_code == 200
    assert response.json()["reason"] == fallback
    assert response.json()["user"]["status_reason"] == fallback
    assert store.get_user("legacy").status_reason == fallback
    events, _ = store.list_admin_audit_page(user_id="legacy", limit=10)
    assert events[0]["reason"] == fallback


def test_status_reason_hash_is_canonical_for_trimmed_and_fallback_replays(
    client,
):
    api, store, _ = client
    for user_id in ("trimmed", "fallback"):
        store.put_user(user_id, user_id.title(), 1, 100, 50)

    trimmed_headers = {
        **ADMIN,
        "If-Match": '"1"',
        "Idempotency-Key": "status-trimmed-replay",
    }
    padded = api.put(
        "/admin/user/status",
        params={"user_id": "trimmed"},
        json={"status": "blocked", "reason": "  Policy review  "},
        headers=trimmed_headers,
    )
    trimmed = api.put(
        "/admin/user/status",
        params={"user_id": "trimmed"},
        json={"status": "blocked", "reason": "Policy review"},
        headers=trimmed_headers,
    )

    assert padded.status_code == trimmed.status_code == 200
    assert padded.json() == trimmed.json()
    assert padded.json()["reason"] == "Policy review"
    assert padded.headers["etag"] == trimmed.headers["etag"] == '"2"'
    assert store.get_user("trimmed").version == 2
    trimmed_events, _ = store.list_admin_audit_page(
        user_id="trimmed", limit=10
    )
    assert [event["event_type"] for event in trimmed_events] == [
        "user.status.updated"
    ]
    assert trimmed_events[0]["reason"] == "Policy review"

    fallback_headers = {
        **ADMIN,
        "If-Match": '"1"',
        "Idempotency-Key": "status-fallback-replay",
    }
    omitted = api.put(
        "/admin/user/status",
        params={"user_id": "fallback"},
        json={"status": "blocked"},
        headers=fallback_headers,
    )
    blank = api.put(
        "/admin/user/status",
        params={"user_id": "fallback"},
        json={"status": "blocked", "reason": "  "},
        headers=fallback_headers,
    )

    assert omitted.status_code == blank.status_code == 200
    assert omitted.json() == blank.json()
    assert store.get_user("fallback").version == 2
    fallback_events, _ = store.list_admin_audit_page(
        user_id="fallback", limit=10
    )
    assert [event["event_type"] for event in fallback_events] == [
        "user.status.updated"
    ]


def test_canonical_admin_routes_disambiguate_path_like_identities(client):
    api, store, _ = client
    user_ids = ("team", "team/audit", "team/usage", "team/usage-history")
    today = datetime.now(timezone.utc).date().isoformat()
    for index, user_id in enumerate(user_ids, start=1):
        created = api.post(
            "/admin/users",
            json={"user_id": user_id, "name": f"Identity {user_id}"},
            headers={
                **ADMIN,
                "Idempotency-Key": f"canonical-create-{index}",
            },
        )
        assert created.status_code == 200
        store._usage.put_item(  # noqa: SLF001
            Item={
                "user_id": user_id,
                "window": today,
                "cost_micro": index * MICRO,
                "input_tokens": index * 10,
                "output_tokens": index * 5,
                "requests": index,
            }
        )

    for index, user_id in enumerate(user_ids, start=1):
        detail = api.get(
            "/admin/user", params={"user_id": user_id}, headers=ADMIN
        )
        assert detail.status_code == 200
        assert detail.json()["user"]["user_id"] == user_id
        assert detail.json()["user"]["name"] == f"Identity {user_id}"

        usage = api.get(
            "/admin/user/usage",
            params={"user_id": user_id, "window": today},
            headers=ADMIN,
        )
        assert usage.status_code == 200
        assert usage.json()["user_id"] == user_id
        assert usage.json()["requests"] == index

        history = api.get(
            "/admin/user/usage-history",
            params={"user_id": user_id, "start": today, "end": today},
            headers=ADMIN,
        )
        assert history.status_code == 200
        assert history.json()["user_id"] == user_id
        assert history.json()["usage"][0]["user_id"] == user_id

        audit = api.get(
            "/admin/user/audit",
            params={"user_id": user_id},
            headers=ADMIN,
        )
        assert audit.status_code == 200
        assert audit.json()["user_id"] == user_id
        assert {event["user_id"] for event in audit.json()["events"]} == {
            user_id
        }

        limits = api.put(
            "/admin/user/limits",
            params={"user_id": user_id},
            json={"daily_usd": index + 10, "reason": "route regression"},
            headers={
                **ADMIN,
                "If-Match": detail.headers["etag"],
                "Idempotency-Key": f"canonical-limits-{index}",
            },
        )
        assert limits.status_code == 200
        assert limits.json()["user_id"] == user_id
        assert limits.json()["user"]["version"] == 2

        status = api.put(
            "/admin/user/status",
            params={"user_id": user_id},
            json={"status": "blocked", "reason": "route regression"},
            headers={
                **ADMIN,
                "If-Match": limits.headers["etag"],
                "Idempotency-Key": f"canonical-status-{index}",
            },
        )
        assert status.status_code == 200
        assert status.json()["user_id"] == user_id
        assert status.json()["user"]["version"] == 3

    for suffix in ("audit", "usage", "usage-history"):
        legacy = api.get(f"/admin/users/team/{suffix}", headers=ADMIN)
        assert legacy.status_code == 200
        assert legacy.json()["user"]["user_id"] == f"team/{suffix}"


def test_canonical_mutation_hash_binds_the_exact_query_identity(client):
    api, store, _ = client
    for user_id in ("team", "team/audit"):
        store.put_user(user_id, user_id, 1, 100, 50)
    headers = {
        **ADMIN,
        "If-Match": '"1"',
        "Idempotency-Key": "canonical-identity-bound",
    }

    first = api.put(
        "/admin/user/limits",
        params={"user_id": "team"},
        json={"daily_usd": 2, "reason": "identity binding"},
        headers=headers,
    )
    conflict = api.put(
        "/admin/user/limits",
        params={"user_id": "team/audit"},
        json={"daily_usd": 2, "reason": "identity binding"},
        headers=headers,
    )

    assert first.status_code == 200
    assert conflict.status_code == 409
    assert conflict.json()["error"]["type"] == "idempotency_conflict"
    assert store.get_user("team/audit").version == 1


@pytest.mark.parametrize(
    ("method", "route"),
    (
        ("get", "/admin/user"),
        ("get", "/admin/user/usage"),
        ("get", "/admin/user/usage-history"),
        ("get", "/admin/user/audit"),
        ("put", "/admin/user/limits"),
        ("put", "/admin/user/status"),
    ),
)
def test_canonical_user_id_must_be_valid_and_appear_once(
    client, method, route
):
    api, _, _ = client

    missing = api.request(method, route, headers=ADMIN)
    duplicate = api.request(
        method,
        f"{route}?user_id=team&user_id=team%2Faudit",
        headers=ADMIN,
    )
    reserved = api.request(
        method,
        route,
        params={"user_id": "CONFIG#internal"},
        headers=ADMIN,
    )

    for response in (missing, duplicate):
        assert response.status_code == 400
        assert response.json() == {
            "error": {
                "message": "user_id must be provided exactly once.",
                "type": "invalid_request_error",
                "code": "invalid_request_error",
            }
        }
    assert reserved.status_code == 400
    assert reserved.json() == {
        "error": {
            "message": "user identity uses a reserved internal prefix",
            "type": "invalid_request_error",
            "code": "invalid_request_error",
        }
    }


@pytest.mark.parametrize(
    ("method", "route"),
    (
        ("get", "/admin/users/CONFIG%23internal"),
        ("get", "/admin/users/CONFIG%23internal/usage"),
        ("get", "/admin/users/CONFIG%23internal/usage-history"),
        ("get", "/admin/users/CONFIG%23internal/audit"),
        ("put", "/admin/users/CONFIG%23internal/limits"),
        ("put", "/admin/users/CONFIG%23internal/status"),
    ),
)
def test_legacy_user_routes_share_reserved_identity_validation(
    client, method, route
):
    api, _, _ = client

    response = api.request(method, route, headers=ADMIN)

    assert response.status_code == 400
    assert response.json() == {
        "error": {
            "message": "user identity uses a reserved internal prefix",
            "type": "invalid_request_error",
            "code": "invalid_request_error",
        }
    }


def test_jwt_admin_audit_actor_uses_subject_with_tenant_quota_identity(
    client, monkeypatch
):
    import app.auth as auth_module
    from app.config import Settings

    api, store, broker = client
    monkeypatch.setenv("JWT_USER_CLAIM", "tenant_id")
    monkeypatch.setenv("ADMIN_JWT_CLAIM", "groups")
    monkeypatch.setenv("ADMIN_JWT_VALUE", "quota-admins")
    fresh = Settings()
    monkeypatch.setattr(gateway, "settings", fresh)
    monkeypatch.setattr(auth_module, "settings", fresh)
    token = make_jwt(
        "human-admin",
        tenant_id="tenant-acme",
        groups=["quota-admins"],
    )

    vended = _vend(api, token)
    assert vended.status_code == 200
    assert vended.json()["user_id"] == "tenant-acme"
    assert broker.users == ["tenant-acme"]

    detail = api.get(
        "/admin/users/tenant-acme",
        headers={"X-Quota-User-Token": token},
    )
    updated = api.put(
        "/admin/users/tenant-acme/limits",
        json={"daily_usd": 2, "reason": "Tenant quota review"},
        headers={
            "X-Quota-User-Token": token,
            "If-Match": detail.headers["etag"],
            "Idempotency-Key": "tenant-limit-review",
        },
    )

    assert updated.status_code == 200
    events, _ = store.list_admin_audit_page(
        user_id="tenant-acme", limit=10
    )
    assert events[0]["actor"] == "human-admin"
    assert events[0]["auth_method"] == "jwt"


def test_admin_audit_attributes_verified_actor_without_credentials(
    client, monkeypatch
):
    api, store, _ = client
    _enable_admin_jwt(monkeypatch)
    token = make_jwt("admin-subject", groups=["quota-admins"])
    response = api.post(
        "/admin/users",
        json={"user_id": "jwt-user"},
        headers={
            "X-Quota-User-Token": token,
            "Idempotency-Key": "jwt-create",
        },
    )
    assert response.status_code == 200
    events = api.get(
        "/admin/users/jwt-user/audit",
        headers={"X-Quota-User-Token": token},
    ).json()["events"]
    assert events[0]["actor"] == "admin-subject"
    assert events[0]["auth_method"] == "jwt"

    raw_audit = json.dumps(
        list(store._admin_audit.items.values()),  # noqa: SLF001
        default=str,
    )
    assert token not in raw_audit
    assert "admin-secret" not in raw_audit

    shared = api.post(
        "/admin/users",
        json={"user_id": "shared-user"},
        headers={**ADMIN, "Idempotency-Key": "shared-create"},
    )
    assert shared.status_code == 200
    shared_events = api.get(
        "/admin/users/shared-user/audit", headers=ADMIN
    ).json()["events"]
    assert shared_events[0]["actor"] == "admin-shared-key"
    assert shared_events[0]["auth_method"] == "shared-key"


def test_detail_usage_history_audit_pagination_and_user_filters(client):
    api, store, _ = client
    for user_id, name in (("alice", "Alice Smith"), ("bob", "Bob Jones")):
        assert api.post(
            "/admin/users",
            json={"user_id": user_id, "name": name},
            headers={**ADMIN, "Idempotency-Key": f"create-{user_id}"},
        ).status_code == 200
    assert api.put(
        "/admin/users/bob/status",
        json={"status": "blocked", "reason": "test"},
        headers={
            **ADMIN,
            "If-Match": '"1"',
            "Idempotency-Key": "block-bob",
        },
    ).status_code == 200

    today = datetime.now(timezone.utc).date()
    for offset, requests in ((0, 3), (1, 2), (2, 1)):
        window = (today - timedelta(days=offset)).isoformat()
        store._usage.put_item(  # noqa: SLF001
            Item={
                "user_id": "alice",
                "window": window,
                "cost_micro": requests * MICRO,
                "input_tokens": requests * 10,
                "output_tokens": requests * 5,
                "requests": requests,
            }
        )

    detail = api.get("/admin/users/alice", headers=ADMIN)
    assert detail.status_code == 200
    assert detail.headers["etag"] == '"1"'
    assert detail.json()["user"]["name"] == "Alice Smith"

    first_history = api.get(
        "/admin/users/alice/usage-history?limit=2", headers=ADMIN
    ).json()
    assert [row["requests"] for row in first_history["usage"]] == [3, 2]
    assert first_history["next_cursor"]
    second_history = api.get(
        "/admin/users/alice/usage-history",
        params={"limit": 2, "cursor": first_history["next_cursor"]},
        headers=ADMIN,
    ).json()
    assert [row["requests"] for row in second_history["usage"]] == [1]

    oldest_invalid = (today - timedelta(days=366)).isoformat()
    invalid = api.get(
        "/admin/users/alice/usage-history",
        params={"start": oldest_invalid},
        headers=ADMIN,
    )
    assert invalid.status_code == 400
    assert invalid.json()["error"]["type"] == "usage_range_outside_retention"

    first_audit = api.get(
        "/admin/audit", params={"user_id": "bob", "limit": 1}, headers=ADMIN
    ).json()
    assert first_audit["events"][0]["event_type"] == "user.status.updated"
    assert first_audit["next_cursor"]
    second_audit = api.get(
        "/admin/audit",
        params={
            "user_id": "bob",
            "limit": 1,
            "cursor": first_audit["next_cursor"],
        },
        headers=ADMIN,
    ).json()
    assert second_audit["events"][0]["event_type"] == "user.created"

    blocked = api.get(
        "/admin/users", params={"status": "blocked"}, headers=ADMIN
    ).json()["users"]
    assert [user["user_id"] for user in blocked] == ["bob"]
    searched = api.get(
        "/admin/users", params={"query": "smith"}, headers=ADMIN
    ).json()["users"]
    assert [user["user_id"] for user in searched] == ["alice"]


def test_user_listing_skips_sentinel_rows_without_returning_an_empty_page(client):
    api, store, _ = client
    store._users.put_item(  # noqa: SLF001
        Item={"user_id": "CONFIG#FIRST", "state": "internal"}
    )
    store.put_user("z-user", "Zed", 1, 10, 10)

    page = api.get("/admin/users?limit=1", headers=ADMIN).json()

    assert [user["user_id"] for user in page["users"]] == ["z-user"]


def test_admin_principal_is_derived_from_one_verified_jwt(client, monkeypatch):
    api, store, _ = client
    monkeypatch.setattr(
        gateway,
        "settings",
        replace(
            gateway.settings,
            admin_jwt_claim="groups",
            admin_jwt_value="quota-admins",
        ),
    )

    class CountingVerifier:
        def __init__(self):
            self.calls = 0

        def verify(self, token):
            self.calls += 1
            assert token == "verified-admin-token"
            return gateway.Identity(
                user_id="verified-admin",
                claims={"groups": ["quota-admins"]},
            )

    counting = CountingVerifier()
    monkeypatch.setattr(gateway, "_verifier", counting)

    response = api.post(
        "/admin/users",
        json={"user_id": "single-verification"},
        headers={
            "X-Quota-User-Token": "verified-admin-token",
            "Idempotency-Key": "single-verification",
        },
    )

    assert response.status_code == 200
    assert counting.calls == 1
    events, _ = store.list_admin_audit_page(
        user_id="single-verification", limit=10
    )
    assert events[0]["actor"] == "verified-admin"


def test_path_like_detail_identity_wins_over_subresource_suffix(client):
    api, _, _ = client
    for suffix in ("audit", "usage", "usage-history"):
        user_id = f"team/{suffix}"
        assert api.post(
            "/admin/users",
            json={"user_id": user_id, "name": f"Identity {suffix}"},
            headers={
                **ADMIN,
                "Idempotency-Key": f"create-suffix-{suffix}",
            },
        ).status_code == 200

        detail = api.get(f"/admin/users/{user_id}", headers=ADMIN)

        assert detail.status_code == 200
        assert detail.json()["user"]["user_id"] == user_id
        assert detail.json()["user"]["name"] == f"Identity {suffix}"


def test_cursors_are_schema_checked_and_bound_to_the_original_query(client):
    api, store, _ = client
    for user_id in ("alice", "bob"):
        assert api.post(
            "/admin/users",
            json={"user_id": user_id},
            headers={**ADMIN, "Idempotency-Key": f"cursor-{user_id}"},
        ).status_code == 200
    first_page = api.get("/admin/users?limit=1", headers=ADMIN).json()
    cursor = first_page["next_cursor"]
    assert cursor

    malformed = api.get(
        "/admin/users",
        params={"cursor": json.dumps({"user_id": "alice"})},
        headers=ADMIN,
    )
    rebound = api.get(
        "/admin/users",
        params={"cursor": cursor, "status": "blocked"},
        headers=ADMIN,
    )
    assert malformed.status_code == 400
    assert rebound.status_code == 400

    today = datetime.now(timezone.utc).date()
    for user_id in ("alice", "bob"):
        for offset in (0, 1):
            store._usage.put_item(  # noqa: SLF001
                Item={
                    "user_id": user_id,
                    "window": (today - timedelta(days=offset)).isoformat(),
                    "requests": 1,
                }
            )
    history = api.get(
        "/admin/users/alice/usage-history?limit=1", headers=ADMIN
    ).json()
    reused = api.get(
        "/admin/users/bob/usage-history",
        params={"limit": 1, "cursor": history["next_cursor"]},
        headers=ADMIN,
    )
    assert reused.status_code == 400


def test_non_conditional_transaction_cancellation_is_retryable_not_version_conflict(
    client, monkeypatch
):
    api, store, _ = client
    store.put_user("alice", "Alice", 1, 100, 50)

    class CanceledClient:
        def transact_write_items(self, **kwargs):
            raise ClientError(
                {
                    "Error": {
                        "Code": "TransactionCanceledException",
                        "Message": "transaction conflict",
                    },
                    "CancellationReasons": [
                        {"Code": "TransactionConflict"},
                        {"Code": "None"},
                        {"Code": "None"},
                    ],
                },
                "TransactWriteItems",
            )

    monkeypatch.setattr(store, "_client", CanceledClient())

    response = api.put(
        "/admin/users/alice/limits",
        json={"daily_usd": 2},
        headers={
            **ADMIN,
            "If-Match": '"1"',
            "Idempotency-Key": "service-cancel",
        },
    )

    assert response.status_code == 503
    assert response.json()["error"]["type"] == "transaction_unavailable"
    assert response.headers["retry-after"] == "1"
    assert store.get_user("alice").version == 1
    assert store.list_admin_audit_page(user_id="alice", limit=10)[0] == []
