"""Event-driven metering for direct Amazon Bedrock Runtime calls.

CloudWatch Logs invokes this Lambda through a subscription on the account's
Bedrock model-invocation log group. Each record is attributed using the
RoleSessionName stamped by the credential broker, priced from the deployment
snapshot, and applied to the user's UTC-day aggregate.

CloudWatch Logs delivery is at-least-once. A DynamoDB transaction creates a
request-id marker and increments the aggregate together, so retries cannot
double-charge a user or lose an increment between separate writes.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import boto3
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError

MICRO = 1_000_000
METRICS_NAMESPACE = os.environ.get(
    "METRICS_NAMESPACE", "BedrockQuotaGateway"
)
_ASSUMED_ROLE_ARN = re.compile(
    r":assumed-role/(?P<role>[^/]+)/(?P<session>[\w+=,.@-]+)$"
)
_SERIALIZER = TypeSerializer()

_DEFAULT_PRICES = {
    "openai.gpt-oss-120b": (0.15, 0.60),
    "openai.gpt-oss-120b-1:0": (0.15, 0.60),
    "openai.gpt-oss-20b": (0.07, 0.30),
    "openai.gpt-oss-20b-1:0": (0.07, 0.30),
    "anthropic.claude-opus-4-7": (15.00, 75.00),
}
_DEFAULT_FALLBACK_PRICE = (15.00, 75.00)


@dataclass(frozen=True)
class InvocationUsage:
    request_id: str
    session_name: str
    model_id: str
    input_tokens: int
    output_tokens: int
    occurred_at: datetime

    @property
    def window(self) -> str:
        return self.occurred_at.strftime("%Y-%m-%d")


_dynamodb_resource = None
_dynamodb_client = None
_sns_client = None


def _resources():
    global _dynamodb_resource, _dynamodb_client, _sns_client
    if _dynamodb_resource is None:
        _dynamodb_resource = boto3.resource("dynamodb")
    if _dynamodb_client is None:
        _dynamodb_client = boto3.client("dynamodb")
    if _sns_client is None:
        _sns_client = boto3.client("sns")
    return _dynamodb_resource, _dynamodb_client, _sns_client


def _decode_subscription(event: dict) -> list[dict]:
    encoded = event.get("awslogs", {}).get("data")
    if not isinstance(encoded, str) or not encoded:
        raise ValueError("expected a CloudWatch Logs subscription event")
    payload = json.loads(gzip.decompress(base64.b64decode(encoded)))
    if payload.get("messageType") == "CONTROL_MESSAGE":
        return []
    return list(payload.get("logEvents", []))


def _non_negative_int(value: Any) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def _timestamp(record: dict, log_event: dict) -> datetime:
    raw = record.get("timestamp")
    if isinstance(raw, str) and raw:
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(
                timezone.utc
            )
        except ValueError:
            pass
    millis = _non_negative_int(log_event.get("timestamp"))
    return datetime.fromtimestamp(millis / 1000, tz=timezone.utc)


def _parse_invocation(
    log_event: dict, expected_role_name: str
) -> InvocationUsage | None:
    try:
        record = json.loads(log_event.get("message", ""))
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(record, dict):
        return None

    identity = record.get("identity")
    arn = identity.get("arn", "") if isinstance(identity, dict) else ""
    match = _ASSUMED_ROLE_ARN.search(str(arn))
    if not match or match.group("role") != expected_role_name:
        return None

    input_data = record.get("input")
    output_data = record.get("output")
    input_data = input_data if isinstance(input_data, dict) else {}
    output_data = output_data if isinstance(output_data, dict) else {}
    input_tokens = _non_negative_int(
        input_data.get(
            "inputTokenCount", input_data.get("inputBodyTokenCount", 0)
        )
    )
    output_tokens = _non_negative_int(
        output_data.get(
            "outputTokenCount", output_data.get("outputBodyTokenCount", 0)
        )
    )
    request_id = str(record.get("requestId") or log_event.get("id") or "")
    if not request_id:
        request_id = hashlib.sha256(
            str(log_event.get("message", "")).encode("utf-8")
        ).hexdigest()
    return InvocationUsage(
        request_id=request_id,
        session_name=match.group("session"),
        model_id=str(record.get("modelId") or "unknown"),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        occurred_at=_timestamp(record, log_event),
    )


def _prices() -> dict[str, tuple[float, float]]:
    raw = os.environ.get("MODEL_PRICES_JSON")
    if not raw:
        return dict(_DEFAULT_PRICES)
    return {
        model_id: (
            float(price["input_per_mtok"]),
            float(price["output_per_mtok"]),
        )
        for model_id, price in json.loads(raw).items()
    }


def _fallback_price() -> tuple[float, float]:
    raw = os.environ.get("MODEL_FALLBACK_PRICE_JSON")
    if not raw:
        return _DEFAULT_FALLBACK_PRICE
    price = json.loads(raw)
    return (
        float(price["input_per_mtok"]),
        float(price["output_per_mtok"]),
    )


def _cost_micro(
    prices: dict[str, tuple[float, float]],
    model_id: str,
    input_tokens: int,
    output_tokens: int,
) -> int:
    input_rate, output_rate = prices.get(model_id, _fallback_price())
    usd = (
        input_tokens * input_rate + output_tokens * output_rate
    ) / 1_000_000
    micro = int(usd * MICRO)
    return micro + 1 if usd * MICRO > micro else micro


def _av(value: Any) -> dict:
    return _SERIALIZER.serialize(value)


def _ttl_epoch(occurred_at: datetime) -> int:
    keep_days = int(os.environ.get("USAGE_RETENTION_DAYS", "35"))
    return int(occurred_at.timestamp()) + keep_days * 86400


def _apply_usage(
    client,
    usage_table_name: str,
    user_id: str,
    usage: InvocationUsage,
    cost_micro: int,
) -> bool:
    """Apply one invocation exactly once. Returns False for a duplicate."""
    ttl = _ttl_epoch(usage.occurred_at)
    marker_key = {
        "user_id": _av(f"REQUEST#{usage.request_id}"),
        "window": _av("EVENT"),
    }
    try:
        client.transact_write_items(
            TransactItems=[
                {
                    "Put": {
                        "TableName": usage_table_name,
                        "Item": {
                            **marker_key,
                            "expires_at": _av(ttl),
                        },
                        "ConditionExpression": "attribute_not_exists(user_id)",
                    }
                },
                {
                    "Update": {
                        "TableName": usage_table_name,
                        "Key": {
                            "user_id": _av(user_id),
                            "window": _av(usage.window),
                        },
                        "UpdateExpression": (
                            "ADD cost_micro :c, input_tokens :i, "
                            "output_tokens :o, requests :one "
                            "SET expires_at = if_not_exists(expires_at, :ttl)"
                        ),
                        "ExpressionAttributeValues": {
                            ":c": _av(cost_micro),
                            ":i": _av(usage.input_tokens),
                            ":o": _av(usage.output_tokens),
                            ":one": _av(1),
                            ":ttl": _av(ttl),
                        },
                    }
                },
            ]
        )
        return True
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code not in {
            "TransactionCanceledException",
            "ConditionalCheckFailedException",
        }:
            raise
        # TransactionCanceledException also represents conflicts, capacity
        # failures, and validation errors. Confirm the request marker before
        # classifying the delivery as a duplicate; otherwise let the Logs
        # subscription retry instead of silently dropping usage.
        marker = client.get_item(
            TableName=usage_table_name,
            Key=marker_key,
            ConsistentRead=True,
        )
        if marker.get("Item"):
            return False
        raise


def _notify(sns, subject: str, payload: dict) -> None:
    topic_arn = os.environ.get("SNS_TOPIC_ARN", "")
    if topic_arn:
        sns.publish(
            TopicArn=topic_arn,
            Subject=subject[:100],
            Message=json.dumps(payload, indent=2, default=str),
        )


def _usage_row(table, user_id: str, window: str) -> dict:
    return table.get_item(
        Key={"user_id": user_id, "window": window},
        ConsistentRead=True,
    ).get("Item", {})


def _limit_ratio(usage: dict, user: dict) -> float:
    dimensions = (
        ("cost_micro", "daily_usd_micro"),
        ("input_tokens", "daily_input_tokens"),
        ("output_tokens", "daily_output_tokens"),
    )
    ratios = [
        int(usage.get(value_key, 0)) / int(user[limit_key])
        for value_key, limit_key in dimensions
        if int(user.get(limit_key, 0)) > 0
    ]
    return max(ratios, default=0.0)


def _evaluate_quota(
    users_table, usage_table, sns, user_id: str, window: str
) -> str:
    """Block or warn from the just-updated current UTC window."""
    if window != datetime.now(timezone.utc).strftime("%Y-%m-%d"):
        return "historical"
    user = users_table.get_item(
        Key={"user_id": user_id}, ConsistentRead=True
    ).get("Item")
    if not user:
        return "missing-user"
    current = _usage_row(usage_table, user_id, window)
    ratio = _limit_ratio(current, user)
    status = str(user.get("status", "active"))
    reason = str(user.get("status_reason", ""))

    if ratio >= 1:
        if status == "blocked" and not reason.startswith("auto:"):
            return "manually-blocked"
        auto_reason = f"auto: quota exhausted in {window}"
        if status != "blocked" or reason != auto_reason:
            changed_at = datetime.now(timezone.utc).isoformat()
            users_table.update_item(
                Key={"user_id": user_id},
                UpdateExpression=(
                    "SET #s = :s, status_reason = :r, status_changed_at = :t"
                ),
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":s": "blocked",
                    ":r": auto_reason,
                    ":t": changed_at,
                },
            )
            refreshed_user = users_table.get_item(
                Key={"user_id": user_id}, ConsistentRead=True
            ).get("Item", {})
            users_table.put_item(
                Item={
                    "user_id": f"REVOCATION#{user_id}",
                    "maps_to": user_id,
                    "desired_status": "blocked",
                    "source_identity": str(
                        refreshed_user.get("source_identity", "")
                    ),
                    "updated_at": changed_at,
                    "expires_at": _ttl_epoch(datetime.now(timezone.utc)),
                }
            )
            _notify(
                sns,
                f"[bedrock-quota] BLOCKED {user_id}",
                {"user_id": user_id, "window": window, "usage": current},
            )
        return "blocked"

    warn_threshold = float(os.environ.get("WARN_THRESHOLD", "0.8"))
    if (
        ratio >= warn_threshold
        and user.get("warning_sent_window") != window
    ):
        users_table.update_item(
            Key={"user_id": user_id},
            UpdateExpression="SET warning_sent_window = :w",
            ExpressionAttributeValues={":w": window},
        )
        _notify(
            sns,
            f"[bedrock-quota] WARNING {user_id}",
            {
                "user_id": user_id,
                "window": window,
                "utilization": ratio,
                "usage": current,
            },
        )
        return "warned"
    return "within-budget"


def _emit_emf(
    user_id: str, usage: InvocationUsage, cost_micro: int, used_fallback: bool
) -> None:
    processed_at = datetime.now(timezone.utc)
    detection_lag_ms = max(
        0,
        int((processed_at - usage.occurred_at).total_seconds() * 1_000),
    )
    record = {
        "_aws": {
            "Timestamp": int(processed_at.timestamp() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": METRICS_NAMESPACE,
                    "Dimensions": [["UserId"], ["Model"], []],
                    "Metrics": [
                        {"Name": "Requests", "Unit": "Count"},
                        {"Name": "InputTokens", "Unit": "Count"},
                        {"Name": "OutputTokens", "Unit": "Count"},
                        {"Name": "EstimatedCostUSD", "Unit": "None"},
                        {
                            "Name": "DetectionLagMilliseconds",
                            "Unit": "Milliseconds",
                        },
                    ],
                }
            ],
        },
        "UserId": user_id,
        "Model": usage.model_id,
        "RequestId": usage.request_id,
        "PriceSource": "fallback" if used_fallback else "snapshot",
        "InvocationOccurredAt": usage.occurred_at.isoformat(),
        "ProcessedAt": processed_at.isoformat(),
        "DetectionLagMilliseconds": detection_lag_ms,
        "Requests": 1,
        "InputTokens": usage.input_tokens,
        "OutputTokens": usage.output_tokens,
        "EstimatedCostUSD": round(cost_micro / MICRO, 8),
    }
    print(json.dumps(record))


def handler(
    event,
    context,
    *,
    dynamodb=None,
    dynamodb_client=None,
    sns=None,
) -> dict:
    if dynamodb is None or dynamodb_client is None or sns is None:
        resource, default_client, sns_client = _resources()
        dynamodb = dynamodb or resource
        dynamodb_client = dynamodb_client or default_client
        sns = sns or sns_client
    client = dynamodb_client
    users_table = dynamodb.Table(os.environ["USERS_TABLE"])
    usage_table = dynamodb.Table(os.environ["USAGE_TABLE"])
    usage_table_name = os.environ["USAGE_TABLE"]
    expected_role = os.environ["BEDROCK_USER_ROLE_NAME"]
    prices = _prices()

    result = {
        "processed": 0,
        "duplicates": 0,
        "unresolved_sessions": [],
        "ignored": 0,
    }
    unresolved: set[str] = set()
    for log_event in _decode_subscription(event):
        usage = _parse_invocation(log_event, expected_role)
        if usage is None:
            result["ignored"] += 1
            continue
        mapping = users_table.get_item(
            Key={"user_id": f"SESSION#{usage.session_name}"},
            ConsistentRead=True,
        ).get("Item")
        if not mapping:
            unresolved.add(usage.session_name)
            continue
        user_id = str(mapping["maps_to"])
        cost_micro = _cost_micro(
            prices, usage.model_id, usage.input_tokens, usage.output_tokens
        )
        if not _apply_usage(
            client, usage_table_name, user_id, usage, cost_micro
        ):
            result["duplicates"] += 1
            continue
        result["processed"] += 1
        _emit_emf(
            user_id,
            usage,
            cost_micro,
            used_fallback=usage.model_id not in prices,
        )
        _evaluate_quota(
            users_table, usage_table, sns, user_id, usage.window
        )

    result["unresolved_sessions"] = sorted(unresolved)
    if unresolved:
        print(
            json.dumps(
                {
                    "level": "warning",
                    "message": "Invocation logs had unknown broker sessions",
                    "sessions": sorted(unresolved),
                }
            )
        )
    return result
