"""Per-user quota gateway for Amazon Bedrock.

Users authenticate with the JWT their application already uses (any OIDC
IdP — Cognito, Okta, Auth0, ...); the quota identity is a configurable claim
(default ``sub``). Two enforcement modes share this app:

- **Mode A — credential broker** (``POST /v1/credentials``, recommended):
  verifies the JWT, checks the user's budget, and vends short-lived AWS
  credentials so the app calls ``bedrock-runtime`` natively (any API/provider).
  The gateway is out of the data path; metering is done asynchronously by the
  reconciler from Bedrock model-invocation logs.

- **Mode B — inline proxy** (``/v1/*`` and ``/anthropic/v1/*``): a drop-in
  OpenAI/Anthropic-compatible proxy for the ``bedrock-mantle`` endpoint that
  reserves each request's worst case against the user's daily budgets, forwards
  it with a short-term Bedrock token minted from the gateway's own IAM role,
  then settles the counters with real usage (including from SSE streams).
  Over-budget requests receive HTTP 429 before any tokens are spent upstream.

      client = OpenAI(
          base_url="https://<gateway>/v1",
          http_client=httpx.Client(
              auth=FunctionUrlSigV4Auth("<user's JWT>", "<region>")
          ),
      )
"""

import json
import math
import time

import boto3
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from . import emf
from .auth import Identity, JwtError, JwtVerifier, extract_bearer, extract_user_token
from .broker import BrokerError, CredentialBroker
from .config import settings
from .metering import (
    SseUsageExtractor,
    estimate_input_tokens,
    extract_usage_json,
    requested_max_output_tokens,
)
from .pricing import MICRO
from .quota import QuotaStore, Reservation, UserRecord
from .upstream import MantleClient, response_headers

app = FastAPI(title="Bedrock per-user quota gateway", docs_url=None, redoc_url=None)

_store: QuotaStore | None = None
_mantle: MantleClient | None = None
_verifier: JwtVerifier | None = None
_broker: "CredentialBroker | None" = None
_admin_key: str | None = None


def store() -> QuotaStore:
    global _store
    if _store is None:
        _store = QuotaStore()
    return _store


def mantle() -> MantleClient:
    global _mantle
    if _mantle is None:
        _mantle = MantleClient()
    return _mantle


def verifier() -> JwtVerifier:
    global _verifier
    if _verifier is None:
        _verifier = JwtVerifier()
    return _verifier


def broker() -> "CredentialBroker":
    global _broker
    if _broker is None:
        _broker = CredentialBroker()
    return _broker


def admin_key() -> str:
    """Admin key from Secrets Manager (ADMIN_KEY_SECRET_ARN) or env (local dev)."""
    global _admin_key
    if _admin_key is None:
        import os
        secret_arn = os.environ.get("ADMIN_KEY_SECRET_ARN")
        if secret_arn:
            sm = boto3.client("secretsmanager", region_name=settings.aws_region)
            _admin_key = sm.get_secret_value(SecretId=secret_arn)["SecretString"]
        else:
            _admin_key = os.environ.get("ADMIN_API_KEY", "")
    return _admin_key


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _error(status: int, message: str, err_type: str, headers: dict | None = None) -> JSONResponse:
    """OpenAI-style error body — SDKs surface these messages verbatim."""
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": err_type, "code": err_type}},
        headers=headers or {},
    )


def _authenticate(request: Request) -> tuple[UserRecord | None, str]:
    """Verify the caller's JWT and resolve the quota user record.

    Returns (user, "") on success or (None, reason) on failure. The token
    is normally read from X-Quota-User-Token so SigV4 can own Authorization.
    Bearer Authorization and x-api-key remain supported for local deployments.
    """
    token = extract_user_token(request.headers)
    if not token:
        return None, "Missing bearer token."
    try:
        identity: Identity = verifier().verify(token)
    except JwtError as e:
        return None, e.reason

    user = store().get_user(identity.user_id, use_cache=True)
    if user is None:
        if not settings.auto_provision_users:
            return None, f"User '{identity.user_id}' is not provisioned on this gateway."
        display_name = str(
            identity.claims.get("email")
            or identity.claims.get("username")
            or identity.claims.get("cognito:username")
            or identity.user_id
        )
        user = store().get_or_provision_user(identity.user_id, name=display_name)
    return user, ""


def _quota_headers(user: UserRecord, remaining_usd_micro: int | None) -> dict[str, str]:
    # A 0 USD limit means "unlimited" (see quota._UNLIMITED_HEADROOM), so
    # report "unlimited" rather than a misleading 0.000000 cap/remaining.
    if not user.daily_usd_micro:
        return {"X-Quota-Limit-USD": "unlimited"}
    headers = {"X-Quota-Limit-USD": f"{user.daily_usd_micro / MICRO:.6f}"}
    if remaining_usd_micro is not None:
        headers["X-Quota-Remaining-USD"] = f"{max(remaining_usd_micro, 0) / MICRO:.6f}"
    return headers


def _model_allowed(model_id: str) -> bool:
    allowed = settings.mode_b_allowed_model_ids
    return not allowed or model_id in allowed


def _model_not_allowed(model_id: str) -> JSONResponse:
    return _error(
        403,
        f"Model '{model_id}' is not allowed by this Mode B deployment.",
        "model_not_allowed",
    )


# ---------------------------------------------------------------------------
# Credential broker: the primary, API-agnostic path.
#
# Instead of proxying inference, hand an in-budget user short-lived AWS
# credentials (RoleSessionName + SourceIdentity = a sanitized id derived from
# their identity claim, reverse-mapped for metering) so they can
# call Bedrock natively on any API/provider. Enforcement is at vend time:
# blocked/over-budget users get 403 and simply can't obtain fresh creds, so
# they lose access at their current session's TTL (bounded overspend).
# ---------------------------------------------------------------------------

def _is_over_budget(user: UserRecord) -> bool:
    """Same-window budget check used to gate credential vending."""
    usage = store().get_window_usage(user.user_id)
    cost_micro = int(round(usage.get("cost_usd", 0.0) * MICRO))
    if user.daily_usd_micro and cost_micro >= user.daily_usd_micro:
        return True
    if user.daily_input_tokens and usage.get("input_tokens", 0) >= user.daily_input_tokens:
        return True
    if user.daily_output_tokens and usage.get("output_tokens", 0) >= user.daily_output_tokens:
        return True
    return False


@app.post("/v1/credentials")
async def vend_credentials(request: Request) -> Response:
    user, auth_error = _authenticate(request)
    if user is None:
        return _error(401, auth_error, "authentication_error")
    if not user.active:
        return _error(403, f"User '{user.user_id}' is {user.status}.", "quota_blocked",
                      headers=_quota_headers(user, None))
    if _is_over_budget(user):
        # Reflect the block so the reconciler/alerts and future vends agree.
        store().set_user_status(user.user_id, "blocked", "auto: over budget at vend time")
        emf.record_throttle(user.user_id, "-", "over-budget-at-vend")
        return _error(429, "Daily quota exhausted; credentials not issued.",
                      "quota_exceeded", headers=_quota_headers(user, 0))

    try:
        creds = broker().vend(Identity(user_id=user.user_id, claims={}))
    except BrokerError as e:
        return _error(e.status, e.reason, "broker_error")

    # Persist RoleSessionName -> user_id so the reconciler can attribute
    # model-invocation-log usage (which carries the session name in the ARN)
    # back to this user's budget row.
    store().record_session(creds.session_name, user.user_id)
    emf.record_credentials_vended(user.user_id)

    return JSONResponse(
        {
            "aws_access_key_id": creds.access_key_id,
            "aws_secret_access_key": creds.secret_access_key,
            "aws_session_token": creds.session_token,
            "expiration": creds.expiration,
            "region": settings.aws_region,
            "user_id": creds.user_id,
        },
        headers=_quota_headers(user, None),
    )


# ---------------------------------------------------------------------------
# Inference proxy (Responses / Chat Completions / Anthropic Messages)
# ---------------------------------------------------------------------------

async def _proxy_inference(request: Request, path: str) -> Response:
    user, auth_error = _authenticate(request)
    if user is None:
        return _error(401, f"Unauthorized: {auth_error}", "invalid_api_key")

    raw = await request.body()
    try:
        body = json.loads(raw)
        assert isinstance(body, dict)
    except (ValueError, AssertionError):
        return _error(400, "Request body must be a JSON object.", "invalid_request_error")

    model_id = body.get("model")
    if not isinstance(model_id, str) or not model_id:
        return _error(400, "Missing required field: model.", "invalid_request_error")
    if not _model_allowed(model_id):
        return _model_not_allowed(model_id)

    streaming = bool(body.get("stream", False))

    # --- reserve -------------------------------------------------------
    est_in = estimate_input_tokens(body, settings.chars_per_token)
    max_out = requested_max_output_tokens(body, settings.fallback_max_output_tokens)
    decision = store().reserve(user, model_id, est_in, max_out)
    if not decision.allowed:
        emf.record_throttle(user.user_id, model_id, decision.reason)
        return _error(
            429,
            f"Daily quota exceeded for user '{user.user_id}': {decision.reason}. "
            "The quota resets at 00:00 UTC.",
            "quota_exceeded",
            headers=_quota_headers(user, 0),
        )
    reservation = decision.reservation
    assert reservation is not None

    # Chat Completions streams only report usage when asked to.
    if streaming and path == "/v1/chat/completions":
        opts = body.get("stream_options") or {}
        opts["include_usage"] = True
        body["stream_options"] = opts
        raw = json.dumps(body).encode("utf-8")

    started = time.monotonic()
    incoming = dict(request.headers)

    if streaming:
        return await _stream_upstream(user, reservation, model_id, path, raw, incoming, started, decision)
    return await _forward_upstream(user, reservation, model_id, path, raw, incoming, started, decision)


async def _forward_upstream(user, reservation: Reservation, model_id, path, raw,
                            incoming, started, decision) -> Response:
    try:
        upstream = await mantle().post_json(path, raw, incoming)
    except Exception:
        store().settle(reservation, None, None, failed=True)
        emf.record_error(user.user_id, model_id, 502)
        return _error(502, "Upstream bedrock-mantle request failed.", "upstream_error")

    latency_ms = (time.monotonic() - started) * 1000

    if upstream.status_code >= 400:
        # Nothing was generated — release the reservation, pass the error through.
        store().settle(reservation, None, None, failed=True)
        emf.record_error(user.user_id, model_id, upstream.status_code)
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            headers=response_headers(upstream),
        )

    try:
        usage = extract_usage_json(upstream.json())
    except ValueError:
        usage = None

    if usage and usage.found:
        cost_micro = store().settle(
            reservation, usage.input_tokens, usage.output_tokens,
            cache_write_tokens=usage.cache_write_tokens,
            cache_read_tokens=usage.cache_read_tokens,
        )
        in_tokens = usage.input_tokens + usage.cache_write_tokens + usage.cache_read_tokens
        out_tokens = usage.output_tokens
    else:
        cost_micro = store().settle(reservation, None, None)
        in_tokens = reservation.reserved_input_tokens
        out_tokens = reservation.reserved_output_tokens
    emf.record_request(user.user_id, model_id, in_tokens, out_tokens,
                       cost_micro / MICRO, latency_ms, upstream.status_code)

    headers = response_headers(upstream)
    headers.update(_quota_headers(user, decision.remaining_usd_micro))
    return Response(content=upstream.content, status_code=upstream.status_code, headers=headers)


async def _stream_upstream(user, reservation: Reservation, model_id, path, raw,
                           incoming, started, decision) -> Response:
    try:
        upstream = await mantle().post_stream(path, raw, incoming)
    except Exception:
        store().settle(reservation, None, None, failed=True)
        emf.record_error(user.user_id, model_id, 502)
        return _error(502, "Upstream bedrock-mantle request failed.", "upstream_error")

    if upstream.status_code >= 400:
        content = await upstream.aread()
        await upstream.aclose()
        store().settle(reservation, None, None, failed=True)
        emf.record_error(user.user_id, model_id, upstream.status_code)
        return Response(content=content, status_code=upstream.status_code,
                        headers=response_headers(upstream))

    extractor = SseUsageExtractor()

    async def tee():
        try:
            async for chunk in upstream.aiter_bytes():
                extractor.feed(chunk)
                yield chunk
        finally:
            await upstream.aclose()
            extractor.close()
            usage = extractor.usage
            if usage.found:
                cost_micro = store().settle(
                    reservation, usage.input_tokens, usage.output_tokens,
                    cache_write_tokens=usage.cache_write_tokens,
                    cache_read_tokens=usage.cache_read_tokens,
                )
                in_tokens = usage.input_tokens + usage.cache_write_tokens + usage.cache_read_tokens
                out_tokens = usage.output_tokens
            else:
                cost_micro = store().settle(reservation, None, None)
                in_tokens = reservation.reserved_input_tokens
                out_tokens = reservation.reserved_output_tokens
            emf.record_request(
                user.user_id, model_id, in_tokens, out_tokens,
                cost_micro / MICRO, (time.monotonic() - started) * 1000, upstream.status_code,
            )

    headers = response_headers(upstream)
    headers.update(_quota_headers(user, decision.remaining_usd_micro))
    return StreamingResponse(tee(), status_code=upstream.status_code,
                             headers=headers, media_type="text/event-stream")


@app.post("/v1/responses")
async def responses(request: Request) -> Response:
    return await _proxy_inference(request, "/v1/responses")


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Response:
    return await _proxy_inference(request, "/v1/chat/completions")


@app.post("/anthropic/v1/messages")
async def anthropic_messages(request: Request) -> Response:
    # Mirrors mantle's own path: Anthropic SDK users set
    # base_url = "<gateway>/anthropic".
    return await _proxy_inference(request, "/anthropic/v1/messages")


@app.post("/v1/messages")
async def anthropic_messages_alias(request: Request) -> Response:
    # Convenience alias for clients that put everything under /v1 — this is
    # also the path Claude Code hits when ANTHROPIC_BASE_URL points here.
    return await _proxy_inference(request, "/anthropic/v1/messages")


# Pass-throughs that don't consume the generation quota -------------------

@app.post("/anthropic/v1/messages/count_tokens")
@app.post("/v1/messages/count_tokens")
async def count_tokens(request: Request) -> Response:
    """Token counting (used heavily by Claude Code). Free — no reservation."""
    user, auth_error = _authenticate(request)
    if user is None:
        return _error(401, f"Unauthorized: {auth_error}", "invalid_api_key")
    raw = await request.body()
    try:
        body = json.loads(raw)
        model_id = body.get("model")
    except (ValueError, AttributeError):
        return _error(400, "Request body must be a JSON object.",
                      "invalid_request_error")
    if not isinstance(model_id, str) or not model_id:
        return _error(400, "Missing required field: model.", "invalid_request_error")
    if not _model_allowed(model_id):
        return _model_not_allowed(model_id)
    upstream = await mantle().post_json("/anthropic/v1/messages/count_tokens",
                                        raw, dict(request.headers))
    return Response(content=upstream.content, status_code=upstream.status_code,
                    headers=response_headers(upstream))

@app.get("/v1/models")
async def list_models(request: Request) -> Response:
    user, auth_error = _authenticate(request)
    if user is None:
        return _error(401, f"Unauthorized: {auth_error}", "invalid_api_key")
    upstream = await mantle().get("/v1/models", {})
    content = upstream.content
    if settings.mode_b_allowed_model_ids and upstream.status_code < 400:
        try:
            payload = upstream.json()
            if isinstance(payload, dict) and isinstance(payload.get("data"), list):
                payload["data"] = [
                    model for model in payload["data"]
                    if isinstance(model, dict)
                    and str(model.get("id", "")) in settings.mode_b_allowed_model_ids
                ]
                content = json.dumps(payload).encode("utf-8")
        except ValueError:
            pass
    return Response(content=content, status_code=upstream.status_code,
                    headers=response_headers(upstream))


@app.get("/v1/responses/{response_id}")
async def get_response(response_id: str, request: Request) -> Response:
    user, auth_error = _authenticate(request)
    if user is None:
        return _error(401, f"Unauthorized: {auth_error}", "invalid_api_key")
    upstream = await mantle().get(f"/v1/responses/{response_id}", dict(request.headers))
    return Response(content=upstream.content, status_code=upstream.status_code,
                    headers=response_headers(upstream))


@app.delete("/v1/responses/{response_id}")
async def delete_response(response_id: str, request: Request) -> Response:
    """Stored-response cleanup (Codex and other Responses-API agents)."""
    user, auth_error = _authenticate(request)
    if user is None:
        return _error(401, f"Unauthorized: {auth_error}", "invalid_api_key")
    upstream = await mantle().delete(f"/v1/responses/{response_id}", dict(request.headers))
    return Response(content=upstream.content, status_code=upstream.status_code,
                    headers=response_headers(upstream))


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok", "upstream": settings.base_url}


# ---------------------------------------------------------------------------
# Admin API
# ---------------------------------------------------------------------------

def _require_admin(request: Request) -> JSONResponse | None:
    provided = extract_bearer(request.headers.get("x-quota-admin-key"))
    if not provided:
        authorization = request.headers.get("authorization")
        if authorization and authorization.lower().startswith("bearer "):
            provided = extract_bearer(authorization)
    expected = admin_key()
    if not expected or provided != expected:
        return _error(403, "Admin authorization required.", "forbidden")
    return None


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


def _parse_limits(body: dict, *, with_defaults: bool) -> tuple[dict, str]:
    defaults = {
        "daily_usd": settings.default_daily_usd,
        "daily_input_tokens": settings.default_daily_input_tokens,
        "daily_output_tokens": settings.default_daily_output_tokens,
    }
    values = {}
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
    """Pre-provision a user (by their IdP subject) with non-default limits.

    Optional when AUTO_PROVISION_USERS is on — users appear automatically
    with default limits on their first authenticated request.
    """
    if (deny := _require_admin(request)) is not None:
        return deny
    body, error = await _admin_json_object(request)
    if error is not None:
        return error
    assert body is not None
    user_id = body.get("user_id")
    if not isinstance(user_id, str) or not user_id.strip():
        return _error(400, "user_id is required (the IdP subject / configured claim value).",
                      "invalid_request_error")
    user_id = user_id.strip()
    name = body.get("name", user_id)
    if not isinstance(name, str) or not name.strip():
        return _error(400, "name must be a non-empty string.",
                      "invalid_request_error")
    limits, limit_error = _parse_limits(body, with_defaults=True)
    if limit_error:
        return _error(400, limit_error, "invalid_request_error")
    store().put_user(
        user_id=user_id,
        name=name.strip(),
        **limits,
    )
    user = store().get_user(user_id)
    assert user is not None
    return JSONResponse({
        "user_id": user_id,
        "provisioned": True,
        "limits": _limits_json(user),
    })


@app.get("/admin/users")
async def list_users(request: Request) -> Response:
    if (deny := _require_admin(request)) is not None:
        return deny
    out = []
    for user in store().list_users():
        usage = store().get_window_usage(user.user_id)
        out.append({
            "user_id": user.user_id, "name": user.name, "status": user.status,
            "limits": _limits_json(user),
            "today": usage,
        })
    return JSONResponse({"users": out})


@app.get("/admin/users/{user_id}/usage")
async def user_usage(user_id: str, request: Request, window: str | None = None) -> Response:
    if (deny := _require_admin(request)) is not None:
        return deny
    return JSONResponse(store().get_window_usage(user_id, window))


@app.put("/admin/users/{user_id}/limits")
async def set_limits(user_id: str, request: Request) -> Response:
    if (deny := _require_admin(request)) is not None:
        return deny
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
            "At least one of daily_usd, daily_input_tokens, or "
            "daily_output_tokens is required.",
            "invalid_request_error",
        )
    store().set_user_limits(user_id, **limits)
    user = store().get_user(user_id)
    assert user is not None
    return JSONResponse({
        "user_id": user_id,
        "updated": True,
        "limits": _limits_json(user),
    })


@app.put("/admin/users/{user_id}/status")
async def set_status(user_id: str, request: Request) -> Response:
    if (deny := _require_admin(request)) is not None:
        return deny
    body, error = await _admin_json_object(request)
    if error is not None:
        return error
    assert body is not None
    if store().get_user(user_id) is None:
        return _error(404, f"User '{user_id}' was not found.", "not_found")
    status = body.get("status")
    if status not in ("active", "blocked"):
        return _error(400, "status must be 'active' or 'blocked'.", "invalid_request_error")
    store().set_user_status(user_id, status, body.get("reason", "admin API"))
    return JSONResponse({"user_id": user_id, "status": status})
