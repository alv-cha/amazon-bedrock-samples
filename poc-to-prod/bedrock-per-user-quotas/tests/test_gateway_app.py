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
