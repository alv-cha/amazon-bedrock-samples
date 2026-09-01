"""Control plane for runtime-only Amazon Bedrock per-user quotas.

The application is deliberately not an inference proxy. It authenticates an
OIDC identity, checks the latest event-driven usage aggregate, and vends a
short-lived STS session that calls ``bedrock-runtime`` directly. The same API
provides administrative quota management.
"""

from __future__ import annotations

import json
import math
import uuid
from datetime import datetime, timedelta, timezone

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from . import emf
from .auth import (
    Identity,
    JwtError,
    JwtVerifier,
    extract_bearer,
    extract_user_token,
)
from .broker import BrokerError, CredentialBroker
from .config import settings
from .quota import (
    MICRO,
    LeaseExpired,
    LeaseNotRefreshable,
    LeaseRateLimited,
    LeaseReservation,
    QuotaStore,
    UserRecord,
    validate_user_id,
)

app = FastAPI(
    title="Amazon Bedrock Runtime quota broker",
    docs_url=None,
    redoc_url=None,
)

_store: QuotaStore | None = None
_verifier: JwtVerifier | None = None
_broker: CredentialBroker | None = None
_admin_key: str | None = None
_emergency_key: str | None = None
_cloudwatch = None


def store() -> QuotaStore:
    global _store
    if _store is None:
        _store = QuotaStore()
    return _store


def verifier() -> JwtVerifier:
    global _verifier
    if _verifier is None:
        _verifier = JwtVerifier()
    return _verifier


def broker() -> CredentialBroker:
    global _broker
    if _broker is None:
        _broker = CredentialBroker()
    return _broker


def cloudwatch_client():
    global _cloudwatch
    if _cloudwatch is None:
        _cloudwatch = boto3.client(
            "cloudwatch", region_name=settings.aws_region
        )
    return _cloudwatch


def admin_key() -> str:
    global _admin_key
    if _admin_key is None:
        import os

        secret_arn = os.environ.get("ADMIN_KEY_SECRET_ARN")
        if secret_arn:
            secrets = boto3.client(
                "secretsmanager", region_name=settings.aws_region
            )
            _admin_key = secrets.get_secret_value(
                SecretId=secret_arn
            )["SecretString"]
        else:
            _admin_key = os.environ.get("ADMIN_API_KEY", "")
    return _admin_key


def emergency_key() -> str:
    global _emergency_key
    if _emergency_key is None:
        import os

        secret_arn = os.environ.get("EMERGENCY_KEY_SECRET_ARN")
        if secret_arn:
            secrets = boto3.client(
                "secretsmanager", region_name=settings.aws_region
            )
            _emergency_key = secrets.get_secret_value(
                SecretId=secret_arn
            )["SecretString"]
        else:
            _emergency_key = os.environ.get("EMERGENCY_ADMIN_KEY", "")
    return _emergency_key


def _error(
    status: int,
    message: str,
    error_type: str,
    headers: dict | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "message": message,
                "type": error_type,
                "code": error_type,
            }
        },
        headers=headers or {},
    )


def _authenticate(
    request: Request,
) -> tuple[UserRecord | None, Identity | None, str]:
    token = extract_user_token(request.headers)
    if not token:
        return None, None, "Missing bearer token."
    try:
        identity: Identity = verifier().verify(token)
    except JwtError as exc:
        return None, None, exc.reason
    try:
        validate_user_id(identity.user_id)
    except ValueError as exc:
        return None, None, str(exc)

    user = store().get_user(identity.user_id)
    if user is None:
        if not settings.auto_provision_users:
            return (
                None,
                None,
                f"User '{identity.user_id}' is not provisioned on this broker.",
            )
        display_name = str(
            identity.claims.get("email")
            or identity.claims.get("username")
            or identity.claims.get("cognito:username")
            or identity.user_id
        )
        user = store().get_or_provision_user(
            identity.user_id, name=display_name
        )
    return user, identity, ""


def _quota_headers(user: UserRecord) -> dict[str, str]:
    return {
        "X-Quota-Limit-USD": (
            f"{user.daily_usd_micro / MICRO:.6f}"
            if user.daily_usd_micro
            else "unlimited"
        ),
        "X-Quota-Window": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }


def _lease_retry_headers(retry_after: datetime) -> dict[str, str]:
    now = datetime.now(timezone.utc)
    seconds = max(0, math.ceil((retry_after - now).total_seconds()))
    return {
        "Retry-After": str(seconds),
        "X-Quota-Refresh-After": retry_after.isoformat(),
    }


def _reserve_permission_lease(
    request: Request,
    user: UserRecord,
    identity: Identity,
) -> tuple[LeaseReservation | None, JSONResponse | None]:
    if settings.credential_enforcement_mode == "legacy":
        return None, None
    requested_id = request.headers.get("x-quota-lease-id") or str(
        uuid.uuid4()
    )
    raw_expiration = identity.claims.get("exp")
    if (
        isinstance(raw_expiration, bool)
        or not isinstance(raw_expiration, (int, float))
    ):
        return None, _error(
            401,
            "Token has no valid expiration claim.",
            "authentication_error",
        )
    jwt_expiration = datetime.fromtimestamp(
        raw_expiration, tz=timezone.utc
    )
    duration = (
        settings.permission_lease_seconds
        if settings.credential_enforcement_mode == "lease"
        else settings.vended_credential_ttl_seconds
    )
    try:
        reservation = store().reserve_lease(
            user.user_id,
            requested_id,
            expires_no_later_than=jwt_expiration,
            lease_seconds=duration,
        )
    except LeaseRateLimited as exc:
        emf.record_throttle(user.user_id, "-", "vend-rate-limit")
        return None, _error(
            429,
            "Credential vend rate limit exceeded.",
            "lease_rate_limited",
            headers=_lease_retry_headers(exc.retry_after),
        )
    except LeaseNotRefreshable as exc:
        return None, _error(
            429,
            "The current permission lease is not ready for refresh.",
            "lease_not_refreshable",
            headers=_lease_retry_headers(exc.retry_after),
        )
    except LeaseExpired:
        return None, _error(
            409,
            "The supplied lease ID has expired; use a new lease ID.",
            "lease_expired",
        )
    except ValueError as exc:
        return None, _error(400, str(exc), "invalid_lease")
    return reservation, None


@app.post("/v1/credentials")
async def vend_credentials(request: Request) -> Response:
    user, identity, auth_error = _authenticate(request)
    if user is None or identity is None:
        return _error(401, auth_error, "authentication_error")
    if store().emergency_stop_active():
        return _error(
            503,
            "Credential vending is disabled by the emergency stop.",
            "emergency_stop",
            headers={"Retry-After": "60"},
        )

    user = store().refresh_auto_status(user)
    if not user.active:
        return _error(
            403,
            f"User '{user.user_id}' is {user.status}.",
            "quota_blocked",
            headers=_quota_headers(user),
        )
    if store().is_over_budget(user):
        store().set_user_status(
            user.user_id,
            "blocked",
            "auto: quota exhausted at credential vend",
        )
        emf.record_throttle(user.user_id, "-", "over-budget-at-vend")
        return _error(
            429,
            "Daily quota exhausted; credentials not issued.",
            "quota_exceeded",
            headers=_quota_headers(user),
        )

    reservation, lease_error = _reserve_permission_lease(
        request, user, identity
    )
    if lease_error is not None:
        return lease_error
    if reservation is not None:
        # Re-check strongly consistent gates after the conditional lease write
        # and immediately before STS. This narrows block/emergency races; any
        # final IAM authorization race remains bounded by the fixed deadline.
        if store().emergency_stop_active():
            return _error(
                503,
                "Credential vending is disabled by the emergency stop.",
                "emergency_stop",
                headers={"Retry-After": "60"},
            )
        latest_user = store().get_user(user.user_id)
        if latest_user is None or not latest_user.active:
            return _error(
                403,
                f"User '{user.user_id}' is blocked.",
                "quota_blocked",
            )
        if store().is_over_budget(latest_user):
            return _error(
                429,
                "Daily quota exhausted; credentials not issued.",
                "quota_exceeded",
            )

    try:
        credentials = broker().vend(
            identity,
            permission_deadline=(
                reservation.expires_at if reservation else None
            ),
        )
    except BrokerError as exc:
        # Keep a newly reserved logical lease after STS failure. A same-ID
        # retry retains its fixed deadline; deleting it here can race with a
        # concurrent successful retry and allow an early extending lease.
        return _error(exc.status, exc.reason, "broker_error")

    store().record_session(credentials.session_name, user.user_id)
    emf.record_credentials_vended(user.user_id)
    if reservation is not None:
        if not reservation.created:
            lease_event = "LeaseRetried"
        elif reservation.generation == 1:
            lease_event = "LeaseStarted"
        else:
            lease_event = "LeaseRefreshed"
        emf.record_lease_event(
            user.user_id, lease_event, reservation.generation
        )
    payload = {
        "aws_access_key_id": credentials.access_key_id,
        "aws_secret_access_key": credentials.secret_access_key,
        "aws_session_token": credentials.session_token,
        # `expiration` is a no-later-than Bedrock permission deadline.
        # Targeted or emergency denies can terminate access earlier; the
        # underlying STS keys may live longer.
        "expiration": credentials.expiration,
        "sts_expiration": (
            credentials.sts_expiration or credentials.expiration
        ),
        "region": settings.aws_region,
        "user_id": credentials.user_id,
        "endpoint": (
            f"https://bedrock-runtime.{settings.aws_region}.amazonaws.com"
        ),
    }
    if reservation is not None:
        payload.update(
            {
                "lease_id": reservation.lease_id,
                "refresh_after": reservation.refresh_after.isoformat(),
                "lease_generation": reservation.generation,
            }
        )
    return JSONResponse(payload, headers=_quota_headers(user))


@app.get("/healthz")
async def healthz() -> dict:
    return {
        "status": "ok",
        "inference_endpoint": "bedrock-runtime",
        "metering": "cloudwatch-logs-subscription",
    }


def _jwt_grants_admin(token: str) -> bool:
    claim = settings.admin_jwt_claim
    if not claim:
        return False
    try:
        identity = verifier().verify(token)
    except JwtError:
        return False
    value = identity.claims.get(claim)
    required = settings.admin_jwt_value
    if isinstance(value, str):
        return value == required
    if isinstance(value, (list, tuple)):
        return required in value
    return False


def _require_admin(request: Request) -> JSONResponse | None:
    provided = extract_bearer(request.headers.get("x-quota-admin-key"))
    if not provided:
        authorization = request.headers.get("authorization")
        if authorization and authorization.lower().startswith("bearer "):
            provided = extract_bearer(authorization)
    expected = admin_key()
    if expected and provided and provided == expected:
        return None
    token = extract_user_token(request.headers)
    if token and _jwt_grants_admin(token):
        return None
    return _error(403, "Admin authorization required.", "forbidden")


def _require_emergency_admin(
    request: Request,
) -> tuple[JSONResponse | None, str]:
    provided = extract_bearer(request.headers.get("x-quota-emergency-key"))
    expected = emergency_key()
    if expected and provided and provided == expected:
        return None, "emergency-shared-key"
    return (
        _error(
            403,
            "Break-glass emergency authorization required.",
            "forbidden",
        ),
        "",
    )


async def _admin_json_object(
    request: Request,
) -> tuple[dict | None, JSONResponse | None]:
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        return None, _error(
            400,
            "Request body must be a JSON object.",
            "invalid_request_error",
        )
    if not isinstance(body, dict):
        return None, _error(
            400,
            "Request body must be a JSON object.",
            "invalid_request_error",
        )
    return body, None


def _limits_json(user: UserRecord) -> dict:
    return {
        "daily_usd": user.daily_usd_micro / MICRO,
        "daily_input_tokens": user.daily_input_tokens,
        "daily_output_tokens": user.daily_output_tokens,
    }


def _parse_limits(
    body: dict, *, with_defaults: bool
) -> tuple[dict, str]:
    defaults = {
        "daily_usd": settings.default_daily_usd,
        "daily_input_tokens": settings.default_daily_input_tokens,
        "daily_output_tokens": settings.default_daily_output_tokens,
    }
    values: dict = {}
    for field_name, default in defaults.items():
        if field_name not in body:
            if with_defaults:
                values[field_name] = default
            continue
        raw = body[field_name]
        if field_name == "daily_usd":
            if isinstance(raw, bool):
                return {}, f"{field_name} must be a non-negative number."
            try:
                value = float(raw)
            except (TypeError, ValueError):
                return {}, f"{field_name} must be a non-negative number."
            if not math.isfinite(value) or value < 0:
                return {}, f"{field_name} must be a non-negative number."
        else:
            if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
                return {}, f"{field_name} must be a non-negative integer."
            value = raw
        values[field_name] = value
    return values, ""


@app.post("/admin/users")
async def create_user(request: Request) -> Response:
    if (denied := _require_admin(request)) is not None:
        return denied
    body, error = await _admin_json_object(request)
    if error is not None:
        return error
    assert body is not None
    user_id = body.get("user_id")
    if not isinstance(user_id, str) or not user_id.strip():
        return _error(
            400,
            "user_id is required (the configured JWT claim value).",
            "invalid_request_error",
        )
    user_id = user_id.strip()
    try:
        validate_user_id(user_id)
    except ValueError as exc:
        return _error(400, str(exc), "invalid_request_error")
    name = body.get("name", user_id)
    if not isinstance(name, str) or not name.strip():
        return _error(
            400, "name must be a non-empty string.", "invalid_request_error"
        )
    limits, limit_error = _parse_limits(body, with_defaults=True)
    if limit_error:
        return _error(400, limit_error, "invalid_request_error")
    store().put_user(user_id=user_id, name=name.strip(), **limits)
    user = store().get_user(user_id)
    assert user is not None
    return JSONResponse(
        {
            "user_id": user_id,
            "provisioned": True,
            "limits": _limits_json(user),
        }
    )


@app.get("/admin/users")
async def list_users(
    request: Request, limit: int = 50, cursor: str | None = None
) -> Response:
    if (denied := _require_admin(request)) is not None:
        return denied
    if limit < 1 or limit > 1000:
        return _error(
            400, "limit must be between 1 and 1000.", "invalid_request_error"
        )
    try:
        users, next_cursor = store().list_users_page(
            limit=limit, cursor=cursor
        )
    except ValueError:
        return _error(400, "Invalid cursor.", "invalid_request_error")
    return JSONResponse(
        {
            "users": [
                {
                    "user_id": user.user_id,
                    "name": user.name,
                    "status": user.status,
                    "status_reason": user.status_reason,
                    "limits": _limits_json(user),
                    "today": store().get_window_usage(user.user_id),
                }
                for user in users
            ],
            "next_cursor": next_cursor,
        }
    )


def _json_object(raw: str) -> dict:
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _iso_timestamp(value) -> str | None:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return str(value) if value else None


def _metric_points(result: dict) -> list[tuple[datetime, float]]:
    points = []
    for timestamp, value in zip(
        result.get("Timestamps", []), result.get("Values", []), strict=False
    ):
        if isinstance(timestamp, datetime):
            points.append((timestamp.astimezone(timezone.utc), float(value)))
    return sorted(points, key=lambda point: point[0], reverse=True)


def _safe_emergency_state() -> dict:
    raw = store().get_emergency_state()
    state = str(raw.get("state", "inactive"))
    desired_active = bool(raw.get("desired_active", False))
    generation = int(raw.get("generation", 0))
    applied_generation = int(raw.get("applied_generation", 0))
    stable_state = "active" if desired_active else "inactive"
    converged = (
        generation == 0 and state == "inactive" and not desired_active
    ) or (state == stable_state and applied_generation == generation)
    return {
        "state": state,
        "desired_active": desired_active,
        "generation": generation,
        "applied_generation": applied_generation,
        "requested_at": _iso_timestamp(raw.get("requested_at")),
        "applied_at": _iso_timestamp(raw.get("applied_at")),
        "converged": converged,
    }


def _empty_operations_metrics(reconciliation_status: str) -> dict:
    return {
        "namespace": settings.metrics_namespace,
        "detection_lag_metric": "DetectionLagMilliseconds",
        "detection_lag_p95_ms": None,
        "detection_lag_timestamp": None,
        "telemetry_status": "unknown",
        "last_reconciliation_at": None,
        "reconciliation_status": reconciliation_status,
        "revoked_identities_desired": None,
        "recent_sync_failure_count": None,
        "recent_overflow_count": None,
        "recent_emergency_failure_count": None,
        "window_minutes": 15,
    }


def _read_operations_cloudwatch(
    now: datetime,
    *,
    revocation_enabled: bool,
    alarm_names: dict[str, str],
) -> tuple[dict, list[dict], dict]:
    metrics = _empty_operations_metrics(
        "unknown" if revocation_enabled else "not_applicable"
    )
    alarms = [
        {"key": key, "state": "UNAVAILABLE", "updated_at": None}
        for key in alarm_names
    ]
    try:
        client = cloudwatch_client()
        metric_specs = (
            ("detectionlag", "DetectionLagMilliseconds", "p95"),
            ("revsyncsuccess", "RevocationSyncSuccess", "Sum"),
            ("revsyncfailure", "RevocationSyncFailure", "Sum"),
            ("revoverflow", "RevocationPolicyOverflow", "Sum"),
            ("revokeddesired", "RevokedIdentitiesDesired", "Maximum"),
            ("emergencyfailure", "EmergencyStopFailure", "Sum"),
        )
        response = client.get_metric_data(
            MetricDataQueries=[
                {
                    "Id": query_id,
                    "MetricStat": {
                        "Metric": {
                            "Namespace": settings.metrics_namespace,
                            "MetricName": metric_name,
                        },
                        "Period": 300,
                        "Stat": statistic,
                    },
                    "ReturnData": True,
                }
                for query_id, metric_name, statistic in metric_specs
            ],
            StartTime=now - timedelta(hours=24),
            EndTime=now,
            ScanBy="TimestampDescending",
        )
        result_by_id = {
            str(result.get("Id", "")): result
            for result in response.get("MetricDataResults", [])
        }
        expected_ids = {query_id for query_id, _, _ in metric_specs}
        complete_ids = {
            query_id
            for query_id, result in result_by_id.items()
            if result.get("StatusCode") == "Complete"
        }
        incomplete_ids = expected_ids - complete_ids
        results = {
            query_id: _metric_points(result)
            for query_id, result in result_by_id.items()
            if query_id in complete_ids
        }
        recent_cutoff = now - timedelta(minutes=15)
        detection = results.get("detectionlag", [])
        success = results.get("revsyncsuccess", [])
        revoked = results.get("revokeddesired", [])
        recent_failures = sum(
            value
            for timestamp, value in results.get("revsyncfailure", [])
            if timestamp >= recent_cutoff
        )
        recent_overflow = sum(
            value
            for timestamp, value in results.get("revoverflow", [])
            if timestamp >= recent_cutoff
        )
        recent_emergency_failures = sum(
            value
            for timestamp, value in results.get("emergencyfailure", [])
            if timestamp >= recent_cutoff
        )
        metrics["telemetry_status"] = (
            "partial"
            if "detectionlag" in incomplete_ids
            else ("complete" if detection else "no_samples")
        )
        if detection:
            metrics["detection_lag_p95_ms"] = detection[0][1]
            metrics["detection_lag_timestamp"] = detection[0][0].isoformat()
        if success:
            metrics["last_reconciliation_at"] = success[0][0].isoformat()
        if revoked:
            metrics["revoked_identities_desired"] = int(revoked[0][1])
        metrics["recent_sync_failure_count"] = int(recent_failures)
        metrics["recent_overflow_count"] = int(recent_overflow)
        metrics["recent_emergency_failure_count"] = int(
            recent_emergency_failures
        )

        described = client.describe_alarms(
            AlarmNames=list(alarm_names.values())
        ) if alarm_names else {"MetricAlarms": []}
        alarm_by_name = {
            alarm.get("AlarmName"): alarm
            for alarm in described.get("MetricAlarms", [])
        }
        alarms = []
        for key, alarm_name in alarm_names.items():
            alarm = alarm_by_name.get(alarm_name, {})
            alarms.append(
                {
                    "key": key,
                    "state": str(
                        alarm.get("StateValue", "INSUFFICIENT_DATA")
                    ),
                    "updated_at": _iso_timestamp(
                        alarm.get("StateUpdatedTimestamp")
                    ),
                }
            )

        if revocation_enabled:
            revocation_alarm_keys = {
                "revocation_failure",
                "revocation_overflow",
                "revocation_dlq",
                "revocation_iterator_age",
            }
            revocation_alarm_states = {
                alarm["key"]: alarm["state"]
                for alarm in alarms
                if alarm["key"] in revocation_alarm_keys
            }
            revocation_alarm = any(
                state == "ALARM"
                for state in revocation_alarm_states.values()
            )
            revocation_alarms_complete = (
                set(revocation_alarm_states) == revocation_alarm_keys
                and all(
                    state == "OK"
                    for state in revocation_alarm_states.values()
                )
            )
            revocation_queries_complete = {
                "revsyncsuccess",
                "revsyncfailure",
                "revoverflow",
                "revokeddesired",
            }.issubset(complete_ids)
            if recent_failures or recent_overflow or revocation_alarm:
                metrics["reconciliation_status"] = "degraded"
            elif not revocation_queries_complete or not revocation_alarms_complete:
                metrics["reconciliation_status"] = "unknown"
            elif not success:
                metrics["reconciliation_status"] = "unknown"
            else:
                stale_seconds = max(
                    900, settings.revocation_reconcile_minutes * 180
                )
                metrics["reconciliation_status"] = (
                    "stale"
                    if (now - success[0][0]).total_seconds() > stale_seconds
                    else "current"
                )
        cloudwatch_state = "partial" if incomplete_ids else "available"
        return metrics, alarms, {"status": cloudwatch_state}
    except (BotoCoreError, ClientError) as exc:
        error_code = (
            exc.response.get("Error", {}).get("Code", "ClientError")
            if isinstance(exc, ClientError)
            else type(exc).__name__
        )
        return metrics, alarms, {
            "status": "unavailable",
            "error_code": error_code,
        }


@app.get("/admin/operations")
async def admin_operations(request: Request) -> Response:
    if (denied := _require_admin(request)) is not None:
        return denied
    now = datetime.now(timezone.utc)
    mode = {
        "legacy": "bounded_overspend",
        "lease": "permission_lease",
        "revocation": "active_session_revocation",
    }.get(settings.credential_enforcement_mode, "unknown")
    revocation_enabled = settings.credential_enforcement_mode == "revocation"
    post_detection_fallback = (
        settings.permission_lease_seconds
        if settings.credential_enforcement_mode == "lease"
        else settings.vended_credential_ttl_seconds
    )
    alarm_names = {
        str(key): str(value)
        for key, value in _json_object(
            settings.operations_alarm_names_json
        ).items()
        if key and value
    }
    metrics, alarms, cloudwatch_status = _read_operations_cloudwatch(
        now,
        revocation_enabled=revocation_enabled,
        alarm_names=alarm_names,
    )
    qualification = _json_object(settings.qualification_status_json)
    return JSONResponse(
        {
            "as_of": now.isoformat(),
            "configuration": {
                "mode": mode,
                "credential_ttl_seconds": (
                    settings.vended_credential_ttl_seconds
                ),
                "permission_lease_seconds": (
                    settings.permission_lease_seconds
                ),
                "permission_lease_enabled": (
                    settings.credential_enforcement_mode == "lease"
                ),
                "effective_permission_lease_seconds": (
                    settings.permission_lease_seconds
                    if settings.credential_enforcement_mode == "lease"
                    else None
                ),
                "post_detection_fallback_seconds": (
                    post_detection_fallback
                ),
                "refresh_overlap_seconds": settings.refresh_overlap_seconds,
                "refresh_jitter_seconds": settings.refresh_jitter_seconds,
                "vend_rate_limit_per_minute": (
                    settings.vend_rate_limit_per_minute
                ),
                "revocation_enabled": revocation_enabled,
                "revocation_policy_shards": (
                    settings.revocation_policy_shards
                ),
                "revocation_policy_max_characters": (
                    settings.revocation_policy_max_characters
                ),
                "revocation_reconcile_minutes": (
                    settings.revocation_reconcile_minutes
                ),
            },
            "emergency": _safe_emergency_state(),
            "qualification": {
                "status": str(
                    qualification.get(
                        settings.credential_enforcement_mode, "unknown"
                    )
                ),
                "emergency_status": str(
                    qualification.get("emergency", "unknown")
                ),
                "source": "deployment_metadata",
            },
            "metrics": metrics,
            "alarms": alarms,
            "cloudwatch": cloudwatch_status,
        }
    )


@app.get("/admin/emergency-stop")
async def get_emergency_stop(request: Request) -> Response:
    if (denied := _require_admin(request)) is not None:
        return denied
    return JSONResponse(store().get_emergency_state())


@app.post("/admin/emergency-stop", status_code=202)
async def set_emergency_stop(request: Request) -> Response:
    denied, emergency_actor = _require_emergency_admin(request)
    if denied is not None:
        return denied
    body, error = await _admin_json_object(request)
    if error is not None:
        return error
    assert body is not None
    action = body.get("action")
    confirmations = {
        "activate": "STOP_ALL_BEDROCK_SESSIONS",
        "recover": "RESTORE_ALL_BEDROCK_SESSIONS",
    }
    if action not in confirmations:
        return _error(
            400,
            "action must be 'activate' or 'recover'.",
            "invalid_request_error",
        )
    if body.get("confirmation") != confirmations[action]:
        return _error(
            400,
            f"confirmation must be {confirmations[action]!r}.",
            "confirmation_required",
        )
    reason = body.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        return _error(
            400,
            "reason must be a non-empty string.",
            "invalid_request_error",
        )
    desired_active = action == "activate"
    current = store().get_emergency_state()
    stable_state = "active" if desired_active else "inactive"
    if (
        bool(current.get("desired_active")) == desired_active
        and current.get("state") == stable_state
    ):
        return JSONResponse(
            {**current, "idempotent": True, "retry": False},
            status_code=202,
        )
    retry = bool(current.get("desired_active")) == desired_active
    state = store().set_emergency_desired(
        active=desired_active,
        actor=emergency_actor,
        reason=reason.strip(),
    )
    return JSONResponse(
        {**state, "idempotent": False, "retry": retry},
        status_code=202,
    )


@app.get("/admin/summary")
async def admin_summary(request: Request) -> Response:
    if (denied := _require_admin(request)) is not None:
        return denied
    now = datetime.now(timezone.utc)
    users = store().list_users()
    blocked = [user.user_id for user in users if not user.active]
    aggregate = {
        "cost_usd": 0.0,
        "input_tokens": 0,
        "output_tokens": 0,
        "requests": 0,
    }
    for user in users:
        usage = store().get_window_usage(user.user_id)
        for key in aggregate:
            aggregate[key] += usage.get(key, 0)
    enforcement_mode = {
        "legacy": "bounded_overspend",
        "lease": "permission_lease",
        "revocation": "active_session_revocation",
    }.get(settings.credential_enforcement_mode, "unknown")
    post_detection_fallback = (
        settings.permission_lease_seconds
        if settings.credential_enforcement_mode == "lease"
        else settings.vended_credential_ttl_seconds
    )
    return JSONResponse(
        {
            "enforcement": {
                "mode": enforcement_mode,
                "source": "dynamodb",
                "as_of": now.isoformat(),
                "window": now.strftime("%Y-%m-%d"),
                "credential_ttl_seconds": (
                    settings.vended_credential_ttl_seconds
                ),
                "permission_lease_seconds": (
                    settings.permission_lease_seconds
                ),
                "post_detection_fallback_seconds": (
                    post_detection_fallback
                ),
                "refresh_overlap_seconds": (
                    settings.refresh_overlap_seconds
                ),
                "refresh_jitter_seconds": settings.refresh_jitter_seconds,
                "vend_rate_limit_per_minute": (
                    settings.vend_rate_limit_per_minute
                ),
                "revocation_policy_shards": (
                    settings.revocation_policy_shards
                ),
                "revocation_reconcile_minutes": (
                    settings.revocation_reconcile_minutes
                ),
                "total_users": len(users),
                "blocked_users": len(blocked),
                "blocked_user_ids": blocked,
                "today": aggregate,
            },
            "observability": {
                "source": "bedrock_model_invocation_logs",
                "delivery": "cloudwatch_logs_subscription",
                "metrics_namespace": settings.metrics_namespace,
                "detection_lag_metric": "DetectionLagMilliseconds",
            },
        }
    )


@app.get("/admin/users/{user_id:path}/usage")
async def user_usage(
    user_id: str, request: Request, window: str | None = None
) -> Response:
    if (denied := _require_admin(request)) is not None:
        return denied
    return JSONResponse(store().get_window_usage(user_id, window))


@app.put("/admin/users/{user_id:path}/limits")
async def set_limits(user_id: str, request: Request) -> Response:
    if (denied := _require_admin(request)) is not None:
        return denied
    body, error = await _admin_json_object(request)
    if error is not None:
        return error
    assert body is not None
    if store().get_user(user_id) is None:
        return _error(404, f"User '{user_id}' was not found.", "not_found")
    limits, limit_error = _parse_limits(body, with_defaults=False)
    if limit_error:
        return _error(400, limit_error, "invalid_request_error")
    if not limits:
        return _error(
            400,
            "At least one daily quota is required.",
            "invalid_request_error",
        )
    store().set_user_limits(user_id, **limits)
    user = store().get_user(user_id)
    assert user is not None
    return JSONResponse(
        {
            "user_id": user_id,
            "updated": True,
            "limits": _limits_json(user),
        }
    )


@app.put("/admin/users/{user_id:path}/status")
async def set_status(user_id: str, request: Request) -> Response:
    if (denied := _require_admin(request)) is not None:
        return denied
    body, error = await _admin_json_object(request)
    if error is not None:
        return error
    assert body is not None
    if store().get_user(user_id) is None:
        return _error(404, f"User '{user_id}' was not found.", "not_found")
    status = body.get("status")
    if status not in {"active", "blocked"}:
        return _error(
            400,
            "status must be 'active' or 'blocked'.",
            "invalid_request_error",
        )
    reason = body.get("reason", "admin API")
    if not isinstance(reason, str):
        return _error(
            400, "reason must be a string.", "invalid_request_error"
        )
    store().set_user_status(user_id, status, reason)
    return JSONResponse(
        {"user_id": user_id, "status": status, "reason": reason}
    )
