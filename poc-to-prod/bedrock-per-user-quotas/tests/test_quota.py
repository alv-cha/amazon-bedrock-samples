from datetime import datetime, timezone

from app.quota import MICRO, QuotaStore, current_window


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


def test_session_mapping_preserves_full_identity_and_has_ttl(fake_dynamodb):
    store = QuotaStore(dynamodb=fake_dynamodb)
    store.record_session("safe-session", "auth0|full-subject")
    assert store.resolve_session("safe-session") == "auth0|full-subject"
    item = fake_dynamodb.Table("users-test").get_item(
        Key={"user_id": "SESSION#safe-session"}
    )["Item"]
    assert item["expires_at"] > int(datetime.now(timezone.utc).timestamp())
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
