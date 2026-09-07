from datetime import datetime, timedelta, timezone

import pytest

from app.quota import (
    MICRO,
    LeaseNotRefreshable,
    LeaseRateLimited,
    QuotaStore,
    current_window,
)


def _put_usage(store: QuotaStore, user_id: str, **values) -> None:
    store._usage.put_item(  # noqa: SLF001 - focused store test
        Item={
            "user_id": user_id,
            "window": current_window(),
            "cost_micro": values.get("cost_micro", 0),
            "input_tokens": values.get("input_tokens", 0),
            "output_tokens": values.get("output_tokens", 0),
            "requests": values.get("requests", 0),
        }
    )


def test_get_or_provision_creates_defaults_without_resetting_existing(
    fake_dynamodb,
):
    store = QuotaStore(dynamodb=fake_dynamodb)
    created = store.get_or_provision_user("new", name="New User")
    assert created.name == "New User"
    assert created.daily_usd_micro > 0

    store.set_user_limits("new", daily_input_tokens=123)
    assert store.get_or_provision_user("new").daily_input_tokens == 123


def test_zero_limits_disable_individual_dimensions(fake_dynamodb):
    store = QuotaStore(dynamodb=fake_dynamodb)
    store.put_user("unlimited", "Unlimited", 0, 0, 0)
    user = store.get_user("unlimited")
    _put_usage(
        store,
        "unlimited",
        cost_micro=999 * MICRO,
        input_tokens=999,
        output_tokens=999,
    )
    assert not store.is_over_budget(user)


def test_each_configured_dimension_blocks_at_exact_limit(fake_dynamodb):
    dimensions = [
        {"cost_micro": MICRO},
        {"input_tokens": 100},
        {"output_tokens": 50},
    ]
    for index, usage in enumerate(dimensions):
        store = QuotaStore(dynamodb=fake_dynamodb)
        user_id = f"user-{index}"
        store.put_user(user_id, user_id, 1.0, 100, 50)
        _put_usage(store, user_id, **usage)
        assert store.is_over_budget(store.get_user(user_id))


def test_positive_sub_micro_budget_is_not_unlimited(fake_dynamodb):
    store = QuotaStore(dynamodb=fake_dynamodb)
    store.put_user("tiny", "Tiny", 0.0000004, 0, 0)
    assert store.get_user("tiny").daily_usd_micro == 1


def test_usd_limits_round_to_nearest_micro_without_float_truncation(
    fake_dynamodb,
):
    store = QuotaStore(dynamodb=fake_dynamodb)
    store.put_user("alice", "Alice", 1.000044, 0, 0)
    assert store.get_user("alice").daily_usd_micro == 1_000_044

    store.set_user_limits("alice", daily_usd=2.000044)
    assert store.get_user("alice").daily_usd_micro == 2_000_044


def test_session_mapping_preserves_full_identity_and_has_ttl(fake_dynamodb):
    store = QuotaStore(dynamodb=fake_dynamodb)
    store.put_user("auth0|full-subject", "Subject", 1, 10, 10)
    store.record_session("safe-session", "auth0|full-subject")
    assert store.resolve_session("safe-session") == "auth0|full-subject"
    item = fake_dynamodb.Table("users-test").get_item(
        Key={"user_id": "SESSION#safe-session"}
    )["Item"]
    assert item["expires_at"] > int(datetime.now(timezone.utc).timestamp())
    assert [user.user_id for user in store.list_users()] == [
        "auth0|full-subject"
    ]


def test_logical_lease_retry_never_extends_fixed_deadline(fake_dynamodb):
    now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    store = QuotaStore(
        dynamodb=fake_dynamodb,
        lease_seconds=300,
        refresh_overlap_seconds=10,
        refresh_jitter_seconds=5,
        vend_rate_limit_per_minute=6,
        jitter_fn=lambda maximum: maximum,
    )
    store.put_user("alice", "Alice", 1, 10, 10)

    first = store.reserve_lease("alice", "lease-a", now=now)
    retry = store.reserve_lease(
        "alice", "lease-a", now=now + timedelta(seconds=20)
    )

    assert first.created
    assert not retry.created
    assert retry.lease_id == first.lease_id
    assert retry.generation == first.generation == 1
    assert retry.expires_at == first.expires_at == now + timedelta(seconds=300)
    assert retry.refresh_after == now + timedelta(seconds=295)

    with pytest.raises(LeaseNotRefreshable) as error:
        store.reserve_lease(
            "alice", "lease-b", now=now + timedelta(seconds=20)
        )
    assert error.value.retry_after == first.refresh_after

    replacement = store.reserve_lease(
        "alice", "lease-b", now=first.refresh_after
    )
    assert replacement.created
    assert replacement.generation == 2
    assert replacement.expires_at > first.expires_at


def test_vend_rate_limit_counts_retries_and_resets_next_minute(fake_dynamodb):
    now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    store = QuotaStore(
        dynamodb=fake_dynamodb,
        lease_seconds=60,
        refresh_overlap_seconds=10,
        refresh_jitter_seconds=0,
        vend_rate_limit_per_minute=2,
        jitter_fn=lambda maximum: 0,
    )
    store.put_user("alice", "Alice", 1, 10, 10)

    store.reserve_lease("alice", "lease-a", now=now)
    store.reserve_lease("alice", "lease-a", now=now + timedelta(seconds=1))
    with pytest.raises(LeaseRateLimited) as error:
        store.reserve_lease(
            "alice", "lease-a", now=now + timedelta(seconds=2)
        )
    assert error.value.retry_after == now + timedelta(minutes=1)

    replacement = store.reserve_lease(
        "alice", "lease-b", now=now + timedelta(minutes=1)
    )
    assert replacement.created


def test_reserved_lease_is_never_rolled_back_after_publish(fake_dynamodb):
    now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    store = QuotaStore(
        dynamodb=fake_dynamodb,
        lease_seconds=60,
        refresh_overlap_seconds=10,
        refresh_jitter_seconds=0,
        vend_rate_limit_per_minute=6,
        jitter_fn=lambda maximum: 0,
    )
    store.put_user("alice", "Alice", 1, 10, 10)
    first = store.reserve_lease("alice", "lease-a", now=now)

    retry = store.reserve_lease(
        "alice", "lease-a", now=now + timedelta(seconds=1)
    )
    assert retry.expires_at == first.expires_at
    with pytest.raises(LeaseNotRefreshable):
        store.reserve_lease(
            "alice", "lease-b", now=now + timedelta(seconds=2)
        )
    assert store.get_active_lease("alice").lease_id == "lease-a"


def test_emergency_stop_state_keeps_vending_closed_through_recovery(
    fake_dynamodb,
):
    now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    store = QuotaStore(dynamodb=fake_dynamodb)
    assert not store.emergency_stop_active()

    activating = store.set_emergency_desired(
        active=True,
        actor="admin@example.com",
        reason="security exercise",
        now=now,
    )
    assert activating["state"] == "activating"
    assert store.emergency_stop_active()
    store.mark_emergency_applied(active=True, now=now + timedelta(seconds=1))
    assert store.get_emergency_state()["state"] == "active"

    recovering = store.set_emergency_desired(
        active=False,
        actor="admin@example.com",
        reason="exercise complete",
        now=now + timedelta(seconds=2),
    )
    assert recovering["state"] == "recovering"
    assert store.emergency_stop_active()
    store.mark_emergency_applied(active=False, now=now + timedelta(seconds=3))
    assert not store.emergency_stop_active()
    assert store.list_users() == []


def test_auto_block_reactivates_only_when_current_window_is_under_quota(
    fake_dynamodb,
):
    store = QuotaStore(dynamodb=fake_dynamodb)
    store.put_user("alice", "Alice", 1.0, 100, 50)
    store.set_user_status("alice", "blocked", "auto: quota exhausted yesterday")

    refreshed = store.refresh_auto_status(store.get_user("alice"))
    assert refreshed.active

    store.set_user_status("alice", "blocked", "auto: quota exhausted today")
    _put_usage(store, "alice", cost_micro=MICRO)
    still_blocked = store.refresh_auto_status(store.get_user("alice"))
    assert not still_blocked.active


def test_manual_block_is_never_auto_reactivated(fake_dynamodb):
    store = QuotaStore(dynamodb=fake_dynamodb)
    store.put_user("alice", "Alice", 1.0, 100, 50)
    store.set_user_status("alice", "blocked", "admin API")
    assert not store.refresh_auto_status(store.get_user("alice")).active
    event = fake_dynamodb.Table("users-test").get_item(
        Key={"user_id": "REVOCATION#alice"}
    )["Item"]
    assert event["desired_status"] == "blocked"
    assert store.list_users()[0].user_id == "alice"


def test_daily_windows_are_isolated(fake_dynamodb):
    store = QuotaStore(dynamodb=fake_dynamodb)
    store._usage.put_item(  # noqa: SLF001
        Item={
            "user_id": "alice",
            "window": "1999-01-01",
            "cost_micro": 10,
            "input_tokens": 20,
            "output_tokens": 30,
            "requests": 1,
        }
    )
    assert store.get_window_usage("alice")["requests"] == 0
    assert store.get_window_usage("alice", "1999-01-01")["input_tokens"] == 20


def test_user_pagination_excludes_session_rows(fake_dynamodb):
    store = QuotaStore(dynamodb=fake_dynamodb)
    for index in range(3):
        store.put_user(f"u{index}", f"U{index}", 1, 10, 10)
    store.record_session("session", "u0")

    seen: set[str] = set()
    cursor = None
    while True:
        users, cursor = store.list_users_page(limit=2, cursor=cursor)
        seen.update(user.user_id for user in users)
        if not cursor:
            break
    assert seen == {"u0", "u1", "u2"}


def test_concurrent_admin_create_wins_over_auto_provision(fake_dynamodb):
    store = QuotaStore(dynamodb=fake_dynamodb)
    table = fake_dynamodb.Table("users-test")
    original_put = table.put_item
    raced = False

    def put_with_admin_winner(**kwargs):
        nonlocal raced
        item = kwargs["Item"]
        if item.get("user_id") == "race" and not raced:
            raced = True
            original_put(
                Item={
                    "user_id": "race",
                    "name": "Admin Winner",
                    "status": "blocked",
                    "status_reason": "created concurrently",
                    "daily_usd_micro": 9 * MICRO,
                    "daily_input_tokens": 900,
                    "daily_output_tokens": 90,
                    "version": 1,
                    "created_at": "2030-01-01T00:00:00+00:00",
                    "updated_at": "2030-01-01T00:00:00+00:00",
                    "status_origin": "admin",
                }
            )
        return original_put(**kwargs)

    table.put_item = put_with_admin_winner

    winner = store.get_or_provision_user("race", name="Auto Default")

    assert winner.name == "Admin Winner"
    assert winner.status == "blocked"
    assert winner.daily_usd_micro == 9 * MICRO
    assert winner.status_origin == "admin"


def test_automatic_status_upgrades_legacy_version_but_bookkeeping_does_not(
    fake_dynamodb,
):
    store = QuotaStore(dynamodb=fake_dynamodb)
    store._users.put_item(  # noqa: SLF001
        Item={
            "user_id": "legacy",
            "name": "Legacy",
            "status": "active",
            "daily_usd_micro": MICRO,
            "daily_input_tokens": 100,
            "daily_output_tokens": 50,
        }
    )

    store.record_session("legacy-session", "legacy")
    assert store.get_user("legacy").version == 0

    store.set_user_status("legacy", "blocked", "auto: quota exhausted")
    updated = store.get_user("legacy")
    assert updated.version == 1
    assert updated.status_origin == "automatic"
    assert updated.updated_at

    store.record_session("legacy-session-2", "legacy")
    assert store.get_user("legacy").version == 1


def test_stale_automatic_refresh_cannot_override_newer_admin_status(
    fake_dynamodb,
):
    store = QuotaStore(dynamodb=fake_dynamodb)
    store.put_user("alice", "Alice", 1, 100, 50)
    store.set_user_status("alice", "blocked", "auto: old automatic block")
    stale_automatic = store.get_user("alice")
    assert stale_automatic.status_origin == "automatic"

    store.update_admin_status(
        "alice",
        "blocked",
        "auto: operator-authored reason",
        expected_version=stale_automatic.version,
        actor="admin-shared-key",
        auth_method="shared-key",
        idempotency_key="manual-wins",
        request_hash="manual-wins-hash",
    )

    refreshed = store.refresh_auto_status(stale_automatic)

    assert not refreshed.active
    assert refreshed.status_origin == "admin"
    assert refreshed.status_reason == "auto: operator-authored reason"
    assert refreshed.version == stale_automatic.version + 1


class _WireShortCircuit(Exception):
    """Raised by the before-send hook after capturing the signed request."""


def test_transaction_client_sends_typed_values_unmodified(monkeypatch):
    """Transactions must go through a genuine low-level DynamoDB client.

    A boto3 *resource* meta client carries the document-interface transform,
    which re-serializes TypeSerializer output into nested maps ({"S": ...}
    becomes {"M": {"S": {"S": ...}}}). DynamoDB then rejects the item key
    with a schema ValidationError and every admin write surfaces as a 503.
    The in-memory fake models a low-level client, so only a wire-shape
    assertion against the real boto3 client can catch this regression.
    """
    import json as jsonlib

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")

    store = QuotaStore()  # real boto3 resource + transaction client
    captured: dict = {}

    def capture(request, **_kwargs):
        captured["body"] = request.body
        raise _WireShortCircuit()

    store._client.meta.events.register_first(  # noqa: SLF001
        "before-send.dynamodb.TransactWriteItems", capture
    )
    typed_item = store._serialize({"user_id": "alice", "version": 1})  # noqa: SLF001
    with pytest.raises(_WireShortCircuit):
        store._client.transact_write_items(  # noqa: SLF001
            TransactItems=[{"Put": {"TableName": "probe", "Item": typed_item}}]
        )

    sent = jsonlib.loads(captured["body"])["TransactItems"][0]["Put"]["Item"]
    assert sent["user_id"] == {"S": "alice"}
    assert sent["version"] == {"N": "1"}
