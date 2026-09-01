"""Credential-broker CloudWatch metrics via Embedded Metric Format (EMF).

EMF lines written to stdout are turned into CloudWatch metrics by Lambda
automatically -- no PutMetricData API calls, no extra latency on the hot
path. Locally they are just structured log lines.

Metrics (namespace configurable, default BedrockQuotaGateway):
  CredentialsVended, LeaseStarted, LeaseRefreshed, LeaseRetried, Throttles
Dimensions: [UserId] and service-wide.
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


def record_lease_event(user_id: str, event: str, generation: int) -> None:
    """Emit one logical-lease lifecycle transition."""
    allowed = {"LeaseStarted", "LeaseRefreshed", "LeaseRetried"}
    if event not in allowed:
        raise ValueError(f"unsupported lease event: {event}")
    _emit(
        dimensions=[["UserId"], []],
        properties={"UserId": user_id, "LeaseGeneration": generation},
        metrics={event: ("Count", 1)},
    )
