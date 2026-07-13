import os
from datetime import datetime, timezone

import handler as reconciler

MICRO = 1_000_000
WINDOW = datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _seed_user(db, user_id, status="active", reason="", usd_limit=1.0):
    db.Table(os.environ["USERS_TABLE"]).put_item(Item={
        "user_id": user_id, "status": status, "status_reason": reason,
        "daily_usd_micro": int(usd_limit * MICRO),
        "daily_input_tokens": 1_000_000, "daily_output_tokens": 1_000_000,
        "name": user_id,
    })


def _seed_usage(db, user_id, cost_usd):
    db.Table(os.environ["USAGE_TABLE"]).put_item(Item={
        "user_id": user_id, "window": WINDOW,
        "cost_micro": int(cost_usd * MICRO),
        "input_tokens": 10, "output_tokens": 10, "requests": 1,
    })


def test_blocks_user_over_budget(fake_dynamodb, fake_sns, monkeypatch):
    monkeypatch.setenv("SNS_TOPIC_ARN", "arn:fake")
    _seed_user(fake_dynamodb, "over", usd_limit=1.0)
    _seed_usage(fake_dynamodb, "over", cost_usd=1.5)

    result = reconciler.handler({}, None, dynamodb=fake_dynamodb, sns=fake_sns)

    assert result["blocked"] == ["over"]
    item = fake_dynamodb.Table(os.environ["USERS_TABLE"]).get_item(
        Key={"user_id": "over"})["Item"]
    assert item["status"] == "blocked"
    assert item["status_reason"].startswith("auto:")
    assert any("BLOCKED" in p["Subject"] for p in fake_sns.published)


def test_unblocks_auto_blocked_user_after_reset(fake_dynamodb, fake_sns, monkeypatch):
    monkeypatch.setenv("SNS_TOPIC_ARN", "arn:fake")
    _seed_user(fake_dynamodb, "fresh", status="blocked", reason="auto: over budget")
    # no usage this window -> under budget

    result = reconciler.handler({}, None, dynamodb=fake_dynamodb, sns=fake_sns)

    assert result["unblocked"] == ["fresh"]
    item = fake_dynamodb.Table(os.environ["USERS_TABLE"]).get_item(
        Key={"user_id": "fresh"})["Item"]
    assert item["status"] == "active"


def test_never_unblocks_admin_blocked_user(fake_dynamodb, fake_sns):
    _seed_user(fake_dynamodb, "banned", status="blocked", reason="admin API")

    result = reconciler.handler({}, None, dynamodb=fake_dynamodb, sns=fake_sns)

    assert result["unblocked"] == []
    item = fake_dynamodb.Table(os.environ["USERS_TABLE"]).get_item(
        Key={"user_id": "banned"})["Item"]
    assert item["status"] == "blocked"


def test_warns_at_threshold(fake_dynamodb, fake_sns, monkeypatch):
    monkeypatch.setenv("SNS_TOPIC_ARN", "arn:fake")
    _seed_user(fake_dynamodb, "hot", usd_limit=1.0)
    _seed_usage(fake_dynamodb, "hot", cost_usd=0.85)

    result = reconciler.handler({}, None, dynamodb=fake_dynamodb, sns=fake_sns)

    assert result["warned"] == ["hot"]
    assert result["blocked"] == []
    assert any("WARNING" in p["Subject"] for p in fake_sns.published)


def test_quiet_when_under_budget(fake_dynamodb, fake_sns):
    _seed_user(fake_dynamodb, "calm", usd_limit=1.0)
    _seed_usage(fake_dynamodb, "calm", cost_usd=0.1)

    result = reconciler.handler({}, None, dynamodb=fake_dynamodb, sns=fake_sns, logs=None)

    assert result["window"] == WINDOW
    assert result["blocked"] == [] and result["unblocked"] == [] and result["warned"] == []
    # No INVOCATION_LOG_GROUP configured in tests -> ingestion is a no-op.
    assert result["metering"] == {"metered_users": 0, "unresolved_sessions": []}
    assert fake_sns.published == []
