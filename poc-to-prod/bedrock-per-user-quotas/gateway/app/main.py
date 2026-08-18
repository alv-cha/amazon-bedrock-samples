"""Control plane for runtime-only Amazon Bedrock per-user quotas.

The application is deliberately not an inference proxy. It authenticates an
OIDC identity, checks the latest event-driven usage aggregate, and vends a
short-lived STS session that calls ``bedrock-runtime`` directly. The same API
provides administrative quota management.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

import boto3
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
from .quota import MICRO, QuotaStore, UserRecord

app = FastAPI(
    title="Amazon Bedrock Runtime quota broker",
    docs_url=None,
    redoc_url=None,
)

_store: QuotaStore | None = None
_verifier: JwtVerifier | None = None
_broker: CredentialBroker | None = None
_admin_key: str | None = None


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


def _authenticate(request: Request) -> tuple[UserRecord | None, str]:
    token = extract_user_token(request.headers)
    if not token:
        return None, "Missing bearer token."
    try:
        identity: Identity = verifier().verify(token)
    except JwtError as exc:
        return None, exc.reason

    user = store().get_user(identity.user_id)
    if user is None:
        if not settings.auto_provision_users:
            return (
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
    return user, ""


def _quota_headers(user: UserRecord) -> dict[str, str]:
    return {
        "X-Quota-Limit-USD": (
            f"{user.daily_usd_micro / MICRO:.6f}"
            if user.daily_usd_micro
            else "unlimited"
        ),
        "X-Quota-Window": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }


@app.post("/v1/credentials")
async def vend_credentials(request: Request) -> Response:
    user, auth_error = _authenticate(request)
    if user is None:
        return _error(401, auth_error, "authentication_error")

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

    try:
        credentials = broker().vend(
            Identity(user_id=user.user_id, claims={})
        )
    except BrokerError as exc:
        return _error(exc.status, exc.reason, "broker_error")

    store().record_session(credentials.session_name, user.user_id)
    emf.record_credentials_vended(user.user_id)
    return JSONResponse(
        {
            "aws_access_key_id": credentials.access_key_id,
            "aws_secret_access_key": credentials.secret_access_key,
            "aws_session_token": credentials.session_token,
            "expiration": credentials.expiration,
            "region": settings.aws_region,
            "user_id": credentials.user_id,
            "endpoint": (
                f"https://bedrock-runtime.{settings.aws_region}.amazonaws.com"
            ),
        },
        headers=_quota_headers(user),
    )


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
    return JSONResponse(
        {
            "enforcement": {
                "mode": "bounded_overspend",
                "source": "dynamodb",
                "as_of": now.isoformat(),
                "window": now.strftime("%Y-%m-%d"),
                "credential_ttl_seconds": (
                    settings.vended_credential_ttl_seconds
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
