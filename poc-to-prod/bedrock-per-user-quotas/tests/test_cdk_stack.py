import json
import os

import aws_cdk as cdk
import pytest
from aws_cdk import aws_lambda as lambda_
from aws_cdk.assertions import Match, Template

import cdk.stacks.quota_gateway_stack as stack_module
from cdk.stacks.quota_gateway_stack import QuotaGatewayStack


@pytest.fixture(autouse=True)
def no_asset_bundling(monkeypatch):
    inline = lambda_.Code.from_inline("def handler(event, context): return {}")
    monkeypatch.setattr(
        stack_module.lambda_.Code,
        "from_asset",
        lambda *args, **kwargs: inline,
    )
    monkeypatch.setattr(
        stack_module.s3deploy.Source,
        "asset",
        lambda *args, **kwargs: stack_module.s3deploy.Source.data(
            "index.html", "<!doctype html>"
        ),
    )
    dist_dir = os.path.join(
        os.path.dirname(stack_module.__file__),
        "..",
        "..",
        "admin-ui",
        "dist",
    )
    os.makedirs(dist_dir, exist_ok=True)


def _template(context: dict) -> Template:
    app = cdk.App(context=context)
    stack = QuotaGatewayStack(
        app,
        "TestStack",
        env=cdk.Environment(
            account="111122223333", region="us-east-1"
        ),
    )
    return Template.from_stack(stack)


def _environment_with(template: Template, key: str) -> dict:
    for resource in template.find_resources("AWS::Lambda::Function").values():
        variables = (
            resource.get("Properties", {})
            .get("Environment", {})
            .get("Variables", {})
        )
        if key in variables:
            return variables
    raise AssertionError(f"No Lambda environment contains {key}")


def test_invocation_logging_requires_explicit_ownership_choice():
    with pytest.raises(ValueError, match="explicit consent"):
        _template({})
    with pytest.raises(ValueError, match="incompatible"):
        _template(
            {
                "manage_invocation_logging": True,
                "invocation_log_group_name": "/existing/group",
            }
        )
    with pytest.raises(ValueError, match="requires invocation_log_group_name"):
        _template({"manage_invocation_logging": False})


def test_runtime_only_stack_is_event_driven_and_has_no_mantle_permissions():
    template = _template({"manage_invocation_logging": True})

    template.resource_count_is("AWS::Events::Rule", 1)
    template.resource_count_is("AWS::Logs::SubscriptionFilter", 1)
    template.resource_count_is("AWS::Lambda::EventSourceMapping", 1)
    template.resource_count_is("AWS::SQS::Queue", 1)
    template.has_resource_properties(
        "AWS::Lambda::Url",
        {"AuthType": "AWS_IAM", "InvokeMode": "BUFFERED"},
    )
    rendered = json.dumps(template.to_json())
    assert "assumed-role/" in rendered
    assert "BedrockUserRole" in rendered
    assert "bedrock-mantle" not in rendered
    assert "RECONCILER_INTERVAL_MINUTES" not in rendered
    assert "MODE_B_ALLOWED_MODEL_IDS_JSON" not in rendered
    assert any(
        logical_id.startswith("GatewayFn")
        for logical_id in template.find_resources(
            "AWS::Lambda::Function"
        )
    )
    assert "GatewayRoleArn" in template.to_json()["Outputs"]


def test_price_snapshot_is_injected_only_into_usage_processor():
    template = _template({"manage_invocation_logging": True})
    template.has_resource_properties(
        "Custom::BedrockModelPriceSnapshot",
        {
            "RegionCode": "us-east-1",
            "CatalogModels": Match.object_like(
                {"gpt-oss-120b": Match.any_value()}
            ),
        },
    )
    processor_env = _environment_with(template, "WARN_THRESHOLD")
    broker_env = _environment_with(template, "BEDROCK_USER_ROLE_ARN")

    assert processor_env["MODEL_PRICES_JSON"]["Fn::GetAtt"][1] == (
        "ModelPricesJson"
    )
    assert processor_env["MODEL_FALLBACK_PRICE_JSON"]["Fn::GetAtt"][1] == (
        "FallbackPriceJson"
    )
    assert "MODEL_PRICES_JSON" not in broker_env
    assert processor_env["BEDROCK_USER_ROLE_NAME"]


def test_defaults_are_injected_and_tables_are_destroyable_for_demo():
    template = _template({"manage_invocation_logging": True})
    broker_env = _environment_with(template, "BEDROCK_USER_ROLE_ARN")
    processor_env = _environment_with(template, "WARN_THRESHOLD")

    assert broker_env["AUTO_PROVISION_USERS"] == "true"
    assert broker_env["DEFAULT_DAILY_USD"] == "1.0"
    assert broker_env["DEFAULT_DAILY_INPUT_TOKENS"] == "1000000"
    assert broker_env["DEFAULT_DAILY_OUTPUT_TOKENS"] == "200000"
    assert broker_env["USAGE_RETENTION_DAYS"] == "35"
    assert broker_env["VENDED_CREDENTIAL_TTL_SECONDS"] == "900"
    assert broker_env["CREDENTIAL_ENFORCEMENT_MODE"] == "legacy"
    assert broker_env["PERMISSION_LEASE_SECONDS"] == "300"
    assert broker_env["REFRESH_OVERLAP_SECONDS"] == "10"
    assert broker_env["REFRESH_JITTER_SECONDS"] == "5"
    assert broker_env["VEND_RATE_LIMIT_PER_MINUTE"] == "6"
    assert broker_env["REVOCATION_POLICY_SHARDS"] == "19"
    assert broker_env["REVOCATION_RECONCILE_MINUTES"] == "5"
    assert broker_env["REVOCATION_POLICY_MAX_CHARACTERS"] == "6144"
    assert "baseline_existing_behavior" in json.dumps(
        broker_env["QUALIFICATION_STATUS_JSON"]
    )
    assert "OPERATIONS_ALARM_NAMES_JSON" in broker_env
    assert processor_env["WARN_THRESHOLD"] == "0.8"
    assert processor_env["USAGE_RETENTION_DAYS"] == "35"
    dashboard = json.dumps(
        next(iter(template.find_resources("AWS::CloudWatch::Dashboard").values()))
    )
    assert "DetectionLagMilliseconds" in dashboard
    assert "LeaseStarted" in dashboard
    assert "LeaseRefreshed" in dashboard
    assert "LeaseRetried" in dashboard

    template.resource_properties_count_is(
        "AWS::DynamoDB::Table",
        {
            "TimeToLiveSpecification": {
                "AttributeName": "expires_at",
                "Enabled": True,
            }
        },
        2,
    )
    for table in template.find_resources("AWS::DynamoDB::Table").values():
        assert table["DeletionPolicy"] == "Delete"


def test_production_values_and_table_retention():
    template = _template(
        {
            "manage_invocation_logging": True,
            "auto_provision_users": False,
            "default_daily_usd": 25,
            "default_daily_input_tokens": 10_000_000,
            "default_daily_output_tokens": 2_000_000,
            "warn_threshold": 0.75,
            "usage_retention_days": 90,
            "retain_tables_on_delete": True,
            "vended_ttl_seconds": 1800,
            "credential_enforcement_mode": "lease",
            "permission_lease_seconds": 300,
            "refresh_overlap_seconds": 20,
            "refresh_jitter_seconds": 7,
            "vend_rate_limit_per_minute": 8,
        }
    )
    broker_env = _environment_with(template, "BEDROCK_USER_ROLE_ARN")
    processor_env = _environment_with(template, "WARN_THRESHOLD")
    assert broker_env["AUTO_PROVISION_USERS"] == "false"
    assert broker_env["DEFAULT_DAILY_USD"] == "25.0"
    assert broker_env["VENDED_CREDENTIAL_TTL_SECONDS"] == "1800"
    assert broker_env["CREDENTIAL_ENFORCEMENT_MODE"] == "lease"
    assert broker_env["PERMISSION_LEASE_SECONDS"] == "300"
    assert broker_env["REFRESH_OVERLAP_SECONDS"] == "20"
    assert broker_env["REFRESH_JITTER_SECONDS"] == "7"
    assert broker_env["VEND_RATE_LIMIT_PER_MINUTE"] == "8"
    assert processor_env["WARN_THRESHOLD"] == "0.75"
    assert processor_env["USAGE_RETENTION_DAYS"] == "90"
    for table in template.find_resources("AWS::DynamoDB::Table").values():
        assert table["DeletionPolicy"] == "Retain"
        assert table["UpdateReplacePolicy"] == "Retain"


@pytest.mark.parametrize("lease_seconds", [60, 300, 900])
def test_supported_permission_lease_durations_synthesize(lease_seconds):
    template = _template(
        {
            "manage_invocation_logging": True,
            "credential_enforcement_mode": "lease",
            "vended_ttl_seconds": 900,
            "permission_lease_seconds": lease_seconds,
            "refresh_overlap_seconds": 10,
            "refresh_jitter_seconds": 5,
        }
    )
    broker_env = _environment_with(template, "BEDROCK_USER_ROLE_ARN")
    assert broker_env["PERMISSION_LEASE_SECONDS"] == str(lease_seconds)


def test_role_chained_revocation_mode_requires_exactly_one_hour():
    template = _template(
        {
            "manage_invocation_logging": True,
            "credential_enforcement_mode": "revocation",
            "vended_ttl_seconds": 3600,
            "revocation_policy_shards": 19,
            "revocation_reconcile_minutes": 3,
        }
    )
    assert _environment_with(template, "BEDROCK_USER_ROLE_ARN")[
        "CREDENTIAL_ENFORCEMENT_MODE"
    ] == "revocation"
    template.has_resource_properties(
        "AWS::DynamoDB::Table",
        {"StreamSpecification": {"StreamViewType": "NEW_AND_OLD_IMAGES"}},
    )
    template.resource_count_is("AWS::Lambda::EventSourceMapping", 2)
    template.resource_count_is("AWS::Events::Rule", 2)
    template.resource_count_is("AWS::SQS::Queue", 2)

    template.has_resource_properties(
        "AWS::Lambda::Function",
        Match.object_like(
            {
                "ReservedConcurrentExecutions": 1,
                "Environment": {
                    "Variables": Match.object_like(
                        {
                            "REVOCATION_POLICY_ARNS_JSON": Match.any_value(),
                            "REVOCATION_POLICY_MAX_CHARACTERS": "6144",
                        }
                    )
                },
            }
        ),
    )
    rendered = json.dumps(template.to_json())
    assert rendered.count("__no_blocked_quota_identity__") == 19
    assert "iam:CreatePolicyVersion" in rendered
    assert "iam:DeletePolicyVersion" in rendered
    assert "RevocationSyncFailure" in rendered
    assert "RevocationPolicyOverflow" in rendered
    assert "revocation_failure" in rendered
    assert "revocation_overflow" in rendered
    assert "revocation_dlq" in rendered
    assert "revocation_iterator_age" in rendered
    role = next(
        value
        for logical_id, value in template.find_resources("AWS::IAM::Role").items()
        if logical_id.startswith("BedrockUserRole")
    )
    assert "PermissionsBoundary" in role["Properties"]

    with pytest.raises(ValueError, match="requires vended_ttl_seconds=3600"):
        _template(
            {
                "manage_invocation_logging": True,
                "credential_enforcement_mode": "revocation",
                "vended_ttl_seconds": 900,
            }
        )
    with pytest.raises(ValueError, match="immutable 19-shard layout"):
        _template(
            {
                "manage_invocation_logging": True,
                "credential_enforcement_mode": "revocation",
                "vended_ttl_seconds": 3600,
                "revocation_policy_shards": 18,
            }
        )


def test_eight_hour_role_chained_session_is_rejected():
    with pytest.raises(ValueError, match="role-chaining maximum is 3600"):
        _template(
            {
                "manage_invocation_logging": True,
                "vended_ttl_seconds": 28_800,
            }
        )


def test_refresh_timing_must_fit_inside_permission_lease():
    with pytest.raises(ValueError, match="less than permission_lease_seconds"):
        _template(
            {
                "manage_invocation_logging": True,
                "permission_lease_seconds": 60,
                "refresh_overlap_seconds": 60,
            }
        )
    with pytest.raises(ValueError, match="less than refresh_overlap_seconds"):
        _template(
            {
                "manage_invocation_logging": True,
                "refresh_overlap_seconds": 10,
                "refresh_jitter_seconds": 10,
            }
        )


def test_vended_role_uses_runtime_iam_allowlist_without_bearer_permission():
    arns = [
        "arn:aws:bedrock:us-east-1::foundation-model/openai.gpt-oss-120b-1:0",
        "arn:aws:bedrock:us-east-1:111122223333:inference-profile/example",
    ]
    template = _template(
        {
            "manage_invocation_logging": True,
            "allowed_model_arns": arns,
        }
    )
    template.has_resource_properties(
        "AWS::IAM::Policy",
        {
            "PolicyDocument": {
                "Statement": Match.array_with(
                    [
                        Match.object_like(
                            {
                                "Action": [
                                    "bedrock:CountTokens",
                                    "bedrock:InvokeModel",
                                    "bedrock:InvokeModelWithResponseStream",
                                ],
                                "Effect": "Allow",
                                "Resource": arns,
                            }
                        )
                    ]
                )
            }
        },
    )
    assert "bedrock:CallWithBearerToken" not in json.dumps(
        [
            policy
            for policy in template.find_resources(
                "AWS::IAM::Policy"
            ).values()
            if "Allow" in json.dumps(policy)
        ]
    )


def test_legacy_runtime_allowlist_alias_still_synthesizes():
    arn = (
        "arn:aws:bedrock:us-east-1::foundation-model/"
        "openai.gpt-oss-20b-1:0"
    )
    template = _template(
        {
            "manage_invocation_logging": True,
            "mode_a_allowed_model_arns": [arn],
            "mode_b_allowed_model_ids": ["ignored-after-migration"],
            "reconciler_interval_minutes": 1,
        }
    )
    assert arn in json.dumps(template.to_json())


def test_new_and_legacy_allowlist_cannot_be_combined():
    with pytest.raises(ValueError, match="allowed_model_arns only"):
        _template(
            {
                "manage_invocation_logging": True,
                "allowed_model_arns": ["*"],
                "mode_a_allowed_model_arns": ["*"],
            }
        )


def test_existing_log_group_does_not_change_account_wide_configuration():
    template = _template(
        {
            "manage_invocation_logging": False,
            "invocation_log_group_name": "/existing/bedrock/invocations",
        }
    )
    template.resource_count_is("Custom::AWS", 0)
    template.resource_count_is("AWS::Logs::SubscriptionFilter", 1)


def test_managed_logging_configuration_is_retained():
    template = _template({"manage_invocation_logging": True})
    template.has_resource(
        "Custom::AWS",
        {
            "DeletionPolicy": "Retain",
            "UpdateReplacePolicy": "Retain",
            "Properties": Match.object_like({}),
        },
    )
    template.has_resource(
        "AWS::IAM::Role",
        {
            "DeletionPolicy": "Retain",
            "UpdateReplacePolicy": "Retain",
            "Properties": Match.object_like(
                {
                    "AssumeRolePolicyDocument": Match.object_like(
                        {
                            "Statement": Match.array_with(
                                [
                                    Match.object_like(
                                        {
                                            "Principal": {
                                                "Service": "bedrock.amazonaws.com"
                                            }
                                        }
                                    )
                                ]
                            )
                        }
                    )
                }
            ),
        },
    )


def test_invoker_principals_and_deny_policy_are_preserved():
    principal = "arn:aws:iam::111122223333:role/BrokerInvoker"
    template = _template(
        {
            "manage_invocation_logging": True,
            "invoker_principal_arns": [principal],
        }
    )
    template.has_resource_properties(
        "AWS::Lambda::Permission",
        {
            "Action": "lambda:InvokeFunctionUrl",
            "FunctionUrlAuthType": "AWS_IAM",
            "Principal": principal,
        },
    )
    rendered = json.dumps(template.to_json())
    assert "deny-direct-bedrock-invocation" in rendered
    assert "bedrock:CallWithBearerToken" in rendered
    assert "__emergency_stop_inactive__" in rendered
    assert "EmergencyStopFailure" in rendered
    assert "dynamodb:LeadingKeys" in rendered
    assert "CONFIG#EMERGENCY_STOP" in rendered
    assert "cloudwatch:GetMetricData" in rendered
    assert "cloudwatch:DescribeAlarms" in rendered
    assert "cloudwatch:*" not in rendered
    assert "emergency_failure" in rendered
    assert "emergency_dlq" in rendered
    assert "EmergencyDenyPolicyArn" in template.to_json()["Outputs"]
    assert "EmergencyKeySecretArn" in template.to_json()["Outputs"]


def test_admin_ui_remains_opt_in():
    disabled = _template({"manage_invocation_logging": True})
    disabled.resource_count_is("AWS::CloudFront::Distribution", 0)
    disabled.has_resource_properties(
        "AWS::Lambda::Url",
        Match.not_(Match.object_like({"Cors": Match.any_value()})),
    )
    enabled = _template(
        {
            "manage_invocation_logging": True,
            "admin_ui": True,
            "admin_jwt_claim": "cognito:groups",
            "admin_jwt_value": "quota-admins",
        }
    )
    enabled.resource_count_is("AWS::CloudFront::Distribution", 1)
    enabled.resource_count_is("AWS::Cognito::IdentityPool", 1)
    rendered = json.dumps(enabled.to_json())
    assert "createGroup" in rendered
    assert "GroupExistsException" in rendered
    assert "quota-admins" in rendered
    assert '"Exclude": ["config.js"]' in rendered
    function_url = next(
        iter(enabled.find_resources("AWS::Lambda::Url").values())
    )
    cors = function_url["Properties"]["Cors"]
    assert cors["AllowMethods"] == ["GET", "POST", "PUT"]
    assert "authorization" in cors["AllowHeaders"]
    assert cors["AllowOrigins"] != ["*"]
    assert "AdminUiDistribution" in json.dumps(cors["AllowOrigins"])


@pytest.mark.parametrize(
    "context",
    [
        {
            "admin_jwt_claim": "groups",
            "admin_jwt_value": "",
        },
        {
            "admin_jwt_claim": "",
            "admin_jwt_value": "quota-admins",
        },
    ],
)
def test_admin_jwt_claim_and_value_must_be_configured_together(context):
    with pytest.raises(ValueError, match="configured together"):
        _template({"manage_invocation_logging": True, **context})


def test_admin_ui_rejects_unsupported_or_unauthorized_identity_setup():
    with pytest.raises(ValueError, match="stack-created demo Cognito"):
        _template(
            {
                "manage_invocation_logging": True,
                "admin_ui": True,
                "jwt_issuer": "https://idp.example.com",
                "admin_jwt_claim": "groups",
                "admin_jwt_value": "quota-admins",
            }
        )
    with pytest.raises(ValueError, match="browser never receives"):
        _template(
            {
                "manage_invocation_logging": True,
                "admin_ui": True,
            }
        )


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("auto_provision_users", "yes", "must be true or false"),
        ("manage_invocation_logging", "yes", "must be true or false"),
        ("retain_tables_on_delete", "sometimes", "must be true or false"),
        ("snapstart", "1", "must be true or false"),
        ("default_daily_usd", 0, "positive number"),
        ("default_daily_input_tokens", -1, "positive integer"),
        ("default_daily_output_tokens", 1.5, "positive integer"),
        ("usage_retention_days", 0, "positive integer"),
        ("warn_threshold", 0, "positive number"),
        ("warn_threshold", 1, "less than 1"),
        ("vended_ttl_seconds", 899, "between 900"),
        ("vended_ttl_seconds", 3_601, "role-chaining maximum is 3600"),
        ("credential_enforcement_mode", "unknown", "must be one of"),
        ("permission_lease_seconds", 120, "one of 60, 300, 900"),
        ("refresh_overlap_seconds", 0, "positive integer"),
        ("refresh_jitter_seconds", -1, "non-negative integer"),
        ("vend_rate_limit_per_minute", 0, "positive integer"),
        ("revocation_policy_shards", 0, "positive integer"),
        ("revocation_policy_shards", 20, "emergency policy uses the twentieth"),
        ("revocation_reconcile_minutes", 0, "positive integer"),
        ("allowed_model_arns", [], "must not be empty"),
        ("allowed_model_arns", ["openai.model"], "resource ARN"),
        ("invoker_principal_arns", ["not-an-arn"], "principal ARNs"),
    ],
)
def test_invalid_deployment_values_fail_synth(key, value, message):
    with pytest.raises(ValueError, match=message):
        _template({"manage_invocation_logging": True, key: value})


def test_model_pricing_accepts_validated_json_and_conservative_fallback():
    model_config = {
        "catalog_models": {"catalog-name": ["provider.dynamic-model"]},
        "price_overrides": {
            "provider.pinned-model": {
                "input_per_mtok": 7,
                "output_per_mtok": 21,
            }
        },
        "fallback_price": {
            "input_per_mtok": 50,
            "output_per_mtok": 100,
        },
    }
    template = _template(
        {
            "manage_invocation_logging": True,
            "model_config": model_config,
        }
    )
    template.has_resource_properties(
        "Custom::BedrockModelPriceSnapshot",
        {
            "CatalogModels": {
                "catalog-name": ["provider.dynamic-model"]
            },
            "PinnedPrices": {
                "provider.pinned-model": {
                    "input_per_mtok": 7.0,
                    "output_per_mtok": 21.0,
                }
            },
            "FallbackPrice": {
                "input_per_mtok": 50.0,
                "output_per_mtok": 100.0,
            },
        },
    )


def test_deployment_file_loads_and_context_overrides_it(tmp_path):
    model_path = tmp_path / "models.json"
    model_path.write_text(
        json.dumps(
            {
                "catalog_models": {"catalog": ["provider.model"]},
                "price_overrides": {},
                "fallback_price": {
                    "input_per_mtok": 20,
                    "output_per_mtok": 80,
                },
            }
        )
    )
    deployment_path = tmp_path / "deployment.json"
    deployment_path.write_text(
        json.dumps(
            {
                "manage_invocation_logging": True,
                "default_daily_usd": 5,
                "model_config": "models.json",
            }
        )
    )
    template = _template(
        {
            "deployment_config": str(deployment_path),
            "default_daily_usd": 9,
        }
    )
    assert (
        _environment_with(template, "BEDROCK_USER_ROLE_ARN")[
            "DEFAULT_DAILY_USD"
        ]
        == "9.0"
    )


def test_model_config_rejects_duplicate_and_non_positive_prices():
    duplicate = {
        "catalog_models": {"catalog": ["provider.model"]},
        "price_overrides": {
            "provider.model": {
                "input_per_mtok": 1,
                "output_per_mtok": 2,
            }
        },
        "fallback_price": {
            "input_per_mtok": 20,
            "output_per_mtok": 80,
        },
    }
    with pytest.raises(ValueError, match="Duplicate model price mapping"):
        _template(
            {
                "manage_invocation_logging": True,
                "model_config": duplicate,
            }
        )
    duplicate["price_overrides"] = {}
    duplicate["fallback_price"]["input_per_mtok"] = 0
    with pytest.raises(ValueError, match="positive number"):
        _template(
            {
                "manage_invocation_logging": True,
                "model_config": duplicate,
            }
        )
