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


def test_invocation_logging_requires_explicit_choice():
    with pytest.raises(ValueError, match="explicit consent"):
        _template({})


def test_managed_logging_stack_has_ttl_serial_reconciler_and_retention():
    template = _template({"manage_invocation_logging": "true"})

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


def test_existing_log_group_avoids_account_wide_custom_resource():
    template = _template({
        "manage_invocation_logging": "false",
        "invocation_log_group_name": "/existing/bedrock/invocations",
    })

    template.resource_count_is("Custom::AWS", 0)
