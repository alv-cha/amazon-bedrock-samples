"""Per-user CloudWatch metrics via Embedded Metric Format (EMF).

EMF lines written to stdout are turned into CloudWatch metrics by Lambda
automatically -- no PutMetricData API calls, no extra latency on the hot
path. Locally they are just structured log lines.

Metrics (namespace configurable, default BedrockMantleGateway):
  Requests, Throttles, Errors, InputTokens, OutputTokens, EstimatedCostUSD
Dimensions: [UserId], [UserId, Model], [Model] and service-wide.
"""

import json
import sys
import time

from .config import settings


def _emit(dimensions: list[list[str]], properties: dict, metrics: dict) -> None:
    record = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [{
                "Namespace": settings.metrics_namespace,
                "Dimensions": dimensions,
                "Metrics": [
                    {"Name": name, "Unit": unit}
                    for name, (unit, _val) in metrics.items()
                ],
            }],
        },
        **properties,
        **{name: val for name, (_unit, val) in metrics.items()},
    }
    sys.stdout.write(json.dumps(record) + "\n")
    sys.stdout.flush()


def record_request(user_id: str, model: str, input_tokens: int, output_tokens: int,
                   cost_usd: float, latency_ms: float, status_code: int) -> None:
    _emit(
        dimensions=[["UserId"], ["UserId", "Model"], ["Model"], []],
        properties={"UserId": user_id, "Model": model, "StatusCode": status_code},
        metrics={
            "Requests": ("Count", 1),
            "InputTokens": ("Count", input_tokens),
            "OutputTokens": ("Count", output_tokens),
            "EstimatedCostUSD": ("None", round(cost_usd, 8)),
            "LatencyMs": ("Milliseconds", round(latency_ms, 1)),
        },
    )


def record_throttle(user_id: str, model: str, reason: str) -> None:
    _emit(
        dimensions=[["UserId"], []],
        properties={"UserId": user_id, "Model": model, "Reason": reason},
        metrics={"Throttles": ("Count", 1)},
    )


def record_credentials_vended(user_id: str) -> None:
    """Emitted when the broker hands short-lived Bedrock creds to a user."""
    _emit(
        dimensions=[["UserId"], []],
        properties={"UserId": user_id},
        metrics={"CredentialsVended": ("Count", 1)},
    )


def record_error(user_id: str, model: str, status_code: int) -> None:
    _emit(
        dimensions=[["UserId"], []],
        properties={"UserId": user_id, "Model": model, "StatusCode": status_code},
        metrics={"Errors": ("Count", 1)},
    )
