#!/usr/bin/env python3
import os

import aws_cdk as cdk

from stacks.quota_gateway_stack import QuotaGatewayStack

app = cdk.App()
QuotaGatewayStack(
    app, "BedrockPerUserQuotaGateway",
    env=cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=os.environ.get("CDK_DEFAULT_REGION", "us-east-1"),
    ),
    description="Runtime-only per-user quota broker for Amazon Bedrock",
)
app.synth()
