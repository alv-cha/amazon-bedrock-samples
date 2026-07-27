"""Central configuration for the quota gateway.

Everything is driven by environment variables so the same code runs in
Lambda (set by CDK) and locally (uvicorn + a .env file).
"""

import json
import os
from dataclasses import dataclass, field


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_string_set(name: str) -> frozenset[str]:
    raw = _env(name, "[]")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} must contain a JSON array") from exc
    if not isinstance(parsed, list) or any(
        not isinstance(item, str) or not item for item in parsed
    ):
        raise ValueError(f"{name} must contain a JSON array of non-empty strings")
    return frozenset(parsed)


@dataclass(frozen=True)
class Settings:
    # --- upstream (bedrock-mantle) ---
    aws_region: str = field(default_factory=lambda: _env("MANTLE_REGION", _env("AWS_REGION", "us-east-1")))
    # Full base URL wins if set; otherwise it is derived from the region.
    mantle_base_url: str = field(default_factory=lambda: _env("MANTLE_BASE_URL", ""))

    # Bedrock Project (Mantle cost-attribution boundary) applied to Mode B
    # inference when a user has no per-user mantle_project_id. "default" is the
    # account's built-in project; set your own via DEFAULT_MANTLE_PROJECT_ID.
    default_mantle_project_id: str = field(
        default_factory=lambda: _env("DEFAULT_MANTLE_PROJECT_ID", "default"))

    # --- storage ---
    users_table: str = field(default_factory=lambda: _env("USERS_TABLE", "bedrock-quota-users"))
    usage_table: str = field(default_factory=lambda: _env("USAGE_TABLE", "bedrock-quota-usage"))

    # --- JWT auth (bring your own IdP) ---
    # Expected token issuer, e.g. https://cognito-idp.us-east-1.amazonaws.com/<pool-id>
    jwt_issuer: str = field(default_factory=lambda: _env("JWT_ISSUER", ""))
    # Expected audience (e.g. your app client id). Empty = not enforced.
    jwt_audience: str = field(default_factory=lambda: _env("JWT_AUDIENCE", ""))
    # JWKS URL; resolved from the issuer's OIDC discovery document if empty.
    jwt_jwks_url: str = field(default_factory=lambda: _env("JWT_JWKS_URL", ""))
    # Claim used as the quota identity (default: OIDC subject).
    jwt_user_claim: str = field(default_factory=lambda: _env("JWT_USER_CLAIM", "sub"))
    # HS256 shared secret for dev/test only; disables JWKS verification.
    jwt_shared_secret: str = field(default_factory=lambda: _env("JWT_SHARED_SECRET", ""))
    # Create a user record with default limits on first authenticated request.
    auto_provision_users: bool = field(
        default_factory=lambda: _env("AUTO_PROVISION_USERS", "true").lower() == "true")

    # --- admin-by-JWT (lets the browser UI authorize with a normal login) ---
    # A verified JWT authorizes the /admin API when this claim contains the
    # required value (string claim equal to it, or list claim containing it).
    # e.g. ADMIN_JWT_CLAIM=cognito:groups, ADMIN_JWT_VALUE=quota-admins.
    # Empty ADMIN_JWT_CLAIM disables JWT-based admin (shared key only), so the
    # admin secret is never required in a browser.
    admin_jwt_claim: str = field(default_factory=lambda: _env("ADMIN_JWT_CLAIM", ""))
    admin_jwt_value: str = field(default_factory=lambda: _env("ADMIN_JWT_VALUE", ""))

    # --- credential broker (per-user short-lived AWS creds) ---
    # Role the broker assumes on behalf of an in-budget user; scoped to
    # Bedrock invoke actions only. Its trust policy must let the broker's
    # Lambda role call sts:AssumeRole + sts:SetSourceIdentity + sts:TagSession.
    bedrock_user_role_arn: str = field(default_factory=lambda: _env("BEDROCK_USER_ROLE_ARN", ""))
    # Lifetime of vended creds. Shorter = tighter overspend bound (a blocked
    # user loses access at next refresh) but more AssumeRole calls.
    vended_credential_ttl_seconds: int = field(
        default_factory=lambda: int(_env("VENDED_CREDENTIAL_TTL_SECONDS", "900")))

    # --- metrics ---
    metrics_namespace: str = field(default_factory=lambda: _env("METRICS_NAMESPACE", "BedrockQuotaGateway"))
    # Reconciler cadence, surfaced read-only by GET /admin/summary so the UI can
    # display it. It is set at deploy time on the EventBridge rule; this env var
    # is informational only (changing it here does NOT change the schedule).
    reconciler_interval_minutes: int = field(
        default_factory=lambda: int(_env("RECONCILER_INTERVAL_MINUTES", "5")))

    # --- quota defaults applied to newly created users (admin API) ---
    default_daily_usd: float = field(default_factory=lambda: float(_env("DEFAULT_DAILY_USD", "1.0")))
    default_daily_input_tokens: int = field(default_factory=lambda: int(_env("DEFAULT_DAILY_INPUT_TOKENS", "1000000")))
    default_daily_output_tokens: int = field(default_factory=lambda: int(_env("DEFAULT_DAILY_OUTPUT_TOKENS", "200000")))
    # DynamoDB TTL for daily usage rows. Deletion is asynchronous after this
    # timestamp; it is not the quota-window reset mechanism.
    usage_retention_days: int = field(
        default_factory=lambda: int(_env("USAGE_RETENTION_DAYS", "35")))

    # Mode B application allowlist. Empty preserves compatibility by allowing
    # every model ID; Mode A uses a separate IAM resource-ARN allowlist.
    mode_b_allowed_model_ids: frozenset[str] = field(
        default_factory=lambda: _env_string_set("MODE_B_ALLOWED_MODEL_IDS_JSON"))

    # Output-token reservation used when the request does not specify
    # max_tokens / max_output_tokens. Deliberately conservative.
    fallback_max_output_tokens: int = field(default_factory=lambda: int(_env("FALLBACK_MAX_OUTPUT_TOKENS", "4096")))

    # chars-per-token heuristic used for pre-flight input estimation.
    chars_per_token: float = field(default_factory=lambda: float(_env("CHARS_PER_TOKEN", "4.0")))

    request_timeout_seconds: float = field(default_factory=lambda: float(_env("REQUEST_TIMEOUT_SECONDS", "300")))

    @property
    def base_url(self) -> str:
        """Root of the bedrock-mantle endpoint (no /v1 suffix).

        OpenAI APIs live under /v1/..., the Anthropic Messages API lives
        under /anthropic/v1/... .
        """
        if self.mantle_base_url:
            return self.mantle_base_url.rstrip("/")
        return f"https://bedrock-mantle.{self.aws_region}.api.aws"


settings = Settings()
