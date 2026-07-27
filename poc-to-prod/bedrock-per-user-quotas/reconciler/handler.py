"""Safety-net reconciler for the per-user quota gateway.

Runs on a configurable EventBridge schedule (deploy-time, default: every
5 minutes) and closes the gaps that real-time enforcement cannot cover on
its own:

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

import hashlib
import json
import os
import re
from datetime import datetime, timezone

import boto3

from metering_ingest import aggregate_by_user, run_insights_query, _window_epoch_bounds

MICRO = 1_000_000
METRICS_NAMESPACE = os.environ.get("METRICS_NAMESPACE", "BedrockQuotaGateway")

# Must match gateway/app/broker.session_name_for EXACTLY: the same sanitized,
# collision-resistant identity the broker stamps as SourceIdentity (charset
# [\w+=.@-] with comma excluded, 20 hex hash chars). Duplicated because the
# reconciler is a separate Lambda asset and can't import the gateway package
# (same reason _cost_micro is duplicated below). If the broker's rule changes,
# change it here too or SourceIdentity denies will target the wrong identity.
_SESSION_SAFE = re.compile(r"[^\w+=.@-]", re.ASCII)
_MAX_SESSION_NAME = 64
_HASH_HEX = 20


def session_name_for(sub: str) -> str:
    cleaned = _SESSION_SAFE.sub("-", sub).strip("-") or "user"
    digest = hashlib.sha256(sub.encode("utf-8")).hexdigest()[:_HASH_HEX]
    suffix = "-" + digest
    return cleaned[: _MAX_SESSION_NAME - len(suffix)] + suffix


def _emit_emf(user_id: str, d_cost_micro: int, d_in: int, d_out: int, d_req: int) -> None:
    """Emit per-user CloudWatch metrics (EMF) for native-vended usage the
    reconciler just metered, so the dashboard reflects Mode A too (the proxy
    path emits its own EMF; without this, native spend was invisible there).
    Emits the DELTA applied this run so cumulative Sum stats stay correct."""
    if d_req <= 0 and d_cost_micro <= 0:
        return
    record = {
        "_aws": {
            "Timestamp": int(datetime.now(timezone.utc).timestamp() * 1000),
            "CloudWatchMetrics": [{
                "Namespace": METRICS_NAMESPACE,
                "Dimensions": [["UserId"], []],
                "Metrics": [
                    {"Name": "Requests", "Unit": "Count"},
                    {"Name": "InputTokens", "Unit": "Count"},
                    {"Name": "OutputTokens", "Unit": "Count"},
                    {"Name": "EstimatedCostUSD", "Unit": "None"},
                ],
            }],
        },
        "UserId": user_id, "Source": "reconciler",
        "Requests": max(d_req, 0), "InputTokens": max(d_in, 0),
        "OutputTokens": max(d_out, 0),
        "EstimatedCostUSD": round(max(d_cost_micro, 0) / MICRO, 8),
    }
    print(json.dumps(record))


# --- pricing (self-contained; the reconciler is a separate Lambda asset and
# cannot import gateway/app/pricing.py at runtime) ---
# USD per 1M tokens. In a real deploy the CDK injects MODEL_PRICES_JSON (the
# SAME table given to the gateway), which _prices() uses as the complete
# authoritative snapshot so the two Lambdas cannot drift. This dict is only
# the local/offline fallback when MODEL_PRICES_JSON is unset.
#
# CACHE CAVEAT: unlike the gateway, this pricer has no prompt-cache
# multipliers, because Bedrock model-invocation logs carry no cache-token
# fields (only input.inputTokenCount / output.outputTokenCount). If those
# counts exclude cache read/write tokens (as the Anthropic usage shape
# suggests), cache-heavy native-vended traffic is UNDER-counted here, so the
# reconciler may under-enforce dollar budgets for coding-agent workloads.
# Getting cache-accurate native metering requires enabling text-data delivery
# and parsing inputBodyJson (heavier + privacy-sensitive). See the note in the
# CDK MODEL_PRICES definition for the accuracy tradeoff.
_GPT_OSS_120B_PRICE = (0.15, 0.60)
_GPT_OSS_20B_PRICE = (0.07, 0.30)
_DEFAULT_PRICES = {
    "openai.gpt-oss-120b": _GPT_OSS_120B_PRICE,
    "openai.gpt-oss-120b-1:0": _GPT_OSS_120B_PRICE,
    "openai.gpt-oss-20b": _GPT_OSS_20B_PRICE,
    "openai.gpt-oss-20b-1:0": _GPT_OSS_20B_PRICE,
    "anthropic.claude-opus-4-7": (15.00, 75.00),
}
# Unknown models bill at the most expensive known rate so a missing entry
# can never be used to slip under a budget.
_FALLBACK_PRICE = (15.00, 75.00)


def _fallback_price() -> tuple[float, float]:
    raw = os.environ.get("MODEL_FALLBACK_PRICE_JSON")
    if not raw:
        return _FALLBACK_PRICE
    parsed = json.loads(raw)
    return (
        float(parsed["input_per_mtok"]),
        float(parsed["output_per_mtok"]),
    )


def _prices() -> dict:
    raw = os.environ.get("MODEL_PRICES_JSON")
    if not raw:
        return dict(_DEFAULT_PRICES)
    prices = {}
    for model_id, p in json.loads(raw).items():
        prices[model_id] = (float(p["input_per_mtok"]), float(p["output_per_mtok"]))
    return prices


def _cost_micro(prices: dict, model_id: str, in_tok: int, out_tok: int) -> int:
    in_rate, out_rate = prices.get(model_id, _fallback_price())
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
    items.extend(
        item for item in resp.get("Items", [])
        if not str(item.get("user_id", "")).startswith("SESSION#")
    )
    while "LastEvaluatedKey" in resp:
        resp = table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
        items.extend(
            item for item in resp.get("Items", [])
            if not str(item.get("user_id", "")).startswith("SESSION#")
        )
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


# --- EXPERIMENTAL: early revocation of in-flight vended sessions ---------
# When experimental_native_session_deny is on, the reconciler maintains an
# inline policy on BedrockUserRole that Denies Bedrock invoke actions for the
# currently-blocked users, keyed on aws:SourceIdentity (the identity the broker
# stamped). This is the AWS-documented session-revocation pattern: it cuts a
# blocked user's ALREADY-vended credentials before they expire, instead of
# waiting out the TTL. It is OFF by default and grants the reconciler
# iam:PutRolePolicy/DeleteRolePolicy on that one role only.
#
# Failure is non-fatal by design: TTL expiry (<= vended TTL) is the guaranteed
# bound, so on policy-size overflow or any IAM error we alert and fall back to
# TTL rather than break the run. IAM propagation is eventually consistent — the
# deny is not instantaneous; measure, don't promise instant cutoff.
_NATIVE_DENY_POLICY_NAME = "quota-native-session-deny"
_DENY_ACTIONS = [
    "bedrock:InvokeModel",
    "bedrock:InvokeModelWithResponseStream",
    "bedrock:Converse",
    "bedrock:ConverseStream",
]
# Inline role policies cap at 10,240 chars; stay well under so we alert-and-skip
# rather than fail a PutRolePolicy on a huge blocked set.
_MAX_DENY_SOURCE_IDENTITIES = 200


def _native_deny_document(blocked_session_ids: list[str]) -> dict:
    return {
        "Version": "2012-10-17",
        "Statement": [{
            "Sid": "DenyBlockedQuotaUsers",
            "Effect": "Deny",
            "Action": _DENY_ACTIONS,
            "Resource": "*",
            "Condition": {
                "StringEquals": {"aws:SourceIdentity": sorted(blocked_session_ids)}
            },
        }],
    }


def _sync_native_deny_policy(iam_client, role_name: str, blocked_user_ids: list[str],
                             sns, topic_arn: str) -> dict:
    """Put/delete the SourceIdentity Deny policy for the blocked set. Returns a
    status dict; never raises (falls back to TTL on any failure)."""
    session_ids = [session_name_for(u) for u in blocked_user_ids]
    try:
        if not session_ids:
            # No one blocked -> remove the policy (idempotent).
            try:
                iam_client.delete_role_policy(
                    RoleName=role_name, PolicyName=_NATIVE_DENY_POLICY_NAME)
            except iam_client.exceptions.NoSuchEntityException:
                pass
            return {"mode": "native_session_deny", "denied": 0}
        if len(session_ids) > _MAX_DENY_SOURCE_IDENTITIES:
            _notify(sns, topic_arn,
                    "[quota-gateway] native-deny OVERFLOW (falling back to TTL)",
                    {"blocked": len(session_ids), "cap": _MAX_DENY_SOURCE_IDENTITIES})
            return {"mode": "native_session_deny", "overflow": True,
                    "blocked": len(session_ids), "denied": 0}
        iam_client.put_role_policy(
            RoleName=role_name,
            PolicyName=_NATIVE_DENY_POLICY_NAME,
            PolicyDocument=json.dumps(_native_deny_document(session_ids)),
        )
        return {"mode": "native_session_deny", "denied": len(session_ids)}
    except Exception as exc:  # noqa: BLE001 - never break the run on IAM issues
        _notify(sns, topic_arn,
                "[quota-gateway] native-deny IAM error (falling back to TTL)",
                {"error": str(exc), "role": role_name})
        return {"mode": "native_session_deny", "error": str(exc), "denied": 0}


def _mark_warning_sent(users_table, user_id: str, window: str) -> None:
    users_table.update_item(
        Key={"user_id": user_id},
        UpdateExpression="SET warning_sent_window = :w",
        ExpressionAttributeValues={":w": window},
    )


def _window_ttl_epoch(now: datetime, keep_days: int | None = None) -> int:
    keep_days = keep_days or int(os.environ.get("USAGE_RETENTION_DAYS", "35"))
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
    """Reconcile the ONE per-user usage counter to the authoritative
    log-derived total for native-vended traffic.

    There is a single set of counters (cost_micro / input_tokens /
    output_tokens / requests) that BOTH writers feed:
      - the proxy (Mode B) ADDs real usage at settle time;
      - this reconciler ADDs the *delta* of native-vended (Mode A) usage.

    To stay idempotent across the every-5-min re-runs (each query re-sums the
    whole day) while ALSO composing with the proxy's ADDs, we remember how
    much this reconciler has already applied to the window in bookkeeping
    fields (``metered_applied_*``) and ADD only ``new_total - already_applied``
    each run. So the counter converges to proxy_usage + native_usage without
    the SET-vs-ADD clobber a shared counter would otherwise suffer.
    """
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

        # Read what we've already applied so this run only ADDs the delta.
        item = usage_table.get_item(
            Key={"user_id": user_id, "window": window}).get("Item") or {}
        d_cost = cost - int(item.get("metered_applied_cost_micro", 0))
        d_in = in_tok - int(item.get("metered_applied_input_tokens", 0))
        d_out = out_tok - int(item.get("metered_applied_output_tokens", 0))
        d_req = reqs - int(item.get("metered_applied_requests", 0))
        if (d_cost, d_in, d_out, d_req) == (0, 0, 0, 0):
            metered += 1
            continue

        # ADD the delta to the shared enforced counters, and SET the
        # bookkeeping to the new authoritative total in the same update.
        usage_table.update_item(
            Key={"user_id": user_id, "window": window},
            UpdateExpression=(
                "ADD cost_micro :dc, input_tokens :di, output_tokens :do, requests :dr "
                "SET metered_applied_cost_micro = :c, metered_applied_input_tokens = :i, "
                "metered_applied_output_tokens = :o, metered_applied_requests = :r, "
                "metered_at = :t, expires_at = :e"
            ),
            ExpressionAttributeValues={
                ":dc": d_cost, ":di": d_in, ":do": d_out, ":dr": d_req,
                ":c": cost, ":i": in_tok, ":o": out_tok, ":r": reqs,
                ":t": now.isoformat(), ":e": _window_ttl_epoch(now),
            },
        )
        _emit_emf(user_id, d_cost, d_in, d_out, d_req)
        metered += 1
    return {
        "metered_users": metered,
        "unresolved_sessions": getattr(aggregate_by_user, "last_unresolved", []),
    }


def handler(event, context, dynamodb=None, sns=None, logs=None, iam=None):  # noqa: ARG001 (Lambda signature)
    if dynamodb is None or sns is None:
        dynamodb, sns = _resources()
    log_group = os.environ.get("INVOCATION_LOG_GROUP", "")
    if logs is None and log_group:
        logs = boto3.client("logs")
    users_table = dynamodb.Table(os.environ["USERS_TABLE"])
    usage_table = dynamodb.Table(os.environ["USAGE_TABLE"])
    topic_arn = os.environ.get("SNS_TOPIC_ARN", "")
    warn_threshold = float(os.environ.get("WARN_THRESHOLD", "0.8"))
    native_deny = os.environ.get("EXPERIMENTAL_NATIVE_SESSION_DENY", "").lower() == "true"
    now = datetime.now(timezone.utc)
    window = now.strftime("%Y-%m-%d")

    # Step 1: pull authoritative per-user usage from Bedrock's own logs.
    # This is the API/provider-agnostic metering; everything below is the
    # existing block/unblock/alert safety net operating on the result.
    ingest = _ingest_usage(logs, users_table, usage_table, log_group, window, now)

    blocked, unblocked, warned = [], [], []
    # Every user in a blocked state after this run (pre-existing + newly
    # blocked, minus unblocked) — the target set for the SourceIdentity deny.
    all_blocked: list[str] = []

    for user in _scan_users(users_table):
        user_id = str(user["user_id"])
        status = str(user.get("status", "active"))
        limit_cost = int(user.get("daily_usd_micro", 0))
        limit_in = int(user.get("daily_input_tokens", 0))
        limit_out = int(user.get("daily_output_tokens", 0))

        usage = usage_table.get_item(Key={"user_id": user_id, "window": window}).get("Item") or {}
        # ONE counter set, fed by both writers (proxy settle ADD + this
        # reconciler's delta ADD in _ingest_usage), so no summing needed here
        # or in get_window_usage — the enforced value is already the total.
        cost = int(usage.get("cost_micro", 0))
        tokens_in = int(usage.get("input_tokens", 0))
        tokens_out = int(usage.get("output_tokens", 0))

        # A limit of 0 means "not enforced" for that dimension (block a user
        # via status, not a 0 budget). Use >= so the boundary matches the
        # broker's vend-time _is_over_budget check exactly; a strict > here
        # made an exactly-at-limit user flap blocked/active every cycle.
        over = (
            (limit_cost and cost >= limit_cost)
            or (limit_in and tokens_in >= limit_in)
            or (limit_out and tokens_out >= limit_out)
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
            all_blocked.append(user_id)
        elif status == "blocked" and str(user.get("status_reason", "")).startswith("auto:") and not over:
            _set_status(users_table, user_id, "active", "auto: window reset, usage back under budget")
            _notify(sns, topic_arn, f"[quota-gateway] UNBLOCKED {user_id}", snapshot)
            unblocked.append(user_id)
        elif status == "blocked":
            # Already blocked and staying blocked (auto or admin) — keep it in
            # the deny set so its in-flight vended sessions stay cut off.
            all_blocked.append(user_id)
        elif (
            status == "active"
            and limit_cost
            and cost >= warn_threshold * limit_cost
            and str(user.get("warning_sent_window", "")) != window
        ):
            warned.append(user_id)
            _notify(sns, topic_arn, f"[quota-gateway] WARNING {user_id} at "
                    f"{100 * cost / limit_cost:.0f}% of daily budget", snapshot)
            _mark_warning_sent(users_table, user_id, window)

    result = {"window": window, "blocked": blocked, "unblocked": unblocked,
              "warned": warned, "metering": ingest}

    # Optional early revocation of in-flight vended sessions for blocked users.
    if native_deny:
        role_name = os.environ.get("BEDROCK_USER_ROLE_NAME", "")
        if role_name:
            if iam is None:
                iam = boto3.client("iam")
            result["native_deny"] = _sync_native_deny_policy(
                iam, role_name, all_blocked, sns, topic_arn)
        else:
            result["native_deny"] = {"mode": "native_session_deny",
                                     "error": "BEDROCK_USER_ROLE_NAME not set"}

    print(json.dumps(result))
    return result
