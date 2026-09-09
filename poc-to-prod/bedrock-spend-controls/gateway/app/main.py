"""Control plane for Bedrock Spend Controls: runtime-only Bedrock quotas.

The application is deliberately not an inference proxy. It authenticates an
OIDC identity, checks the latest event-driven usage aggregate, and vends a
short-lived STS session that calls ``bedrock-runtime`` directly. The same API
provides administrative quota management.
"""

from __future__ import annotations

import hashlib
import json
import math
import secrets
import uuid
from datetime import date, datetime, timedelta, timezone

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from . import emf
from .auth import (
    AdminPrincipal,
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
    VALID_PERMISSION_LEASE_SECONDS,
    WORKLOAD_USER_ID_PREFIX,
    IdempotencyConflict,
    LeaseExpired,
    LeaseNotRefreshable,
    LeaseRateLimited,
    LeaseReservation,
    QuotaStore,
    UserAlreadyExists,
    UserRecord,
    VersionConflict,
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
    details: dict | None = None,
) -> JSONResponse:
    error = {
        "message": message,
        "type": error_type,
        "code": error_type,
    }
    if details is not None:
        error["details"] = details
    return JSONResponse(
        status_code=status,
        content={"error": error},
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
    if identity.user_id.startswith(WORKLOAD_USER_ID_PREFIX):
        # Workload budgets are attributed by inference profile and enforced
        # on the workload's own IAM principal; a JWT claiming the namespace
        # must never vend credentials against a workload's budget.
        return (
            None,
            None,
            "The 'workload:' namespace is reserved for workload-mode "
            "subjects and cannot authenticate through the vend path.",
        )

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
    lease_seconds: int,
) -> tuple[LeaseReservation | None, JSONResponse | None]:
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
    try:
        reservation = store().reserve_lease(
            user.user_id,
            requested_id,
            expires_no_later_than=jwt_expiration,
            lease_seconds=lease_seconds,
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
    for _ in range(3):
        if not user.active or not store().is_over_budget(user):
            break
        changed = store().set_user_status(
            user.user_id,
            "blocked",
            "auto: quota exhausted at credential vend",
            expected_version=user.version,
            expected_status=user.status,
            expected_reason=user.status_reason,
        )
        latest = store().get_user(user.user_id)
        if latest is None:
            return _error(401, "User is no longer provisioned.", "authentication_error")
        user = latest
        if changed:
            emf.record_throttle(user.user_id, "-", "over-budget-at-vend")
            return _error(
                429,
                "Daily quota exhausted; credentials not issued.",
                "quota_exceeded",
                headers=_quota_headers(user),
            )
    if not user.active:
        return _error(
            403,
            f"User '{user.user_id}' is {user.status}.",
            "quota_blocked",
            headers=_quota_headers(user),
        )
    if store().is_over_budget(user):
        return _error(
            503,
            "Quota state changed concurrently; retry credential vending.",
            "quota_state_conflict",
            headers={"Retry-After": "1"},
        )

    reservation, lease_error = _reserve_permission_lease(
        request, user, identity, store().effective_permission_lease_seconds()
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


def _jwt_admin_principal(token: str) -> AdminPrincipal | None:
    """Verify one admin JWT once and derive a safe server-side actor."""
    claim = settings.admin_jwt_claim
    if not claim:
        return None
    try:
        identity = verifier().verify(token)
    except JwtError:
        return None
    value = identity.claims.get(claim)
    required = settings.admin_jwt_value
    granted = value == required if isinstance(value, str) else (
        required in value if isinstance(value, (list, tuple)) else False
    )
    if not granted:
        return None
    subject = identity.claims.get("sub")
    actor = (
        subject
        if isinstance(subject, str) and subject.strip()
        else identity.user_id
    )
    return AdminPrincipal(actor=actor, auth_method="jwt")


def _require_admin(request: Request) -> JSONResponse | None:
    provided = extract_bearer(request.headers.get("x-quota-admin-key"))
    if not provided:
        authorization = request.headers.get("authorization")
        if authorization and authorization.lower().startswith("bearer "):
            provided = extract_bearer(authorization)
    expected = admin_key()
    principal = None
    if (
        expected
        and provided
        and secrets.compare_digest(provided, expected)
    ):
        principal = AdminPrincipal(
            actor="admin-shared-key", auth_method="shared-key"
        )
    if principal is None:
        token = extract_user_token(request.headers)
        if token:
            principal = _jwt_admin_principal(token)
    if principal is None:
        return _error(403, "Admin authorization required.", "forbidden")
    request.state.admin_principal = principal
    return None


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


def _lease_json(user: UserRecord) -> dict | None:
    """Lease timing for the admin UI. The lease_id itself is the renewal
    token and is deliberately never exposed on admin read paths."""
    if user.lease_expires_at_epoch is None or user.lease_generation is None:
        return None
    now = datetime.now(timezone.utc)
    expires_at = datetime.fromtimestamp(user.lease_expires_at_epoch, tz=timezone.utc)
    refresh_after = (
        datetime.fromtimestamp(user.lease_refresh_after_epoch, tz=timezone.utc)
        if user.lease_refresh_after_epoch is not None
        else None
    )
    lease_seconds = (
        user.lease_duration_seconds
        if user.lease_duration_seconds is not None
        # Rows leased before grant-time persistence: assume the deployment
        # default rather than the current runtime dial.
        else settings.permission_lease_seconds
    )
    return {
        "active": now < expires_at,
        "expires_at": expires_at.isoformat(),
        "refresh_after": refresh_after.isoformat() if refresh_after else None,
        "generation": user.lease_generation,
        "granted_at": datetime.fromtimestamp(
            user.lease_expires_at_epoch - lease_seconds, tz=timezone.utc
        ).isoformat(),
        "lease_seconds": lease_seconds,
    }


def _workload_registry() -> dict:
    return _json_object(settings.workload_enforcement_json)


def _user_json(user: UserRecord) -> dict:
    payload = {
        "user_id": user.user_id,
        "name": user.name,
        "status": user.status,
        "status_reason": user.status_reason,
        "status_origin": user.status_origin,
        "version": user.version,
        "created_at": user.created_at,
        "updated_at": user.updated_at,
        "limits": _limits_json(user),
        "lease": _lease_json(user),
    }
    if user.user_id.startswith(WORKLOAD_USER_ID_PREFIX):
        entry = _workload_registry().get(user.user_id)
        payload["granularity"] = "workload"
        payload["enforcement_ready"] = bool(
            isinstance(entry, dict) and entry.get("enforcement_ready")
        )
    else:
        payload["granularity"] = "user"
    return payload


def _etag(user: UserRecord) -> str:
    return f'"{user.version}"'


def _idempotency_key(request: Request) -> tuple[str | None, JSONResponse | None]:
    key = request.headers.get("idempotency-key")
    if key is None:
        return str(uuid.uuid4()), None
    key = key.strip()
    if not key or len(key) > 256:
        return None, _error(
            400,
            "Idempotency-Key must contain 1 to 256 characters.",
            "invalid_request_error",
        )
    return key, None


def _request_hash(
    request: Request, body: dict, principal: AdminPrincipal
) -> str:
    request_shape = {
        "method": request.method,
        "path": request.url.path,
        "body": body,
        "if_match": request.headers.get("if-match"),
        "actor": principal.actor,
        "auth_method": principal.auth_method,
    }
    query = sorted(request.query_params.multi_items())
    if query:
        request_shape["query"] = query
    canonical = json.dumps(
        request_shape,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _expected_version(
    request: Request, observed: UserRecord
) -> tuple[int | None, JSONResponse | None]:
    raw = request.headers.get("if-match")
    if raw is None:
        return observed.version, None
    value = raw.strip()
    if value.startswith("W/"):
        value = value[2:].strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        value = value[1:-1]
    try:
        version = int(value)
    except (TypeError, ValueError):
        version = -1
    if version < 0:
        return None, _error(
            400,
            "If-Match must contain a non-negative user version.",
            "invalid_request_error",
        )
    return version, None


def _mutation_response(
    payload: dict, user: UserRecord, request_id: str
) -> JSONResponse:
    return JSONResponse(
        {**payload, "user": _user_json(user)},
        headers={"ETag": _etag(user), "X-Request-Id": request_id},
    )


def _version_conflict(error: VersionConflict) -> JSONResponse:
    current = error.current_user
    return _error(
        409,
        "The user configuration changed; refresh and retry.",
        "version_conflict",
        headers={"ETag": _etag(current)},
        details={"current_user": _user_json(current)},
    )


def _transaction_unavailable() -> JSONResponse:
    return _error(
        503,
        "The admin mutation could not be committed; retry the request.",
        "transaction_unavailable",
        headers={"Retry-After": "1"},
    )


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
    principal: AdminPrincipal = request.state.admin_principal
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
    request_id, key_error = _idempotency_key(request)
    if key_error is not None:
        return key_error
    assert request_id is not None
    try:
        result = store().create_admin_user(
            user_id=user_id,
            name=name.strip(),
            **limits,
            actor=principal.actor,
            auth_method=principal.auth_method,
            idempotency_key=request_id,
            request_hash=_request_hash(request, body, principal),
        )
    except IdempotencyConflict:
        return _error(
            409,
            "Idempotency-Key was already used for a different request.",
            "idempotency_conflict",
        )
    except UserAlreadyExists as exc:
        return _error(
            409,
            f"User '{user_id}' already exists.",
            "user_already_exists",
            headers={"ETag": _etag(exc.current_user)},
            details={"current_user": _user_json(exc.current_user)},
        )
    except ClientError:
        return _transaction_unavailable()
    user = result.user
    return _mutation_response(
        {
            "user_id": user_id,
            "provisioned": True,
            "limits": _limits_json(user),
        },
        user,
        request_id,
    )


@app.get("/admin/users")
async def list_users(
    request: Request,
    limit: int = 50,
    cursor: str | None = None,
    status: str | None = None,
    query: str | None = None,
    granularity: str | None = None,
) -> Response:
    if (denied := _require_admin(request)) is not None:
        return denied
    if limit < 1 or limit > 1000:
        return _error(
            400, "limit must be between 1 and 1000.", "invalid_request_error"
        )
    if status is not None and status not in {"active", "blocked"}:
        return _error(
            400,
            "status must be 'active' or 'blocked'.",
            "invalid_request_error",
        )
    if granularity is not None and granularity not in {"user", "workload"}:
        return _error(
            400,
            "granularity must be 'user' or 'workload'.",
            "invalid_request_error",
        )
    try:
        users, next_cursor = store().list_users_page(
            limit=limit,
            cursor=cursor,
            status=status,
            query=query,
            granularity=granularity,
        )
    except ValueError:
        return _error(400, "Invalid cursor.", "invalid_request_error")
    return JSONResponse(
        {
            "users": [
                {**_user_json(user), "today": store().get_window_usage(user.user_id)}
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


@app.get("/admin/audit")
async def admin_audit(
    request: Request,
    user_id: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> Response:
    if (denied := _require_admin(request)) is not None:
        return denied
    if limit < 1 or limit > 100:
        return _error(
            400, "limit must be between 1 and 100.", "invalid_request_error"
        )
    if user_id is not None:
        try:
            validate_user_id(user_id)
        except ValueError as exc:
            return _error(400, str(exc), "invalid_request_error")
    try:
        events, next_cursor = store().list_admin_audit_page(
            user_id=user_id, limit=limit, cursor=cursor
        )
    except ValueError:
        return _error(400, "Invalid cursor.", "invalid_request_error")
    return JSONResponse({"events": events, "next_cursor": next_cursor})


@app.get("/admin/operations")
async def admin_operations(request: Request) -> Response:
    if (denied := _require_admin(request)) is not None:
        return denied
    now = datetime.now(timezone.utc)
    enforcement_config = store().get_enforcement_config()
    effective_lease = int(enforcement_config["permission_lease_seconds"])
    alarm_names = {
        str(key): str(value)
        for key, value in _json_object(
            settings.operations_alarm_names_json
        ).items()
        if key and value
    }
    metrics, alarms, cloudwatch_status = _read_operations_cloudwatch(
        now,
        revocation_enabled=True,
        alarm_names=alarm_names,
    )
    qualification = _json_object(settings.qualification_status_json)
    return JSONResponse(
        {
            "as_of": now.isoformat(),
            "configuration": {
                "mode": "layered",
                "credential_ttl_seconds": (
                    settings.vended_credential_ttl_seconds
                ),
                "permission_lease_seconds": effective_lease,
                "permission_lease_source": enforcement_config["source"],
                "permission_lease_default_seconds": (
                    settings.permission_lease_seconds
                ),
                "permission_lease_enabled": True,
                "effective_permission_lease_seconds": effective_lease,
                "post_detection_fallback_seconds": effective_lease,
                "refresh_overlap_seconds": settings.refresh_overlap_seconds,
                "refresh_jitter_seconds": settings.refresh_jitter_seconds,
                "vend_rate_limit_per_minute": (
                    settings.vend_rate_limit_per_minute
                ),
                "revocation_enabled": True,
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
                "status": str(qualification.get("lease", "unknown")),
                "lease_status": str(qualification.get("lease", "unknown")),
                "revocation_status": str(
                    qualification.get("revocation", "unknown")
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


@app.get("/admin/enforcement")
async def get_enforcement(request: Request) -> Response:
    if (denied := _require_admin(request)) is not None:
        return denied
    config = store().get_enforcement_config()
    return JSONResponse(
        {
            **config,
            "valid_permission_lease_seconds": sorted(
                VALID_PERMISSION_LEASE_SECONDS
            ),
            "default_permission_lease_seconds": (
                settings.permission_lease_seconds
            ),
        }
    )


@app.put("/admin/enforcement")
async def set_enforcement(request: Request) -> Response:
    """Runtime enforcement dial: change the permission-lease window.

    Applies to new vends immediately (the vend path reads the row with a
    strongly consistent get); outstanding credentials keep their issued
    deadline, and the revocation layer keeps cutting blocked identities
    regardless of the dial. No redeploy involved.
    """
    if (denied := _require_admin(request)) is not None:
        return denied
    principal: AdminPrincipal = request.state.admin_principal
    body, error = await _admin_json_object(request)
    if error is not None:
        return error
    assert body is not None
    raw_seconds = body.get("permission_lease_seconds")
    if isinstance(raw_seconds, bool) or not isinstance(raw_seconds, int):
        return _error(
            400,
            "permission_lease_seconds must be an integer.",
            "invalid_request_error",
        )
    reason, reason_error = _admin_reason(body)
    if reason_error is not None:
        return reason_error
    assert reason is not None
    try:
        config = store().set_permission_lease_seconds(
            raw_seconds, actor=principal.actor, reason=reason
        )
    except ValueError as exc:
        return _error(400, str(exc), "invalid_request_error")
    emf.record_enforcement_dial(principal.actor, raw_seconds)
    return JSONResponse(config)


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
    enforcement_config = store().get_enforcement_config()
    effective_lease = int(enforcement_config["permission_lease_seconds"])
    return JSONResponse(
        {
            "enforcement": {
                # Layered enforcement: permission lease bounds every vend,
                # SourceIdentity revocation cuts blocked identities, and the
                # emergency stop halts everything. No modes to select.
                "mode": "layered",
                "source": "dynamodb",
                "as_of": now.isoformat(),
                "window": now.strftime("%Y-%m-%d"),
                "credential_ttl_seconds": (
                    settings.vended_credential_ttl_seconds
                ),
                "permission_lease_seconds": effective_lease,
                "permission_lease_source": enforcement_config["source"],
                "post_detection_fallback_seconds": effective_lease,
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


def _suffix_detail_collision(
    user_id: str, suffix: str
) -> JSONResponse | None:
    """Prefer an exact configured path-like identity over a subresource.

    Starlette's path converter makes ``team/audit`` match both the detail
    identity and the ``team`` audit subresource. Existing configured identity
    wins, preventing the route from returning another user's data.
    """
    exact = store().get_user(f"{user_id}/{suffix}")
    if exact is None:
        return None
    return JSONResponse(
        {"user": _user_json(exact)}, headers={"ETag": _etag(exact)}
    )


def _usage_history_range(
    start: str | None, end: str | None
) -> tuple[tuple[str, str] | None, JSONResponse | None]:
    today = datetime.now(timezone.utc).date()
    oldest = today - timedelta(days=settings.usage_retention_days)

    def parse(value: str | None, default: date) -> date:
        return default if value is None else date.fromisoformat(value)

    try:
        start_date = parse(start, oldest)
        end_date = parse(end, today)
    except (TypeError, ValueError):
        return None, _error(
            400,
            "start and end must be ISO dates (YYYY-MM-DD).",
            "invalid_date_range",
        )
    if start_date > end_date:
        return None, _error(
            400, "start must not be after end.", "invalid_date_range"
        )
    if start_date < oldest or end_date > today:
        return None, _error(
            400,
            "Requested usage range is outside configured retention.",
            "usage_range_outside_retention",
            details={
                "oldest_available_date": oldest.isoformat(),
                "latest_available_date": today.isoformat(),
            },
        )
    return (start_date.isoformat(), end_date.isoformat()), None


LEGACY_ADMIN_REASON = "legacy: reason omitted by compatible admin client"


def _user_id_error(user_id: str) -> JSONResponse | None:
    try:
        validate_user_id(user_id)
    except ValueError as exc:
        return _error(400, str(exc), "invalid_request_error")
    return None


def _canonical_user_id_error(
    request: Request, user_id: str | None
) -> JSONResponse | None:
    values = request.query_params.getlist("user_id")
    if user_id is None or len(values) != 1 or values[0] != user_id:
        return _error(
            400,
            "user_id must be provided exactly once.",
            "invalid_request_error",
        )
    return _user_id_error(user_id)


def _admin_reason(body: dict) -> tuple[str | None, JSONResponse | None]:
    reason = body.get("reason", LEGACY_ADMIN_REASON)
    if not isinstance(reason, str):
        return None, _error(
            400, "reason must be a string.", "invalid_request_error"
        )
    return reason.strip() or LEGACY_ADMIN_REASON, None


def _user_usage_history_response(
    user_id: str,
    start: str | None,
    end: str | None,
    limit: int,
    cursor: str | None,
) -> Response:
    if limit < 1 or limit > 100:
        return _error(
            400, "limit must be between 1 and 100.", "invalid_request_error"
        )
    user = store().get_user(user_id)
    if user is None:
        return _error(404, f"User '{user_id}' was not found.", "not_found")
    date_range, range_error = _usage_history_range(start, end)
    if range_error is not None:
        return range_error
    assert date_range is not None
    try:
        history, next_cursor = store().get_usage_history_page(
            user_id,
            start=date_range[0],
            end=date_range[1],
            limit=limit,
            cursor=cursor,
        )
    except ValueError:
        return _error(400, "Invalid cursor.", "invalid_request_error")
    return JSONResponse(
        {
            "user_id": user_id,
            "start": date_range[0],
            "end": date_range[1],
            "usage": history,
            "next_cursor": next_cursor,
        }
    )


@app.get("/admin/user/usage-history")
async def canonical_user_usage_history(
    request: Request,
    user_id: str | None = None,
    start: str | None = None,
    end: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> Response:
    if (denied := _require_admin(request)) is not None:
        return denied
    if (invalid := _canonical_user_id_error(request, user_id)) is not None:
        return invalid
    assert user_id is not None
    return _user_usage_history_response(user_id, start, end, limit, cursor)


@app.get("/admin/users/{user_id:path}/usage-history")
async def user_usage_history(
    user_id: str,
    request: Request,
    start: str | None = None,
    end: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> Response:
    if (denied := _require_admin(request)) is not None:
        return denied
    if (invalid := _user_id_error(user_id)) is not None:
        return invalid
    if (
        collision := _suffix_detail_collision(user_id, "usage-history")
    ) is not None:
        return collision
    return _user_usage_history_response(user_id, start, end, limit, cursor)


def _user_admin_audit_response(
    user_id: str, limit: int, cursor: str | None
) -> Response:
    if limit < 1 or limit > 100:
        return _error(
            400, "limit must be between 1 and 100.", "invalid_request_error"
        )
    if store().get_user(user_id) is None:
        return _error(404, f"User '{user_id}' was not found.", "not_found")
    try:
        events, next_cursor = store().list_admin_audit_page(
            user_id=user_id, limit=limit, cursor=cursor
        )
    except ValueError:
        return _error(400, "Invalid cursor.", "invalid_request_error")
    return JSONResponse(
        {"user_id": user_id, "events": events, "next_cursor": next_cursor}
    )


@app.get("/admin/user/audit")
async def canonical_user_admin_audit(
    request: Request,
    user_id: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> Response:
    if (denied := _require_admin(request)) is not None:
        return denied
    if (invalid := _canonical_user_id_error(request, user_id)) is not None:
        return invalid
    assert user_id is not None
    return _user_admin_audit_response(user_id, limit, cursor)


@app.get("/admin/users/{user_id:path}/audit")
async def user_admin_audit(
    user_id: str,
    request: Request,
    limit: int = 50,
    cursor: str | None = None,
) -> Response:
    if (denied := _require_admin(request)) is not None:
        return denied
    if (invalid := _user_id_error(user_id)) is not None:
        return invalid
    if (
        collision := _suffix_detail_collision(user_id, "audit")
    ) is not None:
        return collision
    return _user_admin_audit_response(user_id, limit, cursor)


def _user_usage_response(user_id: str, window: str | None) -> Response:
    return JSONResponse(store().get_window_usage(user_id, window))


@app.get("/admin/user/usage")
async def canonical_user_usage(
    request: Request, user_id: str | None = None, window: str | None = None
) -> Response:
    if (denied := _require_admin(request)) is not None:
        return denied
    if (invalid := _canonical_user_id_error(request, user_id)) is not None:
        return invalid
    assert user_id is not None
    return _user_usage_response(user_id, window)


@app.get("/admin/users/{user_id:path}/usage")
async def user_usage(
    user_id: str, request: Request, window: str | None = None
) -> Response:
    if (denied := _require_admin(request)) is not None:
        return denied
    if (invalid := _user_id_error(user_id)) is not None:
        return invalid
    if (collision := _suffix_detail_collision(user_id, "usage")) is not None:
        return collision
    return _user_usage_response(user_id, window)


def _user_detail_response(user_id: str) -> Response:
    user = store().get_user(user_id)
    if user is None:
        return _error(404, f"User '{user_id}' was not found.", "not_found")
    return JSONResponse(
        {"user": _user_json(user)}, headers={"ETag": _etag(user)}
    )


@app.get("/admin/user")
async def canonical_user_detail(
    request: Request, user_id: str | None = None
) -> Response:
    if (denied := _require_admin(request)) is not None:
        return denied
    if (invalid := _canonical_user_id_error(request, user_id)) is not None:
        return invalid
    assert user_id is not None
    return _user_detail_response(user_id)


@app.get("/admin/users/{user_id:path}")
async def get_user_detail(user_id: str, request: Request) -> Response:
    if (denied := _require_admin(request)) is not None:
        return denied
    if (invalid := _user_id_error(user_id)) is not None:
        return invalid
    return _user_detail_response(user_id)


async def _set_limits_response(user_id: str, request: Request) -> Response:
    principal: AdminPrincipal = request.state.admin_principal
    body, error = await _admin_json_object(request)
    if error is not None:
        return error
    assert body is not None
    current = store().get_user(user_id)
    if current is None:
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
    reason, reason_error = _admin_reason(body)
    if reason_error is not None:
        return reason_error
    assert reason is not None
    canonical_body = (
        {**body, "reason": reason} if "reason" in body else body
    )
    expected, match_error = _expected_version(request, current)
    if match_error is not None:
        return match_error
    request_id, key_error = _idempotency_key(request)
    if key_error is not None:
        return key_error
    assert expected is not None and request_id is not None
    try:
        result = store().update_admin_limits(
            user_id,
            limits,
            reason=reason,
            expected_version=expected,
            actor=principal.actor,
            auth_method=principal.auth_method,
            idempotency_key=request_id,
            request_hash=_request_hash(
                request, canonical_body, principal
            ),
        )
    except IdempotencyConflict:
        return _error(
            409,
            "Idempotency-Key was already used for a different request.",
            "idempotency_conflict",
        )
    except VersionConflict as exc:
        return _version_conflict(exc)
    except KeyError:
        return _error(404, f"User '{user_id}' was not found.", "not_found")
    except ClientError:
        return _transaction_unavailable()
    user = result.user
    return _mutation_response(
        {
            "user_id": user_id,
            "updated": True,
            "limits": _limits_json(user),
        },
        user,
        request_id,
    )


@app.put("/admin/user/limits")
async def canonical_set_limits(
    request: Request, user_id: str | None = None
) -> Response:
    if (denied := _require_admin(request)) is not None:
        return denied
    if (invalid := _canonical_user_id_error(request, user_id)) is not None:
        return invalid
    assert user_id is not None
    return await _set_limits_response(user_id, request)


@app.put("/admin/users/{user_id:path}/limits")
async def set_limits(user_id: str, request: Request) -> Response:
    if (denied := _require_admin(request)) is not None:
        return denied
    if (invalid := _user_id_error(user_id)) is not None:
        return invalid
    return await _set_limits_response(user_id, request)


async def _set_status_response(user_id: str, request: Request) -> Response:
    principal: AdminPrincipal = request.state.admin_principal
    body, error = await _admin_json_object(request)
    if error is not None:
        return error
    assert body is not None
    current = store().get_user(user_id)
    if current is None:
        return _error(404, f"User '{user_id}' was not found.", "not_found")
    status = body.get("status")
    if status not in {"active", "blocked"}:
        return _error(
            400,
            "status must be 'active' or 'blocked'.",
            "invalid_request_error",
        )
    reason, reason_error = _admin_reason(body)
    if reason_error is not None:
        return reason_error
    assert reason is not None
    canonical_body = {**body, "reason": reason}
    expected, match_error = _expected_version(request, current)
    if match_error is not None:
        return match_error
    request_id, key_error = _idempotency_key(request)
    if key_error is not None:
        return key_error
    assert expected is not None and request_id is not None
    try:
        result = store().update_admin_status(
            user_id,
            status,
            reason,
            expected_version=expected,
            actor=principal.actor,
            auth_method=principal.auth_method,
            idempotency_key=request_id,
            request_hash=_request_hash(request, canonical_body, principal),
        )
    except IdempotencyConflict:
        return _error(
            409,
            "Idempotency-Key was already used for a different request.",
            "idempotency_conflict",
        )
    except VersionConflict as exc:
        return _version_conflict(exc)
    except KeyError:
        return _error(404, f"User '{user_id}' was not found.", "not_found")
    except ClientError:
        return _transaction_unavailable()
    user = result.user
    return _mutation_response(
        {"user_id": user_id, "status": status, "reason": reason},
        user,
        request_id,
    )


@app.put("/admin/user/status")
async def canonical_set_status(
    request: Request, user_id: str | None = None
) -> Response:
    if (denied := _require_admin(request)) is not None:
        return denied
    if (invalid := _canonical_user_id_error(request, user_id)) is not None:
        return invalid
    assert user_id is not None
    return await _set_status_response(user_id, request)


@app.put("/admin/users/{user_id:path}/status")
async def set_status(user_id: str, request: Request) -> Response:
    if (denied := _require_admin(request)) is not None:
        return denied
    if (invalid := _user_id_error(user_id)) is not None:
        return invalid
    return await _set_status_response(user_id, request)
