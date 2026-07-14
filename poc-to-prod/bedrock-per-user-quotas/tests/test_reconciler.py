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
    """Seed PROXY-side usage (the reserve/settle ADD counters)."""
    db.Table(os.environ["USAGE_TABLE"]).put_item(Item={
        "user_id": user_id, "window": WINDOW,
        "cost_micro": int(cost_usd * MICRO),
        "input_tokens": 10, "output_tokens": 10, "requests": 1,
    })


def _seed_metered_usage(db, user_id, cost_usd):
    """Seed the post-ingest state for NATIVE-vended usage: under the unified-
    counter model the reconciler ADDs native spend into the SAME cost_micro
    the proxy uses, and records what it applied in metered_applied_*."""
    micro = int(cost_usd * MICRO)
    db.Table(os.environ["USAGE_TABLE"]).put_item(Item={
        "user_id": user_id, "window": WINDOW,
        "cost_micro": micro, "input_tokens": 10, "output_tokens": 10, "requests": 1,
        "metered_applied_cost_micro": micro, "metered_applied_input_tokens": 10,
        "metered_applied_output_tokens": 10, "metered_applied_requests": 1,
    })


class _FakeLogs:
    """Minimal CloudWatch Logs client returning one canned Insights result
    row per (session, model), enough to drive reconciler._ingest_usage."""

    def __init__(self, rows):
        self._rows = rows

    def start_query(self, **kwargs):
        return {"queryId": "q1"}

    def get_query_results(self, queryId):  # noqa: N803 (boto3 API)
        results = []
        for r in self._rows:
            results.append([{"field": k, "value": str(v)} for k, v in r.items()])
        return {"status": "Complete", "results": results}


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

    second = reconciler.handler({}, None, dynamodb=fake_dynamodb, sns=fake_sns)
    assert second["warned"] == []
    assert len(fake_sns.published) == 1


def test_session_map_rows_are_not_scanned_as_users(fake_dynamodb):
    users = fake_dynamodb.Table(os.environ["USERS_TABLE"])
    _seed_user(fake_dynamodb, "real-user")
    users.put_item(Item={"user_id": "SESSION#abc", "maps_to": "real-user"})

    assert [u["user_id"] for u in reconciler._scan_users(users)] == ["real-user"]


def test_quiet_when_under_budget(fake_dynamodb, fake_sns):
    _seed_user(fake_dynamodb, "calm", usd_limit=1.0)
    _seed_usage(fake_dynamodb, "calm", cost_usd=0.1)

    result = reconciler.handler({}, None, dynamodb=fake_dynamodb, sns=fake_sns, logs=None)

    assert result["window"] == WINDOW
    assert result["blocked"] == [] and result["unblocked"] == [] and result["warned"] == []
    # No INVOCATION_LOG_GROUP configured in tests -> ingestion is a no-op.
    assert result["metering"] == {"metered_users": 0, "unresolved_sessions": []}
    assert fake_sns.published == []


def test_blocks_on_native_metered_usage(fake_dynamodb, fake_sns, monkeypatch):
    """Native-vended spend (now in the unified cost_micro counter) trips the block."""
    monkeypatch.setenv("SNS_TOPIC_ARN", "arn:fake")
    _seed_user(fake_dynamodb, "native", usd_limit=1.0)
    _seed_metered_usage(fake_dynamodb, "native", cost_usd=1.5)

    result = reconciler.handler({}, None, dynamodb=fake_dynamodb, sns=fake_sns)
    assert result["blocked"] == ["native"]


def _ingest_native(db, sub, session, model, in_tok, out_tok, reqs, now):
    """Run reconciler._ingest_usage once with a fake log row for `session`."""
    users = db.Table(os.environ["USERS_TABLE"])
    usage = db.Table(os.environ["USAGE_TABLE"])
    # Reverse-map row so the session resolves to the user/tenant.
    users.put_item(Item={"user_id": f"SESSION#{session}", "maps_to": sub})
    arn = f"arn:aws:sts::111122223333:assumed-role/BedrockUserRole/{session}"
    logs = _FakeLogs([{"arn": arn, "model": model,
                       "input_tokens": in_tok, "output_tokens": out_tok, "requests": reqs}])
    return reconciler._ingest_usage(logs, users, usage, "lg", WINDOW, now)


def test_native_ingestion_is_idempotent_and_composes_with_proxy(fake_dynamodb, monkeypatch):
    """#1/#5/#10: reconciler ADDs the native DELTA into the ONE cost_micro
    counter, so (a) re-running doesn't double-count and (b) it composes with
    proxy spend already ADDed to the same row."""
    now = datetime.now(timezone.utc)
    _seed_user(fake_dynamodb, "acme", usd_limit=100.0)
    usage = fake_dynamodb.Table(os.environ["USAGE_TABLE"])
    # Proxy (Mode B) already settled some spend into the shared counter.
    usage.put_item(Item={"user_id": "acme", "window": WINDOW,
                         "cost_micro": 500, "input_tokens": 5, "output_tokens": 5, "requests": 1})

    # First native ingestion: 1000 in / 200 out on gpt-oss-120b.
    _ingest_native(fake_dynamodb, "acme", "acme-hash", "openai.gpt-oss-120b",
                   1000, 200, 3, now)
    row1 = usage.get_item(Key={"user_id": "acme", "window": WINDOW})["Item"]
    native_cost = reconciler._cost_micro(reconciler._prices(),
                                         "openai.gpt-oss-120b", 1000, 200)
    # Shared counter = proxy(500) + native; tokens/requests composed too.
    assert int(row1["cost_micro"]) == 500 + native_cost
    assert int(row1["input_tokens"]) == 5 + 1000
    assert int(row1["requests"]) == 1 + 3

    # Re-run with the SAME logs (Insights re-sums the whole day): delta is 0,
    # so the counter must NOT move (idempotent).
    _ingest_native(fake_dynamodb, "acme", "acme-hash", "openai.gpt-oss-120b",
                   1000, 200, 3, now)
    row2 = usage.get_item(Key={"user_id": "acme", "window": WINDOW})["Item"]
    assert int(row2["cost_micro"]) == int(row1["cost_micro"])
    assert int(row2["input_tokens"]) == int(row1["input_tokens"])

    # More native usage arrives: only the incremental delta is added.
    _ingest_native(fake_dynamodb, "acme", "acme-hash", "openai.gpt-oss-120b",
                   1500, 200, 4, now)
    row3 = usage.get_item(Key={"user_id": "acme", "window": WINDOW})["Item"]
    extra = reconciler._cost_micro(reconciler._prices(),
                                   "openai.gpt-oss-120b", 500, 0)  # +500 input tokens
    assert int(row3["cost_micro"]) == int(row1["cost_micro"]) + extra
    assert int(row3["input_tokens"]) == 5 + 1500


def test_native_bedrock_model_id_uses_known_price():
    prices = reconciler._prices()
    native = reconciler._cost_micro(
        prices, "openai.gpt-oss-20b-1:0", 1_000_000, 1_000_000
    )
    mantle = reconciler._cost_micro(
        prices, "openai.gpt-oss-20b", 1_000_000, 1_000_000
    )
    assert native == mantle == 370_000


def test_blocks_at_exact_limit_boundary(fake_dynamodb, fake_sns, monkeypatch):
    """#6: usage exactly == limit must block (>=), matching the vend gate, so
    the user doesn't flap blocked/active every cycle."""
    monkeypatch.setenv("SNS_TOPIC_ARN", "arn:fake")
    _seed_user(fake_dynamodb, "edge", usd_limit=1.0)
    _seed_usage(fake_dynamodb, "edge", cost_usd=1.0)  # exactly at limit

    result = reconciler.handler({}, None, dynamodb=fake_dynamodb, sns=fake_sns)
    assert result["blocked"] == ["edge"]
