import json

import aws_cdk as cdk
import pytest
from aws_cdk import aws_lambda as lambda_
from aws_cdk.assertions import Match, Template

import cdk.stacks.quota_gateway_stack as stack_module
from cdk.stacks.quota_gateway_stack import QuotaGatewayStack


@pytest.fixture(autouse=True)
def no_docker_asset_bundling(monkeypatch):
    inline = lambda_.Code.from_inline("def handler(event, context): return {}")
    monkeypatch.setattr(stack_module.lambda_.Code, "from_asset",
                        lambda *args, **kwargs: inline)


def _template(context: dict[str, str]) -> Template:
    app = cdk.App(context=context)
    stack = QuotaGatewayStack(
        app,
        "TestStack",
        env=cdk.Environment(account="111122223333", region="us-east-1"),
    )
    return Template.from_stack(stack)


def _lambda_environment(template: Template, *, handler: str) -> dict:
    functions = template.find_resources("AWS::Lambda::Function")
    function = next(
        resource for resource in functions.values()
        if resource["Properties"].get("Handler") == handler
        and "Environment" in resource["Properties"]
    )
    return function["Properties"]["Environment"]["Variables"]


def test_invocation_logging_requires_explicit_choice():
    with pytest.raises(ValueError, match="explicit consent"):
        _template({})


def test_managed_logging_stack_has_ttl_serial_reconciler_and_retention():
    template = _template({"manage_invocation_logging": "true"})

    template.has_resource_properties(
        "Custom::BedrockModelPriceSnapshot",
        {
            "RegionCode": "us-east-1",
            "CatalogModels": {
                "gpt-oss-120b": [
                    "openai.gpt-oss-120b",
                    "openai.gpt-oss-120b-1:0",
                ],
                "gpt-oss-20b": [
                    "openai.gpt-oss-20b",
                    "openai.gpt-oss-20b-1:0",
                ],
            },
        },
    )
    template.has_resource_properties(
        "AWS::IAM::Policy",
        {
            "PolicyDocument": {
                "Statement": Match.array_with([
                    Match.object_like({
                        "Action": "pricing:GetProducts",
                        "Effect": "Allow",
                        "Resource": "*",
                    })
                ])
            }
        },
    )

    template.resource_properties_count_is(
        "AWS::DynamoDB::Table",
        {
            "TimeToLiveSpecification": {
                "AttributeName": "expires_at",
                "Enabled": True,
            },
        },
        2,
    )
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Handler": "handler.handler",
            "ReservedConcurrentExecutions": 1,
        },
    )
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
            "Properties": Match.object_like({
                "AssumeRolePolicyDocument": Match.object_like({
                    "Statement": Match.array_with([
                        Match.object_like({
                            "Principal": {"Service": "bedrock.amazonaws.com"},
                            "Condition": Match.object_like({
                                "StringEquals": {
                                    "aws:SourceAccount": "111122223333"
                                },
                            }),
                        })
                    ])
                }),
                "Policies": Match.array_with([
                    Match.object_like({"PolicyName": "WriteInvocationLogs"})
                ]),
            }),
        },
    )

    roles = template.find_resources("AWS::IAM::Role")
    logging_role = next(
        role for role in roles.values()
        if role.get("Properties", {}).get("Policies", [{}])[0].get(
            "PolicyName"
        ) == "WriteInvocationLogs"
    )
    log_stream_arn = (
        logging_role["Properties"]["Policies"][0]["PolicyDocument"]
        ["Statement"][0]["Resource"]
    )
    assert log_stream_arn == {
        "Fn::Join": [
            "",
            [
                "arn:",
                {"Ref": "AWS::Partition"},
                ":logs:us-east-1:111122223333:log-group:",
                {"Ref": "BedrockInvocationLogs9E813CF7"},
                ":log-stream:aws/bedrock/modelinvocations",
            ],
        ]
    }

    functions = template.find_resources("AWS::Lambda::Function")
    metered_functions = [
        function for function in functions.values()
        if function["Properties"].get("Handler") == "run.sh"
        or function["Properties"].get("ReservedConcurrentExecutions") == 1
    ]
    assert len(metered_functions) == 2
    for function in metered_functions:
        price_value = (
            function["Properties"]["Environment"]["Variables"]
            ["MODEL_PRICES_JSON"]
        )
        assert price_value["Fn::GetAtt"][1] == "ModelPricesJson"


def test_existing_log_group_avoids_account_wide_custom_resource():
    template = _template({
        "manage_invocation_logging": "false",
        "invocation_log_group_name": "/existing/bedrock/invocations",
    })

    template.resource_count_is("Custom::AWS", 0)


def test_default_quota_configuration_is_injected_into_lambdas():
    template = _template({"manage_invocation_logging": "true"})
    gateway_env = _lambda_environment(template, handler="run.sh")
    reconciler_env = _lambda_environment(template, handler="handler.handler")

    assert gateway_env["AUTO_PROVISION_USERS"] == "true"
    assert gateway_env["DEFAULT_DAILY_USD"] == "1.0"
    assert gateway_env["DEFAULT_DAILY_INPUT_TOKENS"] == "1000000"
    assert gateway_env["DEFAULT_DAILY_OUTPUT_TOKENS"] == "200000"
    assert gateway_env["USAGE_RETENTION_DAYS"] == "35"
    assert gateway_env["MODE_B_ALLOWED_MODEL_IDS_JSON"] == "[]"
    assert reconciler_env["WARN_THRESHOLD"] == "0.8"
    assert reconciler_env["USAGE_RETENTION_DAYS"] == "35"
    assert (
        gateway_env["MODEL_FALLBACK_PRICE_JSON"]
        == reconciler_env["MODEL_FALLBACK_PRICE_JSON"]
    )
    assert gateway_env["MODEL_FALLBACK_PRICE_JSON"]["Fn::GetAtt"][1] == (
        "FallbackPriceJson"
    )

    for table in template.find_resources("AWS::DynamoDB::Table").values():
        assert table["DeletionPolicy"] == "Delete"
        assert table["UpdateReplacePolicy"] == "Delete"


def test_gateway_can_delete_stored_mantle_responses():
    template = _template({"manage_invocation_logging": "true"})

    template.has_resource_properties(
        "AWS::IAM::Policy",
        {
            "PolicyDocument": {
                "Statement": Match.array_with([
                    Match.object_like({
                        "Action": "bedrock-mantle:DeleteInference",
                        "Effect": "Allow",
                        "Resource": {
                            "Fn::Join": [
                                "",
                                [
                                    "arn:",
                                    {"Ref": "AWS::Partition"},
                                    (
                                        ":bedrock-mantle:us-east-1:"
                                        "111122223333:project/*"
                                    ),
                                ],
                            ]
                        },
                    })
                ])
            }
        },
    )


def test_reconciler_interval_defaults_to_five_minutes():
    template = _template({"manage_invocation_logging": "true"})
    template.has_resource_properties(
        "AWS::Events::Rule", {"ScheduleExpression": "rate(5 minutes)"}
    )


def test_reconciler_interval_is_configurable():
    template = _template({
        "manage_invocation_logging": "true",
        "reconciler_interval_minutes": 1,
    })
    template.has_resource_properties(
        "AWS::Events::Rule", {"ScheduleExpression": "rate(1 minute)"}
    )


def test_custom_quota_configuration_and_table_retention():
    template = _template({
        "manage_invocation_logging": True,
        "auto_provision_users": False,
        "default_daily_usd": 12.5,
        "default_daily_input_tokens": 2_000_000,
        "default_daily_output_tokens": 300_000,
        "warn_threshold": 0.65,
        "usage_retention_days": 90,
        "retain_tables_on_delete": True,
    })
    gateway_env = _lambda_environment(template, handler="run.sh")
    reconciler_env = _lambda_environment(template, handler="handler.handler")

    assert gateway_env["AUTO_PROVISION_USERS"] == "false"
    assert gateway_env["DEFAULT_DAILY_USD"] == "12.5"
    assert gateway_env["DEFAULT_DAILY_INPUT_TOKENS"] == "2000000"
    assert gateway_env["DEFAULT_DAILY_OUTPUT_TOKENS"] == "300000"
    assert gateway_env["USAGE_RETENTION_DAYS"] == "90"
    assert reconciler_env["WARN_THRESHOLD"] == "0.65"
    assert reconciler_env["USAGE_RETENTION_DAYS"] == "90"

    for table in template.find_resources("AWS::DynamoDB::Table").values():
        assert table["DeletionPolicy"] == "Retain"
        assert table["UpdateReplacePolicy"] == "Retain"


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
        ("reconciler_interval_minutes", 0, "positive integer"),
        ("reconciler_interval_minutes", 1.5, "positive integer"),
        ("reconciler_interval_minutes", "x", "positive integer"),
        ("warn_threshold", 0, "positive number"),
        ("warn_threshold", 1, "less than 1"),
        ("warn_threshold", 1.1, "less than 1"),
    ],
)
def test_invalid_deployment_values_fail_synth(key, value, message):
    with pytest.raises(ValueError, match=message):
        _template({"manage_invocation_logging": True, key: value})


def test_invocation_logging_rejects_incompatible_combinations():
    with pytest.raises(ValueError, match="incompatible"):
        _template({
            "manage_invocation_logging": True,
            "invocation_log_group_name": "/existing/group",
        })
    with pytest.raises(ValueError, match="requires invocation_log_group_name"):
        _template({"manage_invocation_logging": False})


def test_mode_allowlists_are_applied_to_different_enforcement_layers():
    mode_a_arns = [
        "arn:aws:bedrock:us-east-1::foundation-model/openai.gpt-oss-120b-1:0",
        "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-opus-4-7",
    ]
    mode_b_ids = ["openai.gpt-oss-120b"]
    template = _template({
        "manage_invocation_logging": True,
        "mode_a_allowed_model_arns": mode_a_arns,
        "mode_b_allowed_model_ids": mode_b_ids,
    })

    gateway_env = _lambda_environment(template, handler="run.sh")
    assert json.loads(gateway_env["MODE_B_ALLOWED_MODEL_IDS_JSON"]) == mode_b_ids
    template.has_resource_properties(
        "AWS::IAM::Policy",
        {
            "PolicyDocument": {
                "Statement": Match.array_with([
                    Match.object_like({
                        "Action": Match.array_with([
                            "bedrock:InvokeModel",
                            "bedrock:Converse",
                        ]),
                        "Resource": mode_a_arns,
                    })
                ])
            }
        },
    )


def test_invoker_principal_arns_are_preserved():
    principal_arn = "arn:aws:iam::111122223333:role/GatewayInvoker"
    template = _template({
        "manage_invocation_logging": True,
        "invoker_principal_arns": [principal_arn],
    })
    template.has_resource_properties(
        "AWS::Lambda::Permission",
        {
            "Action": "lambda:InvokeFunctionUrl",
            "FunctionUrlAuthType": "AWS_IAM",
            "Principal": principal_arn,
        },
    )


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        (
            "mode_b_allowed_model_ids",
            ["arn:aws:bedrock:us-east-1::foundation-model/example"],
            "application model IDs",
        ),
        ("mode_a_allowed_model_arns", ["openai.gpt-oss-120b"], "resource ARNs"),
        ("mode_a_allowed_model_arns", [], "must not be empty"),
        ("invoker_principal_arns", ["not-an-arn"], "principal ARNs"),
    ],
)
def test_allowlist_validation(key, value, message):
    with pytest.raises(ValueError, match=message):
        _template({"manage_invocation_logging": True, key: value})


def test_model_pricing_can_be_supplied_as_validated_json_context():
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
    template = _template({
        "manage_invocation_logging": True,
        "model_config": model_config,
    })
    template.has_resource_properties(
        "Custom::BedrockModelPriceSnapshot",
        {
            "CatalogModels": {
                "catalog-name": ["provider.dynamic-model"],
            },
            "PinnedPrices": {
                "provider.pinned-model": {
                    "input_per_mtok": 7.0,
                    "output_per_mtok": 21.0,
                },
            },
            "FallbackPrice": {
                "input_per_mtok": 50.0,
                "output_per_mtok": 100.0,
            },
        },
    )
    gateway_env = _lambda_environment(template, handler="run.sh")
    reconciler_env = _lambda_environment(template, handler="handler.handler")
    assert (
        gateway_env["MODEL_FALLBACK_PRICE_JSON"]
        == reconciler_env["MODEL_FALLBACK_PRICE_JSON"]
    )
    assert gateway_env["MODEL_FALLBACK_PRICE_JSON"]["Fn::GetAtt"][1] == (
        "FallbackPriceJson"
    )


def test_deployment_file_loads_and_context_overrides_it(tmp_path):
    model_path = tmp_path / "models.json"
    model_path.write_text(json.dumps({
        "catalog_models": {"catalog": ["provider.model"]},
        "price_overrides": {},
        "fallback_price": {
            "input_per_mtok": 20,
            "output_per_mtok": 80,
        },
    }))
    deployment_path = tmp_path / "deployment.json"
    deployment_path.write_text(json.dumps({
        "manage_invocation_logging": True,
        "default_daily_usd": 5,
        "model_config": "models.json",
    }))

    template = _template({
        "deployment_config": str(deployment_path),
        "default_daily_usd": 9,
    })
    gateway_env = _lambda_environment(template, handler="run.sh")
    assert gateway_env["DEFAULT_DAILY_USD"] == "9.0"


def test_model_config_rejects_duplicate_or_non_positive_prices():
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
        _template({
            "manage_invocation_logging": True,
            "model_config": duplicate,
        })

    duplicate["price_overrides"] = {}
    duplicate["fallback_price"]["input_per_mtok"] = 0
    with pytest.raises(ValueError, match="positive number"):
        _template({
            "manage_invocation_logging": True,
            "model_config": duplicate,
        })
