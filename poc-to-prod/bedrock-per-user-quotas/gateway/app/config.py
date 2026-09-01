"""Central configuration for the quota gateway.

Everything is driven by environment variables so the same code runs in
Lambda (set by CDK) and locally (uvicorn + a .env file).
"""

import os
from dataclasses import dataclass, field


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    aws_region: str = field(
        default_factory=lambda: _env("AWS_REGION", "us-east-1")
    )

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
    # ``legacy`` preserves current behavior. ``lease`` adds an immutable,
    # time-bounded STS session policy. ``revocation`` is reserved for the
    # separately qualified SourceIdentity deny path.
    credential_enforcement_mode: str = field(
        default_factory=lambda: _env("CREDENTIAL_ENFORCEMENT_MODE", "legacy"))
    permission_lease_seconds: int = field(
        default_factory=lambda: int(_env("PERMISSION_LEASE_SECONDS", "300")))
    refresh_overlap_seconds: int = field(
        default_factory=lambda: int(_env("REFRESH_OVERLAP_SECONDS", "10")))
    refresh_jitter_seconds: int = field(
        default_factory=lambda: int(_env("REFRESH_JITTER_SECONDS", "5")))
    vend_rate_limit_per_minute: int = field(
        default_factory=lambda: int(_env("VEND_RATE_LIMIT_PER_MINUTE", "6")))
    revocation_policy_shards: int = field(
        default_factory=lambda: int(_env("REVOCATION_POLICY_SHARDS", "19")))
    revocation_reconcile_minutes: int = field(
        default_factory=lambda: int(_env("REVOCATION_RECONCILE_MINUTES", "5")))
    revocation_policy_max_characters: int = field(
        default_factory=lambda: int(_env("REVOCATION_POLICY_MAX_CHARACTERS", "6144")))
    operations_alarm_names_json: str = field(
        default_factory=lambda: _env("OPERATIONS_ALARM_NAMES_JSON", "{}"))
    qualification_status_json: str = field(
        default_factory=lambda: _env(
            "QUALIFICATION_STATUS_JSON",
            '{"legacy":"baseline_existing_behavior","lease":"pending_live_sandbox_probe",'
            '"revocation":"experimental_pending_propagation_isolation_probe",'
            '"emergency":"pending_live_activation_recovery_exercise"}',
        )
    )

    # --- metrics ---
    metrics_namespace: str = field(default_factory=lambda: _env("METRICS_NAMESPACE", "BedrockQuotaGateway"))

    # --- quota defaults applied to newly created users (admin API) ---
    default_daily_usd: float = field(default_factory=lambda: float(_env("DEFAULT_DAILY_USD", "1.0")))
    default_daily_input_tokens: int = field(default_factory=lambda: int(_env("DEFAULT_DAILY_INPUT_TOKENS", "1000000")))
    default_daily_output_tokens: int = field(default_factory=lambda: int(_env("DEFAULT_DAILY_OUTPUT_TOKENS", "200000")))
    # DynamoDB TTL for daily usage rows. Deletion is asynchronous after this
    # timestamp; it is not the quota-window reset mechanism.
    usage_retention_days: int = field(
        default_factory=lambda: int(_env("USAGE_RETENTION_DAYS", "35")))


settings = Settings()
