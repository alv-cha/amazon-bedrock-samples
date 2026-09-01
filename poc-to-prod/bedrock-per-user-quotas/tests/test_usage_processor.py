import base64
import gzip
import json
import os
from datetime import datetime, timedelta, timezone

import pytest
from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import ClientError

import handler as processor

ROLE_NAME = "BedrockUserRole"
_DESERIALIZER = TypeDeserializer()


def _decode_map(values: dict) -> dict:
    return {key: _DESERIALIZER.deserialize(value) for key, value in values.items()}


class FakeDynamoClient:
    def __init__(self, resource):
        self.resource = resource

    def transact_write_items(self, TransactItems):  # noqa: N803
        put = TransactItems[0]["Put"]
        update = TransactItems[1]["Update"]
        put_table = self.resource.Table(put["TableName"])
        put_item = _decode_map(put["Item"])
        put_key = {
            key: put_item[key]
            for key in put_table.key_attrs
        }
        if put_table.get_item(Key=put_key).get("Item"):
            raise ClientError(
                {
                    "Error": {
                        "Code": "TransactionCanceledException",
                        "Message": "duplicate",
                    }
                },
                "TransactWriteItems",
            )

        update_table = self.resource.Table(update["TableName"])
        update_key = _decode_map(update["Key"])
        update_values = _decode_map(update["ExpressionAttributeValues"])
        put_table.put_item(Item=put_item)
        update_table.update_item(
            Key=update_key,
            UpdateExpression=update["UpdateExpression"],
            ExpressionAttributeValues=update_values,
        )

    def get_item(self, TableName, Key, ConsistentRead=False):  # noqa: N803
        assert ConsistentRead is True
        table = self.resource.Table(TableName)
        item = table.get_item(Key=_decode_map(Key)).get("Item")
        return {"Item": item} if item else {}


class TransientCancellationClient(FakeDynamoClient):
    def transact_write_items(self, TransactItems):  # noqa: N803
        raise ClientError(
            {
                "Error": {
                    "Code": "TransactionCanceledException",
                    "Message": "transaction conflict",
                }
            },
            "TransactWriteItems",
        )


def _subscription(records: list[dict]) -> dict:
    payload = {
        "messageType": "DATA_MESSAGE",
        "owner": "111122223333",
        "logGroup": "/aws/bedrock/modelinvocations",
        "logStream": "stream",
        "logEvents": records,
    }
    compressed = gzip.compress(json.dumps(payload).encode())
    return {"awslogs": {"data": base64.b64encode(compressed).decode()}}


def _record(
    *,
    request_id="request-1",
    session="alice-session",
    model="openai.gpt-oss-20b",
    input_tokens=100,
    output_tokens=50,
    when: datetime | None = None,
    role=ROLE_NAME,
) -> dict:
    when = when or datetime.now(timezone.utc)
    message = {
        "schemaType": "ModelInvocationLog",
        "timestamp": when.isoformat().replace("+00:00", "Z"),
        "requestId": request_id,
        "modelId": model,
        "identity": {
            "arn": (
                "arn:aws:sts::111122223333:"
                f"assumed-role/{role}/{session}"
            )
        },
        "input": {"inputTokenCount": input_tokens},
        "output": {"outputTokenCount": output_tokens},
    }
    return {
        "id": f"log-{request_id}",
        "timestamp": int(when.timestamp() * 1000),
        "message": json.dumps(message),
    }


def _seed_user(db, user_id: str, *, usd=1.0, in_limit=1000, out_limit=1000):
    db.Table(os.environ["USERS_TABLE"]).put_item(
        Item={
            "user_id": user_id,
            "name": user_id,
            "status": "active",
            "status_reason": "",
            "daily_usd_micro": int(usd * processor.MICRO),
            "daily_input_tokens": in_limit,
            "daily_output_tokens": out_limit,
        }
    )


def _seed_session(db, session: str, user_id: str):
    db.Table(os.environ["USERS_TABLE"]).put_item(
        Item={"user_id": f"SESSION#{session}", "maps_to": user_id}
    )


def _run(event, db, sns):
    return processor.handler(
        event,
        None,
        dynamodb=db,
        dynamodb_client=FakeDynamoClient(db),
        sns=sns,
    )


def test_subscription_event_updates_daily_usage(fake_dynamodb, fake_sns, monkeypatch):
    monkeypatch.setenv("BEDROCK_USER_ROLE_NAME", ROLE_NAME)
    monkeypatch.setenv(
        "MODEL_PRICES_JSON",
        '{"openai.gpt-oss-20b":{"input_per_mtok":1,"output_per_mtok":2}}',
    )
    _seed_user(fake_dynamodb, "alice")
    _seed_session(fake_dynamodb, "alice-session", "alice")

    result = _run(_subscription([_record()]), fake_dynamodb, fake_sns)

    assert result["processed"] == 1
    row = fake_dynamodb.Table(os.environ["USAGE_TABLE"]).get_item(
        Key={
            "user_id": "alice",
            "window": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        }
    )["Item"]
    assert row["input_tokens"] == 100
    assert row["output_tokens"] == 50
    assert row["requests"] == 1
    assert row["cost_micro"] == 200


def test_emf_reports_invocation_to_detection_lag(
    fake_dynamodb, fake_sns, monkeypatch, capsys
):
    monkeypatch.setenv("BEDROCK_USER_ROLE_NAME", ROLE_NAME)
    _seed_user(fake_dynamodb, "alice")
    _seed_session(fake_dynamodb, "alice-session", "alice")
    occurred_at = datetime.now(timezone.utc) - timedelta(seconds=2)

    _run(
        _subscription([_record(when=occurred_at)]),
        fake_dynamodb,
        fake_sns,
    )

    records = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.strip()
    ]
    emf = next(record for record in records if "_aws" in record)
    metric_names = {
        metric["Name"]
        for metric in emf["_aws"]["CloudWatchMetrics"][0]["Metrics"]
    }
    assert "DetectionLagMilliseconds" in metric_names
    assert emf["DetectionLagMilliseconds"] >= 2_000
    assert emf["InvocationOccurredAt"] == occurred_at.isoformat()
    assert emf["ProcessedAt"]


def test_duplicate_delivery_is_idempotent(fake_dynamodb, fake_sns, monkeypatch):
    monkeypatch.setenv("BEDROCK_USER_ROLE_NAME", ROLE_NAME)
    _seed_user(fake_dynamodb, "alice")
    _seed_session(fake_dynamodb, "alice-session", "alice")
    event = _subscription([_record()])

    assert _run(event, fake_dynamodb, fake_sns)["processed"] == 1
    duplicate = _run(event, fake_dynamodb, fake_sns)
    assert duplicate["duplicates"] == 1
    row = fake_dynamodb.Table(os.environ["USAGE_TABLE"]).get_item(
        Key={
            "user_id": "alice",
            "window": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        }
    )["Item"]
    assert row["requests"] == 1


def test_transient_transaction_cancellation_is_retried_not_dropped(
    fake_dynamodb, monkeypatch
):
    monkeypatch.setenv("BEDROCK_USER_ROLE_NAME", ROLE_NAME)
    usage = processor.InvocationUsage(
        request_id="retry-me",
        session_name="alice-session",
        model_id="openai.gpt-oss-20b",
        input_tokens=10,
        output_tokens=5,
        occurred_at=datetime.now(timezone.utc),
    )

    with pytest.raises(ClientError, match="transaction conflict"):
        processor._apply_usage(
            TransientCancellationClient(fake_dynamodb),
            os.environ["USAGE_TABLE"],
            "alice",
            usage,
            1,
        )

    row = fake_dynamodb.Table(os.environ["USAGE_TABLE"]).get_item(
        Key={"user_id": "alice", "window": usage.window}
    )
    assert "Item" not in row


def test_unknown_session_and_other_role_are_not_metered(
    fake_dynamodb, fake_sns, monkeypatch
):
    monkeypatch.setenv("BEDROCK_USER_ROLE_NAME", ROLE_NAME)
    event = _subscription(
        [
            _record(session="unknown"),
            _record(request_id="other", role="DifferentRole"),
        ]
    )
    result = _run(event, fake_dynamodb, fake_sns)
    assert result["unresolved_sessions"] == ["unknown"]
    assert result["ignored"] == 1
    assert result["processed"] == 0


def test_processor_warns_once_then_blocks_at_any_limit(
    fake_dynamodb, fake_sns, monkeypatch
):
    monkeypatch.setenv("BEDROCK_USER_ROLE_NAME", ROLE_NAME)
    monkeypatch.setenv("SNS_TOPIC_ARN", "arn:aws:sns:us-east-1:111122223333:test")
    monkeypatch.setenv("WARN_THRESHOLD", "0.8")
    _seed_user(fake_dynamodb, "alice", in_limit=100, out_limit=1000)
    _seed_session(fake_dynamodb, "alice-session", "alice")

    first = _record(request_id="first", input_tokens=80, output_tokens=0)
    _run(_subscription([first]), fake_dynamodb, fake_sns)
    assert len(fake_sns.published) == 1
    assert "WARNING" in fake_sns.published[0]["Subject"]

    duplicate_warning = _record(
        request_id="second", input_tokens=1, output_tokens=0
    )
    _run(_subscription([duplicate_warning]), fake_dynamodb, fake_sns)
    assert len(fake_sns.published) == 1

    blocking = _record(
        request_id="third", input_tokens=19, output_tokens=0
    )
    _run(_subscription([blocking]), fake_dynamodb, fake_sns)
    user = fake_dynamodb.Table(os.environ["USERS_TABLE"]).get_item(
        Key={"user_id": "alice"}
    )["Item"]
    assert user["status"] == "blocked"
    assert user["status_reason"].startswith("auto:")
    revocation_event = fake_dynamodb.Table(os.environ["USERS_TABLE"]).get_item(
        Key={"user_id": "REVOCATION#alice"}
    )["Item"]
    assert revocation_event["desired_status"] == "blocked"
    assert len(fake_sns.published) == 2
    assert "BLOCKED" in fake_sns.published[1]["Subject"]


def test_historical_late_log_is_metered_but_does_not_block_current_day(
    fake_dynamodb, fake_sns, monkeypatch
):
    monkeypatch.setenv("BEDROCK_USER_ROLE_NAME", ROLE_NAME)
    _seed_user(fake_dynamodb, "alice", in_limit=10)
    _seed_session(fake_dynamodb, "alice-session", "alice")
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    event = _subscription(
        [_record(when=yesterday, input_tokens=100, output_tokens=0)]
    )
    _run(event, fake_dynamodb, fake_sns)
    user = fake_dynamodb.Table(os.environ["USERS_TABLE"]).get_item(
        Key={"user_id": "alice"}
    )["Item"]
    assert user["status"] == "active"
    row = fake_dynamodb.Table(os.environ["USAGE_TABLE"]).get_item(
        Key={"user_id": "alice", "window": yesterday.strftime("%Y-%m-%d")}
    )["Item"]
    assert row["input_tokens"] == 100


def test_unknown_model_uses_configured_conservative_fallback(
    fake_dynamodb, fake_sns, monkeypatch
):
    monkeypatch.setenv("BEDROCK_USER_ROLE_NAME", ROLE_NAME)
    monkeypatch.setenv(
        "MODEL_PRICES_JSON",
        '{"known":{"input_per_mtok":1,"output_per_mtok":2}}',
    )
    monkeypatch.setenv(
        "MODEL_FALLBACK_PRICE_JSON",
        '{"input_per_mtok":40,"output_per_mtok":90}',
    )
    _seed_user(fake_dynamodb, "alice", usd=1000)
    _seed_session(fake_dynamodb, "alice-session", "alice")
    event = _subscription(
        [
            _record(
                model="unknown",
                input_tokens=1_000_000,
                output_tokens=1_000_000,
            )
        ]
    )
    _run(event, fake_dynamodb, fake_sns)
    row = fake_dynamodb.Table(os.environ["USAGE_TABLE"]).get_item(
        Key={
            "user_id": "alice",
            "window": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        }
    )["Item"]
    assert row["cost_micro"] == 130 * processor.MICRO


def test_control_message_is_a_noop(fake_dynamodb, fake_sns, monkeypatch):
    monkeypatch.setenv("BEDROCK_USER_ROLE_NAME", ROLE_NAME)
    payload = gzip.compress(
        json.dumps({"messageType": "CONTROL_MESSAGE"}).encode()
    )
    event = {"awslogs": {"data": base64.b64encode(payload).decode()}}
    assert _run(event, fake_dynamodb, fake_sns)["processed"] == 0
