"""Validated deployment configuration for the Bedrock Spend Controls stack."""

from __future__ import annotations

import json
import re
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from constructs import Node


CDK_DIR = Path(__file__).resolve().parents[1]

_DEPLOYMENT_KEYS = {
    "adapter_layer_arn",
    "admin_jwt_claim",
    "admin_jwt_value",
    "admin_ui",
    "allowed_model_arns",
    "alert_email",
    "auto_provision_users",
    "default_daily_input_tokens",
    "default_daily_output_tokens",
    "default_daily_usd",
    # Accepted only as migration inputs from the former dual-mode design.
    "default_mantle_project_id",
    "experimental_native_session_deny",
    "invocation_log_group_name",
    "invoker_principal_arns",
    "jwt_audience",
    "jwt_issuer",
    "jwt_jwks_url",
    "jwt_user_claim",
    "manage_invocation_logging",
    "mode_a_allowed_model_arns",
    "mode_b_allowed_model_ids",
    "model_config",
    "permission_lease_seconds",
    "reconciler_interval_minutes",
    "refresh_jitter_seconds",
    "refresh_overlap_seconds",
    "retain_tables_on_delete",
    "revocation_policy_shards",
    "revocation_reconcile_minutes",
    "snapstart",
    "usage_retention_days",
    "vend_rate_limit_per_minute",
    "vended_ttl_seconds",
    "warn_threshold",
    "workloads",
}

_DEFAULTS = {
    "adapter_layer_arn": "",
    "admin_jwt_claim": "",
    "admin_jwt_value": "",
    "admin_ui": False,
    "allowed_model_arns": ["*"],
    "alert_email": "",
    "auto_provision_users": True,
    "default_daily_input_tokens": 1_000_000,
    "default_daily_output_tokens": 200_000,
    "default_daily_usd": 1.0,
    "invocation_log_group_name": "",
    "invoker_principal_arns": [],
    "jwt_audience": "",
    "jwt_issuer": "",
    "jwt_jwks_url": "",
    "jwt_user_claim": "sub",
    "model_config": "config/model-pricing.json",
    "permission_lease_seconds": 300,
    "refresh_jitter_seconds": 5,
    "refresh_overlap_seconds": 10,
    "retain_tables_on_delete": False,
    "revocation_policy_shards": 19,
    "revocation_reconcile_minutes": 5,
    "snapstart": False,
    "usage_retention_days": 35,
    "vend_rate_limit_per_minute": 6,
    "vended_ttl_seconds": 900,
    "warn_threshold": 0.8,
    "workloads": "",
}


@dataclass(frozen=True)
class ModelPricingConfig:
    catalog_models: dict[str, list[str]]
    price_overrides: dict[str, dict[str, float]]
    fallback_price: dict[str, float]


@dataclass(frozen=True)
class WorkloadConfig:
    """One directly-invoking application enrolled in workload mode.

    ``name`` becomes the quota subject ``workload:<name>``; ``model`` is the
    foundation-model ID or cross-region inference-profile ID the dedicated
    application inference profile copies from; ``role_arn`` (optional) is the
    workload's IAM role for direct policy attachment and Deny enforcement.
    Without ``role_arn`` the stack emits a policy snippet instead and the
    workload is metered/alerted but not hard-enforced (enforcement_ready is
    false until a role is provided).
    """

    name: str
    model: str
    role_arn: str = ""

    @property
    def workload_id(self) -> str:
        return f"workload:{self.name}"


@dataclass(frozen=True)
class DeploymentConfig:
    adapter_layer_arn: str
    admin_jwt_claim: str
    admin_jwt_value: str
    admin_ui: bool
    allowed_model_arns: tuple[str, ...]
    alert_email: str
    auto_provision_users: bool
    default_daily_input_tokens: int
    default_daily_output_tokens: int
    default_daily_usd: float
    deprecated_options: tuple[str, ...]
    invocation_log_group_name: str
    invoker_principal_arns: tuple[str, ...]
    jwt_audience: str
    jwt_issuer: str
    jwt_jwks_url: str
    jwt_user_claim: str
    manage_invocation_logging: bool
    model_pricing: ModelPricingConfig
    permission_lease_seconds: int
    refresh_jitter_seconds: int
    refresh_overlap_seconds: int
    retain_tables_on_delete: bool
    revocation_policy_shards: int
    revocation_reconcile_minutes: int
    snapstart: bool
    usage_retention_days: int
    vend_rate_limit_per_minute: int
    vended_ttl_seconds: int
    warn_threshold: float
    workloads: tuple[WorkloadConfig, ...]

    @classmethod
    def from_node(cls, node: Node) -> "DeploymentConfig":
        raw_deployment = node.try_get_context("deployment_config")
        deployment, deployment_dir = _load_deployment(raw_deployment)

        def value(name: str) -> Any:
            context_value = node.try_get_context(name)
            if context_value is not None:
                return context_value
            return deployment.get(name, _DEFAULTS.get(name))

        manage_raw = value("manage_invocation_logging")
        manage_logging = (
            None
            if manage_raw is None
            else _boolean("manage_invocation_logging", manage_raw)
        )
        existing_log_group = _string(
            "invocation_log_group_name", value("invocation_log_group_name")
        )
        if existing_log_group and manage_logging is True:
            raise ValueError(
                "manage_invocation_logging=true is incompatible with "
                "invocation_log_group_name. Choose managed logging or an "
                "existing log group, not both."
            )
        if existing_log_group:
            manage_logging = False
        elif manage_logging is None:
            raise ValueError(
                "Bedrock model-invocation logging is an account + region-wide "
                "setting, so this stack will not change it without explicit "
                "consent. Use manage_invocation_logging=true to let this "
                "sample manage it, or use manage_invocation_logging=false "
                "with invocation_log_group_name."
            )
        elif not manage_logging:
            raise ValueError(
                "manage_invocation_logging=false requires "
                "invocation_log_group_name for an existing log group that "
                "already receives Bedrock model-invocation logs."
            )

        model_source = value("model_config")
        model_base = (
            CDK_DIR
            if node.try_get_context("model_config") is not None
            else deployment_dir
        )
        model_pricing = _model_pricing(model_source, model_base)

        workloads_base = (
            CDK_DIR
            if node.try_get_context("workloads") is not None
            else deployment_dir
        )
        workloads = _workloads(value("workloads"), workloads_base)

        new_allowlist_explicit = (
            node.try_get_context("allowed_model_arns") is not None
            or "allowed_model_arns" in deployment
        )
        legacy_allowlist_explicit = (
            node.try_get_context("mode_a_allowed_model_arns") is not None
            or "mode_a_allowed_model_arns" in deployment
        )
        if new_allowlist_explicit and legacy_allowlist_explicit:
            raise ValueError(
                "Use allowed_model_arns only; it replaces the legacy "
                "mode_a_allowed_model_arns key."
            )
        allowed_model_arns = _string_list(
            "allowed_model_arns",
            (
                value("allowed_model_arns")
                if new_allowlist_explicit or not legacy_allowlist_explicit
                else value("mode_a_allowed_model_arns")
            ),
        )
        if not allowed_model_arns:
            raise ValueError("allowed_model_arns must not be empty")
        for model_arn in allowed_model_arns:
            if model_arn != "*" and (
                not model_arn.startswith("arn:") or ":bedrock:" not in model_arn
            ):
                raise ValueError(
                    "allowed_model_arns accepts Bedrock IAM resource "
                    f"ARNs (or '*'), not model IDs: {model_arn}"
                )

        deprecated_options = tuple(
            name
            for name in (
                "default_mantle_project_id",
                "experimental_native_session_deny",
                "mode_b_allowed_model_ids",
                "reconciler_interval_minutes",
            )
            if node.try_get_context(name) is not None or name in deployment
        )
        if legacy_allowlist_explicit:
            deprecated_options += ("mode_a_allowed_model_arns",)

        invoker_arns = _string_list(
            "invoker_principal_arns", value("invoker_principal_arns")
        )
        for principal_arn in invoker_arns:
            if not principal_arn.startswith("arn:"):
                raise ValueError(
                    "invoker_principal_arns entries must be IAM principal ARNs; "
                    f"got {principal_arn}"
                )

        vended_ttl_seconds = _positive_int(
            "vended_ttl_seconds", value("vended_ttl_seconds")
        )
        if not 900 <= vended_ttl_seconds <= 3_600:
            raise ValueError(
                "vended_ttl_seconds must be between 900 (15 min) and 3600 "
                "seconds because this Lambda broker uses role chaining; the "
                f"role-chaining maximum is 3600; got {vended_ttl_seconds}"
            )

        permission_lease_seconds = _positive_int(
            "permission_lease_seconds", value("permission_lease_seconds")
        )
        if permission_lease_seconds not in {60, 300, 900}:
            raise ValueError(
                "permission_lease_seconds must be one of 60, 300, 900; "
                f"got {permission_lease_seconds}"
            )
        if permission_lease_seconds > vended_ttl_seconds:
            raise ValueError(
                "permission_lease_seconds must not exceed "
                "vended_ttl_seconds"
            )

        refresh_overlap_seconds = _positive_int(
            "refresh_overlap_seconds", value("refresh_overlap_seconds")
        )
        if refresh_overlap_seconds >= permission_lease_seconds:
            raise ValueError(
                "refresh_overlap_seconds must be less than "
                "permission_lease_seconds"
            )
        refresh_jitter_seconds = _non_negative_int(
            "refresh_jitter_seconds", value("refresh_jitter_seconds")
        )
        if refresh_jitter_seconds >= refresh_overlap_seconds:
            raise ValueError(
                "refresh_jitter_seconds must be less than "
                "refresh_overlap_seconds"
            )
        vend_rate_limit_per_minute = _positive_int(
            "vend_rate_limit_per_minute",
            value("vend_rate_limit_per_minute"),
        )
        revocation_policy_shards = _positive_int(
            "revocation_policy_shards", value("revocation_policy_shards")
        )
        if revocation_policy_shards != 19:
            raise ValueError(
                "revocation_policy_shards is an immutable 19-shard layout: "
                "the emergency policy uses the twentieth role attachment, "
                "and changing the count would rehash active denies; "
                f"got {revocation_policy_shards}"
            )
        revocation_reconcile_minutes = _positive_int(
            "revocation_reconcile_minutes",
            value("revocation_reconcile_minutes"),
        )

        warn_threshold = _positive_float(
            "warn_threshold", value("warn_threshold")
        )
        if warn_threshold >= 1:
            raise ValueError(
                f"warn_threshold must be greater than 0 and less than 1; "
                f"got {warn_threshold}"
            )

        jwt_user_claim = _string("jwt_user_claim", value("jwt_user_claim"))
        if not jwt_user_claim:
            raise ValueError("jwt_user_claim must not be empty")
        jwt_issuer = _string("jwt_issuer", value("jwt_issuer"))
        admin_ui = _boolean("admin_ui", value("admin_ui"))
        admin_jwt_claim = _string(
            "admin_jwt_claim", value("admin_jwt_claim")
        )
        admin_jwt_value = _string(
            "admin_jwt_value", value("admin_jwt_value")
        )
        if bool(admin_jwt_claim) != bool(admin_jwt_value):
            raise ValueError(
                "admin_jwt_claim and admin_jwt_value must be configured "
                "together"
            )
        if admin_ui and jwt_issuer:
            raise ValueError(
                "admin_ui=true currently supports only the stack-created "
                "demo Cognito pool; host and integrate the UI separately "
                "when jwt_issuer is configured"
            )
        if admin_ui and not admin_jwt_claim:
            raise ValueError(
                "admin_ui=true requires admin_jwt_claim and "
                "admin_jwt_value because the browser never receives the "
                "shared admin secret"
            )

        return cls(
            adapter_layer_arn=_string(
                "adapter_layer_arn", value("adapter_layer_arn")
            ),
            admin_jwt_claim=admin_jwt_claim,
            admin_jwt_value=admin_jwt_value,
            admin_ui=admin_ui,
            allowed_model_arns=tuple(allowed_model_arns),
            alert_email=_string("alert_email", value("alert_email")),
            auto_provision_users=_boolean(
                "auto_provision_users", value("auto_provision_users")
            ),
            default_daily_input_tokens=_positive_int(
                "default_daily_input_tokens",
                value("default_daily_input_tokens"),
            ),
            default_daily_output_tokens=_positive_int(
                "default_daily_output_tokens",
                value("default_daily_output_tokens"),
            ),
            default_daily_usd=_positive_float(
                "default_daily_usd", value("default_daily_usd")
            ),
            deprecated_options=deprecated_options,
            invocation_log_group_name=existing_log_group,
            invoker_principal_arns=tuple(invoker_arns),
            jwt_audience=_string("jwt_audience", value("jwt_audience")),
            jwt_issuer=jwt_issuer,
            jwt_jwks_url=_string("jwt_jwks_url", value("jwt_jwks_url")),
            jwt_user_claim=jwt_user_claim,
            manage_invocation_logging=manage_logging,
            model_pricing=model_pricing,
            permission_lease_seconds=permission_lease_seconds,
            refresh_jitter_seconds=refresh_jitter_seconds,
            refresh_overlap_seconds=refresh_overlap_seconds,
            retain_tables_on_delete=_boolean(
                "retain_tables_on_delete", value("retain_tables_on_delete")
            ),
            revocation_policy_shards=revocation_policy_shards,
            revocation_reconcile_minutes=revocation_reconcile_minutes,
            snapstart=_boolean("snapstart", value("snapstart")),
            usage_retention_days=_positive_int(
                "usage_retention_days", value("usage_retention_days")
            ),
            vend_rate_limit_per_minute=vend_rate_limit_per_minute,
            vended_ttl_seconds=vended_ttl_seconds,
            warn_threshold=warn_threshold,
            workloads=workloads,
        )


def _load_deployment(raw: Any) -> tuple[dict[str, Any], Path]:
    if raw is None:
        return {}, CDK_DIR
    deployment, source_path = _mapping("deployment_config", raw, CDK_DIR)
    unknown = sorted(set(deployment) - _DEPLOYMENT_KEYS)
    if unknown:
        raise ValueError(
            "Unknown deployment_config keys: " + ", ".join(unknown)
        )
    return deployment, source_path.parent if source_path else CDK_DIR


def _model_pricing(raw: Any, base_dir: Path) -> ModelPricingConfig:
    data, _ = _mapping("model_config", raw, base_dir)
    expected = {"catalog_models", "price_overrides", "fallback_price"}
    unknown = sorted(set(data) - expected)
    missing = sorted(expected - set(data))
    if unknown or missing:
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unknown:
            details.append("unknown " + ", ".join(unknown))
        raise ValueError("Invalid model_config: " + "; ".join(details))

    catalog_raw = data["catalog_models"]
    if not isinstance(catalog_raw, dict):
        raise ValueError("model_config.catalog_models must be an object")
    catalog_models: dict[str, list[str]] = {}
    model_ids: set[str] = set()
    for catalog_model, raw_ids in catalog_raw.items():
        name = _string("catalog model name", catalog_model)
        ids = _string_list(f"catalog_models.{name}", raw_ids)
        if not name or not ids:
            raise ValueError(
                f"catalog_models.{name or '<empty>'} must contain model IDs"
            )
        duplicates = model_ids.intersection(ids)
        if duplicates:
            raise ValueError(
                "Duplicate model IDs in model_config: "
                + ", ".join(sorted(duplicates))
            )
        model_ids.update(ids)
        catalog_models[name] = ids

    overrides_raw = data["price_overrides"]
    if not isinstance(overrides_raw, dict):
        raise ValueError("model_config.price_overrides must be an object")
    price_overrides: dict[str, dict[str, float]] = {}
    for model_id, price in overrides_raw.items():
        model_id = _string("price override model ID", model_id)
        if model_id in model_ids:
            raise ValueError(f"Duplicate model price mapping for {model_id}")
        model_ids.add(model_id)
        if not isinstance(price, dict):
            raise ValueError(
                f"price_overrides.{model_id} must be an object"
            )
        # A hand-pinned price bypasses the Pricing API, so it must document
        # why the catalog cannot price this model (for example the catalog
        # has no entry for an inference-profile ID). The reason is deploy
        # metadata only; the resolved snapshot carries just the two rates.
        price = dict(price)
        reason = price.pop("reason", None)
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError(
                f"price_overrides.{model_id} must include a non-empty "
                "'reason' explaining why the Pricing API cannot price it"
            )
        price_overrides[model_id] = _price(
            f"price_overrides.{model_id}", price
        )

    if not model_ids:
        raise ValueError(
            "model_config must define at least one catalog model or price override"
        )
    fallback = _price("fallback_price", data["fallback_price"])
    return ModelPricingConfig(catalog_models, price_overrides, fallback)


_WORKLOAD_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
_WORKLOAD_ROLE_ARN = re.compile(r"^arn:aws[a-z-]*:iam::\d{12}:role/.+")
_WORKLOAD_KEYS = {"name", "model", "role_arn"}


def _workloads(raw: Any, base_dir: Path) -> tuple[WorkloadConfig, ...]:
    """Parse the optional workload-mode roster.

    Accepts '' (workload mode off), an inline JSON object, or a JSON file
    path. The document shape is {"workloads": [{"name", "model",
    "role_arn"?}, ...]}.
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return ()
    data, _ = _mapping("workloads", raw, base_dir)
    unknown = sorted(set(data) - {"workloads"})
    if unknown:
        raise ValueError("Unknown workloads keys: " + ", ".join(unknown))
    entries = data.get("workloads")
    if not isinstance(entries, list) or not entries:
        raise ValueError(
            "workloads must contain a non-empty 'workloads' JSON array"
        )
    parsed: list[WorkloadConfig] = []
    seen_names: set[str] = set()
    for index, entry in enumerate(entries):
        label = f"workloads[{index}]"
        if not isinstance(entry, dict):
            raise ValueError(f"{label} must be a JSON object")
        unknown_entry = sorted(set(entry) - _WORKLOAD_KEYS)
        if unknown_entry:
            raise ValueError(
                f"Unknown {label} keys: " + ", ".join(unknown_entry)
            )
        name = entry.get("name")
        if not isinstance(name, str) or not _WORKLOAD_NAME.match(name):
            raise ValueError(
                f"{label}.name must match [a-z0-9][a-z0-9-]{{0,47}}; "
                f"got {name!r}"
            )
        if name in seen_names:
            raise ValueError(f"workloads names must be unique; {name!r} repeats")
        seen_names.add(name)
        model = entry.get("model")
        if not isinstance(model, str) or not model.strip():
            raise ValueError(f"{label}.model must be a non-empty string")
        if model.startswith("arn:"):
            raise ValueError(
                f"{label}.model must be a model or inference-profile ID, "
                f"not an ARN: {model}"
            )
        role_arn = entry.get("role_arn", "")
        if role_arn and (
            not isinstance(role_arn, str)
            or not _WORKLOAD_ROLE_ARN.match(role_arn)
        ):
            raise ValueError(
                f"{label}.role_arn must be an IAM role ARN "
                f"(arn:aws:iam::<account>:role/...); got {role_arn!r}"
            )
        parsed.append(
            WorkloadConfig(name=name, model=model.strip(), role_arn=role_arn)
        )
    return tuple(parsed)


def _mapping(
    name: str, raw: Any, base_dir: Path
) -> tuple[dict[str, Any], Path | None]:
    if isinstance(raw, dict):
        return raw, None
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"{name} must be a JSON object or a JSON file path")
    text = raw.strip()
    if text.startswith("{"):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{name} contains invalid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError(f"{name} JSON must be an object")
        return parsed, None

    path = Path(text).expanduser()
    candidates = [path] if path.is_absolute() else [base_dir / path, Path.cwd() / path]
    resolved = next((candidate for candidate in candidates if candidate.is_file()), None)
    if resolved is None:
        raise ValueError(f"{name} file not found: {text}")
    try:
        parsed = json.loads(resolved.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} file contains invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{name} file must contain a JSON object")
    return parsed, resolved


def _boolean(name: str, raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str) and raw.strip().lower() in {"true", "false"}:
        return raw.strip().lower() == "true"
    raise ValueError(f"{name} must be true or false; got {raw!r}")


def _positive_float(name: str, raw: Any) -> float:
    if isinstance(raw, bool):
        raise ValueError(f"{name} must be a positive number; got {raw!r}")
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{name} must be a positive number; got {raw!r}"
        ) from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive number; got {raw!r}")
    return value


def _positive_int(name: str, raw: Any) -> int:
    if isinstance(raw, bool):
        raise ValueError(f"{name} must be a positive integer; got {raw!r}")
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{name} must be a positive integer; got {raw!r}"
        ) from exc
    if isinstance(raw, float) and not raw.is_integer():
        raise ValueError(f"{name} must be a positive integer; got {raw!r}")
    if isinstance(raw, str) and str(value) != raw.strip():
        raise ValueError(f"{name} must be a positive integer; got {raw!r}")
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer; got {raw!r}")
    return value


def _non_negative_int(name: str, raw: Any) -> int:
    if isinstance(raw, bool):
        raise ValueError(
            f"{name} must be a non-negative integer; got {raw!r}"
        )
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{name} must be a non-negative integer; got {raw!r}"
        ) from exc
    if isinstance(raw, float) and not raw.is_integer():
        raise ValueError(
            f"{name} must be a non-negative integer; got {raw!r}"
        )
    if isinstance(raw, str) and str(value) != raw.strip():
        raise ValueError(
            f"{name} must be a non-negative integer; got {raw!r}"
        )
    if value < 0:
        raise ValueError(
            f"{name} must be a non-negative integer; got {raw!r}"
        )
    return value


def _string(name: str, raw: Any) -> str:
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise ValueError(f"{name} must be a string; got {raw!r}")
    return raw.strip()


def _string_list(name: str, raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                raw = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{name} contains invalid JSON: {exc}") from exc
        else:
            raw = text.split(",")
    if not isinstance(raw, list):
        raise ValueError(f"{name} must be a list or comma-separated string")
    values = [_string(f"{name} entry", item) for item in raw]
    if any(not item for item in values):
        raise ValueError(f"{name} entries must not be empty")
    if len(values) != len(set(values)):
        raise ValueError(f"{name} must not contain duplicates")
    return values


def _price(name: str, raw: Any) -> dict[str, float]:
    if not isinstance(raw, dict):
        raise ValueError(f"{name} must be an object")
    expected = {"input_per_mtok", "output_per_mtok"}
    if set(raw) != expected:
        raise ValueError(
            f"{name} must contain exactly input_per_mtok and output_per_mtok"
        )
    return {
        field: _positive_float(f"{name}.{field}", raw[field])
        for field in sorted(expected)
    }
