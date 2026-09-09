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
    assert user["version"] == 1
    assert user["status_origin"] == "automatic"
    assert user["updated_at"]
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
    fake_dynamodb, fake_sns, monkeypatch, capsys
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
    emf = next(
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.strip() and "_aws" in json.loads(line)
    )
    assert emf["Model"] == "unknown"
    assert emf["PriceSource"] == "fallback"


def test_us_opus_inference_profile_uses_exact_snapshot_price(
    fake_dynamodb, fake_sns, monkeypatch, capsys
):
    monkeypatch.setenv("BEDROCK_USER_ROLE_NAME", ROLE_NAME)
    monkeypatch.setenv(
        "MODEL_PRICES_JSON",
        json.dumps(
            {
                "us.anthropic.claude-opus-4-7": {
                    "input_per_mtok": 5.5,
                    "output_per_mtok": 27.5,
                }
            }
        ),
    )
    monkeypatch.setenv(
        "MODEL_FALLBACK_PRICE_JSON",
        '{"input_per_mtok":15,"output_per_mtok":75}',
    )
    _seed_user(fake_dynamodb, "alice", usd=1000)
    _seed_session(fake_dynamodb, "alice-session", "alice")

    _run(
        _subscription(
            [
                _record(
                    model="us.anthropic.claude-opus-4-7",
                    input_tokens=1_000_000,
                    output_tokens=1_000_000,
                )
            ]
        ),
        fake_dynamodb,
        fake_sns,
    )

    row = fake_dynamodb.Table(os.environ["USAGE_TABLE"]).get_item(
        Key={
            "user_id": "alice",
            "window": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        }
    )["Item"]
    assert row["cost_micro"] == 33 * processor.MICRO
    emf = next(
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.strip() and "_aws" in json.loads(line)
    )
    assert emf["Model"] == "us.anthropic.claude-opus-4-7"
    assert emf["PriceSource"] == "snapshot"
    assert emf["EstimatedCostUSD"] == 33.0


def test_control_message_is_a_noop(fake_dynamodb, fake_sns, monkeypatch):
    monkeypatch.setenv("BEDROCK_USER_ROLE_NAME", ROLE_NAME)
    payload = gzip.compress(
        json.dumps({"messageType": "CONTROL_MESSAGE"}).encode()
    )
    event = {"awslogs": {"data": base64.b64encode(payload).decode()}}
    assert _run(event, fake_dynamodb, fake_sns)["processed"] == 0


def test_processor_does_not_take_over_admin_status_with_auto_prefixed_reason(
    fake_dynamodb, fake_sns, monkeypatch
):
    monkeypatch.setenv("BEDROCK_USER_ROLE_NAME", ROLE_NAME)
    _seed_user(fake_dynamodb, "alice", in_limit=10)
    _seed_session(fake_dynamodb, "alice-session", "alice")
    users = fake_dynamodb.Table(os.environ["USERS_TABLE"])
    users.update_item(
        Key={"user_id": "alice"},
        UpdateExpression=(
            "SET #s = :status, status_reason = :reason, "
            "status_origin = :origin, version = :version"
        ),
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":status": "blocked",
            ":reason": "auto: operator-authored reason",
            ":origin": "admin",
            ":version": 7,
        },
    )

    _run(
        _subscription([_record(input_tokens=100, output_tokens=0)]),
        fake_dynamodb,
        fake_sns,
    )

    user = users.get_item(Key={"user_id": "alice"})["Item"]
    assert user["status"] == "blocked"
    assert user["status_origin"] == "admin"
    assert user["status_reason"] == "auto: operator-authored reason"
    assert user["version"] == 7
    assert "Item" not in users.get_item(Key={"user_id": "REVOCATION#alice"})


def _emf_records(capsys):
    return [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.strip() and "_aws" in line and "_aws" in json.loads(line)
    ]


def test_profile_id_resolves_to_base_model_price_before_fallback(
    fake_dynamodb, fake_sns, monkeypatch, capsys
):
    monkeypatch.setenv("BEDROCK_USER_ROLE_NAME", ROLE_NAME)
    monkeypatch.setenv(
        "MODEL_PRICES_JSON",
        json.dumps({
            "anthropic.claude-opus-4-7": {
                "input_per_mtok": 5.0,
                "output_per_mtok": 25.0,
            }
        }),
    )
    monkeypatch.setenv(
        "MODEL_FALLBACK_PRICE_JSON",
        '{"input_per_mtok":15,"output_per_mtok":75}',
    )
    _seed_user(fake_dynamodb, "alice", usd=1000)
    _seed_session(fake_dynamodb, "alice-session", "alice")

    _run(
        _subscription([
            _record(
                model="eu.anthropic.claude-opus-4-7",
                input_tokens=1_000_000,
                output_tokens=1_000_000,
            )
        ]),
        fake_dynamodb,
        fake_sns,
    )

    row = fake_dynamodb.Table(os.environ["USAGE_TABLE"]).get_item(
        Key={
            "user_id": "alice",
            "window": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        }
    )["Item"]
    # Base model price (5 + 25), not the 90 USD fallback.
    assert row["cost_micro"] == 30 * processor.MICRO
    emf = _emf_records(capsys)[0]
    assert emf["PriceSource"] == "base-model"
    assert emf["FallbackPricedRequests"] == 0


def test_explicit_profile_price_wins_over_base_model(
    fake_dynamodb, fake_sns, monkeypatch, capsys
):
    monkeypatch.setenv("BEDROCK_USER_ROLE_NAME", ROLE_NAME)
    monkeypatch.setenv(
        "MODEL_PRICES_JSON",
        json.dumps({
            "anthropic.claude-opus-4-7": {
                "input_per_mtok": 5.0,
                "output_per_mtok": 25.0,
            },
            "us.anthropic.claude-opus-4-7": {
                "input_per_mtok": 5.5,
                "output_per_mtok": 27.5,
            },
        }),
    )
    _seed_user(fake_dynamodb, "alice", usd=1000)
    _seed_session(fake_dynamodb, "alice-session", "alice")

    _run(
        _subscription([
            _record(
                model="us.anthropic.claude-opus-4-7",
                input_tokens=1_000_000,
                output_tokens=1_000_000,
            )
        ]),
        fake_dynamodb,
        fake_sns,
    )

    row = fake_dynamodb.Table(os.environ["USAGE_TABLE"]).get_item(
        Key={
            "user_id": "alice",
            "window": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        }
    )["Item"]
    assert row["cost_micro"] == 33 * processor.MICRO
    emf = _emf_records(capsys)[0]
    assert emf["PriceSource"] == "snapshot"
    assert emf["FallbackPricedRequests"] == 0


def test_fallback_pricing_emits_alarmable_metric(
    fake_dynamodb, fake_sns, monkeypatch, capsys
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

    _run(
        _subscription([_record(model="unknown", input_tokens=1, output_tokens=1)]),
        fake_dynamodb,
        fake_sns,
    )

    emf = _emf_records(capsys)[0]
    assert emf["PriceSource"] == "fallback"
    assert emf["FallbackPricedRequests"] == 1
    metric_names = {
        metric["Name"]
        for metric in emf["_aws"]["CloudWatchMetrics"][0]["Metrics"]
    }
    assert "FallbackPricedRequests" in metric_names


class _FakeSsmParameters:
    def __init__(self, value=None, error=None):
        self.value = value
        self.error = error
        self.calls = 0

    def get_parameter(self, Name):  # noqa: N803 (boto3 API)
        self.calls += 1
        if self.error is not None:
            raise self.error
        return {"Parameter": {"Name": Name, "Value": self.value}}


@pytest.fixture(autouse=True)
def _reset_price_cache():
    processor._price_cache.update({"next_attempt_at": 0.0, "value": None})
    yield
    processor._price_cache.update({"next_attempt_at": 0.0, "value": None})


def test_parameter_prices_override_the_deployment_snapshot(
    fake_dynamodb, fake_sns, monkeypatch, capsys
):
    monkeypatch.setenv("BEDROCK_USER_ROLE_NAME", ROLE_NAME)
    monkeypatch.setenv("PRICES_PARAMETER_NAME", "/quota/model-prices")
    monkeypatch.setenv(
        "MODEL_PRICES_JSON",
        '{"m":{"input_per_mtok":100,"output_per_mtok":100}}',
    )
    ssm = _FakeSsmParameters(
        value=json.dumps({
            "models": {"m": {"input_per_mtok": 1.0, "output_per_mtok": 2.0}},
            "fallback": {"input_per_mtok": 40.0, "output_per_mtok": 90.0},
            "resolved_at": "2026-09-02T00:00:00+00:00",
        })
    )
    _seed_user(fake_dynamodb, "alice", usd=1000)
    _seed_session(fake_dynamodb, "alice-session", "alice")

    processor.handler(
        _subscription([
            _record(model="m", input_tokens=1_000_000, output_tokens=1_000_000)
        ]),
        None,
        dynamodb=fake_dynamodb,
        dynamodb_client=FakeDynamoClient(fake_dynamodb),
        sns=fake_sns,
        ssm=ssm,
    )

    row = fake_dynamodb.Table(os.environ["USAGE_TABLE"]).get_item(
        Key={
            "user_id": "alice",
            "window": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        }
    )["Item"]
    # Refreshed parameter price (1 + 2), not the stale env snapshot (200).
    assert row["cost_micro"] == 3 * processor.MICRO
    assert ssm.calls == 1


def test_broken_parameter_falls_back_to_the_deployment_snapshot(
    fake_dynamodb, fake_sns, monkeypatch, capsys
):
    monkeypatch.setenv("BEDROCK_USER_ROLE_NAME", ROLE_NAME)
    monkeypatch.setenv("PRICES_PARAMETER_NAME", "/quota/model-prices")
    monkeypatch.setenv(
        "MODEL_PRICES_JSON",
        '{"m":{"input_per_mtok":1,"output_per_mtok":2}}',
    )
    ssm = _FakeSsmParameters(error=RuntimeError("parameter store down"))
    _seed_user(fake_dynamodb, "alice", usd=1000)
    _seed_session(fake_dynamodb, "alice-session", "alice")

    processor.handler(
        _subscription([
            _record(model="m", input_tokens=1_000_000, output_tokens=1_000_000)
        ]),
        None,
        dynamodb=fake_dynamodb,
        dynamodb_client=FakeDynamoClient(fake_dynamodb),
        sns=fake_sns,
        ssm=ssm,
    )

    row = fake_dynamodb.Table(os.environ["USAGE_TABLE"]).get_item(
        Key={
            "user_id": "alice",
            "window": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        }
    )["Item"]
    # Metering never stops: env snapshot is the availability fallback.
    assert row["cost_micro"] == 3 * processor.MICRO
    warning = next(
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.strip() and "level" in json.loads(line or "{}")
    )
    assert warning["level"] == "warning"


def test_stale_parameter_value_outlives_a_failed_refresh(monkeypatch, capsys):
    monkeypatch.setenv("PRICES_PARAMETER_NAME", "/quota/model-prices")
    ssm = _FakeSsmParameters(
        value=json.dumps({
            "models": {"m": {"input_per_mtok": 1.0, "output_per_mtok": 2.0}},
            "fallback": {"input_per_mtok": 40.0, "output_per_mtok": 90.0},
        })
    )

    first = processor._parameter_prices(ssm, 1_000.0)
    assert first == ({"m": (1.0, 2.0)}, (40.0, 90.0))
    assert ssm.calls == 1

    # Within the TTL, no new fetch.
    assert processor._parameter_prices(ssm, 1_000.0 + 10) == first
    assert ssm.calls == 1

    # After the TTL a refresh is attempted; when it fails the last good
    # value keeps being served (it is fresher than the env snapshot).
    ssm.error = RuntimeError("parameter store down")
    stale = processor._parameter_prices(
        ssm, 1_000.0 + processor._PRICE_CACHE_TTL_SECONDS + 1
    )
    assert stale == first
    assert ssm.calls == 2
    assert "last good value" in capsys.readouterr().out

    # Failures are retried at most once per retry window.
    processor._parameter_prices(
        ssm, 1_000.0 + processor._PRICE_CACHE_TTL_SECONDS + 2
    )
    assert ssm.calls == 2


# ---------------------------------------------------------------------------
# Workload mode: attribution by application-inference-profile ARN
# ---------------------------------------------------------------------------

PROFILE_ARN = (
    "arn:aws:bedrock:us-east-1:111122223333:"
    "application-inference-profile/abc123xyz"
)


def _workload_env(monkeypatch, *, model="us.anthropic.claude-opus-4-7"):
    monkeypatch.setenv("BEDROCK_USER_ROLE_NAME", ROLE_NAME)
    monkeypatch.setenv(
        "MODEL_PRICES_JSON",
        json.dumps(
            {model: {"input_per_mtok": 5.5, "output_per_mtok": 27.5}}
        ),
    )
    monkeypatch.setenv(
        "WORKLOAD_PROFILES_JSON",
        json.dumps(
            {
                "payments": {
                    "workload_id": "workload:payments",
                    "profile_arn": PROFILE_ARN,
                    "model": model,
                }
            }
        ),
    )
    monkeypatch.setenv("DEFAULT_DAILY_USD", "2.5")
    monkeypatch.setenv("DEFAULT_DAILY_INPUT_TOKENS", "111")
    monkeypatch.setenv("DEFAULT_DAILY_OUTPUT_TOKENS", "222")


def _workload_record(**overrides) -> dict:
    """A direct-invocation record: customer principal, profile-ARN modelId."""
    record = _record(
        model=overrides.pop("model", PROFILE_ARN),
        request_id=overrides.pop("request_id", "workload-req-1"),
        **overrides,
    )
    message = json.loads(record["message"])
    message["identity"] = {
        "arn": "arn:aws:iam::111122223333:role/payments-app"
    }
    record["message"] = json.dumps(message)
    return record


def test_workload_invocation_attributes_by_profile_arn(
    fake_dynamodb, fake_sns, monkeypatch
):
    _workload_env(monkeypatch)

    result = _run(
        _subscription(
            [_workload_record(input_tokens=1000, output_tokens=100)]
        ),
        fake_dynamodb,
        fake_sns,
    )

    assert result["processed"] == 1
    assert result["ignored"] == 0
    window = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    usage = (
        fake_dynamodb.Table(os.environ["USAGE_TABLE"])
        .get_item(
            Key={"user_id": "workload:payments", "window": window}
        )
        .get("Item")
    )
    # Priced by the profile's underlying model, not the fallback:
    # 1000 in * 5.5/M + 100 out * 27.5/M = 8250 micro-USD.
    assert usage["cost_micro"] == 8250
    assert usage["input_tokens"] == 1000


def test_workload_row_is_auto_provisioned_with_deploy_defaults(
    fake_dynamodb, fake_sns, monkeypatch
):
    _workload_env(monkeypatch)

    _run(_subscription([_workload_record()]), fake_dynamodb, fake_sns)

    row = (
        fake_dynamodb.Table(os.environ["USERS_TABLE"])
        .get_item(Key={"user_id": "workload:payments"})
        .get("Item")
    )
    assert row["name"] == "payments"
    assert row["status"] == "active"
    assert row["status_origin"] == "automatic"
    assert row["daily_usd_micro"] == 2_500_000
    assert row["daily_input_tokens"] == 111
    assert row["daily_output_tokens"] == 222
    assert row["version"] == 1


def test_workload_auto_provision_never_overwrites_admin_limits(
    fake_dynamodb, fake_sns, monkeypatch
):
    _workload_env(monkeypatch)
    _seed_user(fake_dynamodb, "workload:payments", usd=9.0)

    _run(_subscription([_workload_record()]), fake_dynamodb, fake_sns)

    row = (
        fake_dynamodb.Table(os.environ["USERS_TABLE"])
        .get_item(Key={"user_id": "workload:payments"})
        .get("Item")
    )
    assert row["daily_usd_micro"] == 9 * processor.MICRO


def test_unknown_application_profile_is_ignored(
    fake_dynamodb, fake_sns, monkeypatch
):
    _workload_env(monkeypatch)
    foreign = (
        "arn:aws:bedrock:us-east-1:111122223333:"
        "application-inference-profile/not-ours"
    )

    result = _run(
        _subscription([_workload_record(model=foreign)]),
        fake_dynamodb,
        fake_sns,
    )

    assert result["processed"] == 0
    assert result["ignored"] == 1
    assert result["unresolved_sessions"] == []


def test_workload_usage_is_idempotent_per_request_id(
    fake_dynamodb, fake_sns, monkeypatch
):
    _workload_env(monkeypatch)
    event = _subscription(
        [
            _workload_record(request_id="dup-1"),
            _workload_record(request_id="dup-1"),
        ]
    )

    result = _run(event, fake_dynamodb, fake_sns)

    assert result["processed"] == 1
    assert result["duplicates"] == 1


def test_workload_over_budget_blocks_the_workload_row(
    fake_dynamodb, fake_sns, monkeypatch
):
    _workload_env(monkeypatch)
    monkeypatch.setenv("DEFAULT_DAILY_USD", "0.008")  # 8000 micro

    result = _run(
        _subscription(
            [_workload_record(input_tokens=1000, output_tokens=100)]
        ),
        fake_dynamodb,
        fake_sns,
    )

    assert result["processed"] == 1
    row = (
        fake_dynamodb.Table(os.environ["USERS_TABLE"])
        .get_item(Key={"user_id": "workload:payments"})
        .get("Item")
    )
    assert row["status"] == "blocked"
    assert row["status_origin"] == "automatic"
    assert row["status_reason"].startswith("auto: quota exhausted")


def test_vended_session_via_workload_profile_bills_the_workload(
    fake_dynamodb, fake_sns, monkeypatch
):
    """Deterministic precedence: the profile is the cost object."""
    _workload_env(monkeypatch)
    _seed_user(fake_dynamodb, "alice")
    _seed_session(fake_dynamodb, "alice-session", "alice")
    record = _record(model=PROFILE_ARN, request_id="mixed-1")

    result = _run(_subscription([record]), fake_dynamodb, fake_sns)

    assert result["processed"] == 1
    window = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    workload_usage = (
        fake_dynamodb.Table(os.environ["USAGE_TABLE"])
        .get_item(
            Key={"user_id": "workload:payments", "window": window}
        )
        .get("Item")
    )
    alice_usage = (
        fake_dynamodb.Table(os.environ["USAGE_TABLE"])
        .get_item(Key={"user_id": "alice", "window": window})
        .get("Item")
    )
    assert workload_usage is not None
    assert alice_usage is None
