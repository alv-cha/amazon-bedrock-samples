"""Safety-net reconciler for the per-user quota gateway.

Runs on an EventBridge schedule (default: every 5 minutes) and closes the
gaps that real-time enforcement cannot cover on its own:

1. **Drift blocking** — if a user's *settled* usage for the current UTC day
   exceeds their limits (e.g. streams that under-reported usage, price
   table edits mid-day, manual writes), the user is blocked. The gateway
   denies blocked users on the next request.
2. **Auto-unblock** — users blocked by this reconciler (reason prefixed
   ``auto:``) are re-activated once the current window is back under
   budget, i.e. after the daily reset.
3. **Alerting** — SNS notifications when users are blocked or cross the
   configurable warning threshold (default 80% of the USD budget).

Self-contained: needs only boto3, which the Lambda runtime provides.
"""

import json
import os
from datetime import datetime, timezone

import boto3

from metering_ingest import aggregate_by_user, run_insights_query, _window_epoch_bounds

MICRO = 1_000_000


# --- pricing (self-contained; mirrors gateway/app/pricing.py) ---
# USD per 1M tokens. Override with MODEL_PRICES_JSON to match the gateway.
_DEFAULT_PRICES = {
    "openai.gpt-oss-120b": (0.15, 0.60),
    "openai.gpt-oss-20b": (0.07, 0.30),
    "anthropic.claude-opus-4-7": (15.00, 75.00),
}
# Unknown models bill at the most expensive known rate so a missing entry
# can never be used to slip under a budget.
_FALLBACK_PRICE = (15.00, 75.00)


def _prices() -> dict:
    prices = dict(_DEFAULT_PRICES)
    raw = os.environ.get("MODEL_PRICES_JSON")
    if raw:
        for model_id, p in json.loads(raw).items():
            prices[model_id] = (float(p["input_per_mtok"]), float(p["output_per_mtok"]))
    return prices


def _cost_micro(prices: dict, model_id: str, in_tok: int, out_tok: int) -> int:
    in_rate, out_rate = prices.get(model_id, _FALLBACK_PRICE)
    usd = (max(in_tok, 0) * in_rate + max(out_tok, 0) * out_rate) / 1_000_000
    micro = int(usd * MICRO)
    return micro + 1 if usd * MICRO > micro else micro  # round up



# Lazy singletons so the module imports cleanly in unit tests.
_dynamodb = None
_sns = None


def _resources():
    global _dynamodb, _sns
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb")
    if _sns is None:
        _sns = boto3.client("sns")
    return _dynamodb, _sns


def _current_window() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _scan_users(table) -> list[dict]:
    items, resp = [], table.scan()
    items.extend(resp.get("Items", []))
    while "LastEvaluatedKey" in resp:
        resp = table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
        items.extend(resp.get("Items", []))
    return items


def _notify(sns, topic_arn: str, subject: str, payload: dict) -> None:
    if not topic_arn:
        return
    sns.publish(TopicArn=topic_arn, Subject=subject[:100],
                Message=json.dumps(payload, indent=2, default=str))


def _set_status(users_table, user_id: str, status: str, reason: str) -> None:
    users_table.update_item(
        Key={"user_id": user_id},
        UpdateExpression="SET #s = :s, status_reason = :r, status_changed_at = :t",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":s": status, ":r": reason,
            ":t": datetime.now(timezone.utc).isoformat(),
        },
    )


def _window_ttl_epoch(now: datetime, keep_days: int = 35) -> int:
    return int(now.timestamp()) + keep_days * 86400


class _SessionResolver:
    """Minimal reverse map (session_name -> user_id) over the users table,
    matching QuotaStore.resolve_session so metering_ingest can attribute
    usage without importing the gateway package."""

    def __init__(self, users_table):
        self._t = users_table
        self._cache: dict[str, str | None] = {}

    def resolve_session(self, session_name: str) -> str | None:
        if session_name not in self._cache:
            item = self._t.get_item(
                Key={"user_id": f"SESSION#{session_name}"}).get("Item")
            self._cache[session_name] = str(item["maps_to"]) if item else None
        return self._cache[session_name]


def _ingest_usage(logs_client, users_table, usage_table, log_group: str,
                  window: str, now: datetime) -> dict:
    """Read Bedrock model-invocation logs and write authoritative per-user
    usage for the current window. Idempotent: overwrites the window's
    metered counters with the summed truth (SET, not ADD), so re-running the
    reconciler converges rather than double-counts."""
    if not log_group:
        return {"metered_users": 0, "unresolved_sessions": []}
    start, end = _window_epoch_bounds(now)
    rows = run_insights_query(logs_client, log_group, start, end)
    per_user = aggregate_by_user(rows, _SessionResolver(users_table))
    prices = _prices()

    metered = 0
    for user_id, entries in per_user.items():
        in_tok = sum(e["input_tokens"] for e in entries)
        out_tok = sum(e["output_tokens"] for e in entries)
        reqs = sum(e["requests"] for e in entries)
        cost = sum(_cost_micro(prices, e["model"], e["input_tokens"], e["output_tokens"])
                   for e in entries)
        usage_table.update_item(
            Key={"user_id": user_id, "window": window},
            UpdateExpression=(
                "SET cost_micro = :c, input_tokens = :i, output_tokens = :o, "
                "requests = :r, metered_at = :t, expires_at = :e"
            ),
            ExpressionAttributeValues={
                ":c": cost, ":i": in_tok, ":o": out_tok, ":r": reqs,
                ":t": now.isoformat(), ":e": _window_ttl_epoch(now),
            },
        )
        metered += 1
    return {
        "metered_users": metered,
        "unresolved_sessions": getattr(aggregate_by_user, "last_unresolved", []),
    }


def handler(event, context, dynamodb=None, sns=None, logs=None):  # noqa: ARG001 (Lambda signature)
    if dynamodb is None or sns is None:
        dynamodb, sns = _resources()
    log_group = os.environ.get("INVOCATION_LOG_GROUP", "")
    if logs is None and log_group:
        logs = boto3.client("logs")
    users_table = dynamodb.Table(os.environ["USERS_TABLE"])
    usage_table = dynamodb.Table(os.environ["USAGE_TABLE"])
    topic_arn = os.environ.get("SNS_TOPIC_ARN", "")
    warn_threshold = float(os.environ.get("WARN_THRESHOLD", "0.8"))
    now = datetime.now(timezone.utc)
    window = now.strftime("%Y-%m-%d")

    # Step 1: pull authoritative per-user usage from Bedrock's own logs.
    # This is the API/provider-agnostic metering; everything below is the
    # existing block/unblock/alert safety net operating on the result.
    ingest = _ingest_usage(logs, users_table, usage_table, log_group, window, now)

    blocked, unblocked, warned = [], [], []

    for user in _scan_users(users_table):
        user_id = str(user["user_id"])
        status = str(user.get("status", "active"))
        limit_cost = int(user.get("daily_usd_micro", 0))
        limit_in = int(user.get("daily_input_tokens", 0))
        limit_out = int(user.get("daily_output_tokens", 0))

        usage = usage_table.get_item(Key={"user_id": user_id, "window": window}).get("Item") or {}
        cost = int(usage.get("cost_micro", 0))
        tokens_in = int(usage.get("input_tokens", 0))
        tokens_out = int(usage.get("output_tokens", 0))

        over = (
            (limit_cost and cost > limit_cost)
            or (limit_in and tokens_in > limit_in)
            or (limit_out and tokens_out > limit_out)
        )

        snapshot = {
            "user_id": user_id, "window": window,
            "cost_usd": cost / MICRO, "limit_usd": limit_cost / MICRO,
            "input_tokens": tokens_in, "output_tokens": tokens_out,
        }

        if status == "active" and over:
            _set_status(users_table, user_id, "blocked", "auto: settled usage exceeded daily budget")
            _notify(sns, topic_arn, f"[quota-gateway] BLOCKED {user_id}", snapshot)
            blocked.append(user_id)
        elif status == "blocked" and str(user.get("status_reason", "")).startswith("auto:") and not over:
            _set_status(users_table, user_id, "active", "auto: window reset, usage back under budget")
            _notify(sns, topic_arn, f"[quota-gateway] UNBLOCKED {user_id}", snapshot)
            unblocked.append(user_id)
        elif status == "active" and limit_cost and cost >= warn_threshold * limit_cost:
            warned.append(user_id)
            _notify(sns, topic_arn, f"[quota-gateway] WARNING {user_id} at "
                    f"{100 * cost / limit_cost:.0f}% of daily budget", snapshot)

    result = {"window": window, "blocked": blocked, "unblocked": unblocked,
              "warned": warned, "metering": ingest}
    print(json.dumps(result))
    return result
