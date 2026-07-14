"""CDK stack for the Bedrock per-user quota gateway.

Resources:
- DynamoDB: users table (limits/status per JWT subject), usage table (TTL)
- Gateway Lambda: FastAPI behind the AWS Lambda Web Adapter, exposed via a
  Function URL in RESPONSE_STREAM mode so SSE streaming passes through
- JWT identity: bring your own OIDC issuer via ``-c jwt_issuer=...``
  (optionally ``-c jwt_audience=...``), or let the stack create a demo
  Cognito User Pool
- Admin key in Secrets Manager
- Reconciler Lambda on a 5-minute EventBridge schedule + SNS alert topic
- CloudWatch dashboard over the gateway's EMF metrics
"""

import aws_cdk as cdk
from aws_cdk import (
    Duration,
    RemovalPolicy,
    Stack,
    aws_cloudwatch as cw,
    aws_cognito as cognito,
    aws_dynamodb as ddb,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_secretsmanager as sm,
    aws_sns as sns,
    aws_sns_subscriptions as subs,
    custom_resources as cr,
)
from constructs import Construct

METRICS_NAMESPACE = "BedrockQuotaGateway"

# Price List model names mapped to every ID the gateway and Bedrock invocation
# logs can emit. A deployment-time custom resource resolves these into one
# immutable USD-per-MTok snapshot shared by the gateway and reconciler.
PRICE_CATALOG_MODELS = {
    "gpt-oss-120b": [
        "openai.gpt-oss-120b",
        "openai.gpt-oss-120b-1:0",
    ],
    "gpt-oss-20b": [
        "openai.gpt-oss-20b",
        "openai.gpt-oss-20b-1:0",
    ],
}

# Price List API does not yet expose recent Claude models. Keep explicit
# deployment-time overrides for those gaps; unknown models still use the
# conservative runtime fallback.
PINNED_PRICE_OVERRIDES = {
    "anthropic.claude-opus-4-7": {
        "input_per_mtok": 15.00,
        "output_per_mtok": 75.00,
    },
}

# MODEL_PRICES_JSON is injected into BOTH Lambdas. The gateway prices at
# settle time; the reconciler is authoritative for native-vended traffic.
#
# CACHE-PRICING CAVEAT (Mode A): Bedrock model-invocation logs record only
# input.inputTokenCount / output.outputTokenCount — there are NO cache-token
# fields — so the reconciler cannot apply the gateway's prompt-cache
# multipliers to native-vended traffic. The gateway (proxy path) is
# cache-aware; the reconciler (native path) is not, so for cache-heavy
# coding-agent workloads the two pricers differ and native dollar enforcement
# is approximate. Cache-accurate native metering would require enabling
# text-data delivery and parsing inputBodyJson. See README for the tradeoff.


class QuotaGatewayStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        alert_email = self.node.try_get_context("alert_email")
        jwt_issuer = self.node.try_get_context("jwt_issuer")
        jwt_audience = self.node.try_get_context("jwt_audience") or ""
        jwt_jwks_url = self.node.try_get_context("jwt_jwks_url") or ""
        jwt_user_claim = self.node.try_get_context("jwt_user_claim") or "sub"
        # Vended-credential lifetime = the broker's AssumeRole DurationSeconds
        # (VENDED_CREDENTIAL_TTL_SECONDS env). STS bounds AssumeRole duration to
        # 900s–43200s. The vended role's max_session_duration is derived from it
        # (see below) so DurationSeconds can never exceed the role's ceiling.
        vended_ttl_seconds = int(self.node.try_get_context("vended_ttl_seconds") or 900)
        if not 900 <= vended_ttl_seconds <= 43200:
            raise ValueError(
                "vended_ttl_seconds must be between 900 (15 min) and 43200 "
                f"(12h, the STS AssumeRole duration limit); got {vended_ttl_seconds}."
            )
        # A role's max_session_duration floor is 3600s (STS), independent of the
        # AssumeRole duration floor (900s), so lift the ceiling to at least 1h.
        max_session_seconds = max(vended_ttl_seconds, 3600)
        # -c snapstart=true: resume the gateway from a Firecracker microVM
        # snapshot instead of cold-starting (Python SnapStart). Requires
        # publishing versions; the Function URL then targets an alias.
        use_snapstart = str(self.node.try_get_context("snapstart")).lower() == "true"

        # ------------------------------------------------------------------
        # Deployment-time Bedrock price snapshot
        # ------------------------------------------------------------------
        price_resolver_fn = lambda_.Function(
            self, "PriceResolverFn",
            runtime=lambda_.Runtime.PYTHON_3_12,
            memory_size=256,
            timeout=Duration.minutes(1),
            handler="handler.handler",
            code=lambda_.Code.from_asset("pricing_resolver"),
        )
        price_resolver_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["pricing:GetProducts"],
                resources=["*"],
            )
        )
        price_provider = cr.Provider(
            self, "PriceResolverProvider",
            on_event_handler=price_resolver_fn,
        )
        price_snapshot = cdk.CustomResource(
            self, "BedrockModelPriceSnapshot",
            service_token=price_provider.service_token,
            resource_type="Custom::BedrockModelPriceSnapshot",
            properties={
                "RegionCode": self.region,
                "CatalogModels": PRICE_CATALOG_MODELS,
                "PinnedPrices": PINNED_PRICE_OVERRIDES,
            },
        )
        model_prices_json = price_snapshot.get_att_string("ModelPricesJson")

        # ------------------------------------------------------------------
        # Identity: BYO OIDC issuer, or a demo Cognito User Pool
        # ------------------------------------------------------------------
        user_pool = None
        if not jwt_issuer:
            user_pool = cognito.UserPool(
                self, "DemoUserPool",
                self_sign_up_enabled=False,
                sign_in_aliases=cognito.SignInAliases(username=True, email=True),
                removal_policy=RemovalPolicy.DESTROY,
            )
            user_pool_client = user_pool.add_client(
                "DemoAppClient",
                auth_flows=cognito.AuthFlow(user_password=True, user_srp=True),
                generate_secret=False,
                id_token_validity=Duration.hours(12),
            )
            jwt_issuer = (
                f"https://cognito-idp.{self.region}.amazonaws.com/{user_pool.user_pool_id}"
            )
            # ID tokens carry the app client id as `aud`.
            jwt_audience = user_pool_client.user_pool_client_id

        # ------------------------------------------------------------------
        # DynamoDB
        # ------------------------------------------------------------------
        users_table = ddb.Table(
            self, "UsersTable",
            partition_key=ddb.Attribute(name="user_id", type=ddb.AttributeType.STRING),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="expires_at",
            removal_policy=RemovalPolicy.DESTROY,  # sample: destroy on teardown
        )

        usage_table = ddb.Table(
            self, "UsageTable",
            partition_key=ddb.Attribute(name="user_id", type=ddb.AttributeType.STRING),
            sort_key=ddb.Attribute(name="window", type=ddb.AttributeType.STRING),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="expires_at",
            removal_policy=RemovalPolicy.DESTROY,
        )

        # ------------------------------------------------------------------
        # Admin key
        # ------------------------------------------------------------------
        admin_secret = sm.Secret(
            self, "AdminApiKey",
            description="Admin key for the quota gateway /admin API",
            generate_secret_string=sm.SecretStringGenerator(
                exclude_punctuation=True, password_length=40,
            ),
        )

        # ------------------------------------------------------------------
        # Gateway Lambda (FastAPI + Lambda Web Adapter, streaming)
        # ------------------------------------------------------------------
        # AWS Lambda Web Adapter public layer (zip packaging). Name/version
        # per https://github.com/awslabs/aws-lambda-web-adapter — override
        # with -c adapter_layer_arn=... if a newer version ships.
        adapter_layer_arn = self.node.try_get_context("adapter_layer_arn") or (
            f"arn:aws:lambda:{self.region}:753240598075:layer:LambdaAdapterLayerX86:28"
        )
        adapter_layer = lambda_.LayerVersion.from_layer_version_arn(
            self, "WebAdapterLayer", adapter_layer_arn,
        )

        gateway_fn = lambda_.Function(
            self, "GatewayFn",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.X86_64,
            memory_size=1024,
            timeout=Duration.minutes(5),
            handler="run.sh",
            layers=[adapter_layer],
            snap_start=lambda_.SnapStartConf.ON_PUBLISHED_VERSIONS if use_snapstart else None,
            code=lambda_.Code.from_asset(
                "../gateway",
                bundling=cdk.BundlingOptions(
                    image=lambda_.Runtime.PYTHON_3_12.bundling_image,
                    command=[
                        "bash", "-c",
                        # Force x86_64 manylinux wheels so bundling on
                        # arm64 hosts (Apple Silicon) can't produce
                        # aarch64 native deps for this x86_64 function.
                        "pip install -r requirements.txt "
                        "--platform manylinux2014_x86_64 --implementation cp "
                        "--python-version 3.12 --only-binary=:all: "
                        "--target /asset-output "
                        "&& cp -r app run.sh /asset-output/ "
                        "&& chmod +x /asset-output/run.sh",
                    ],
                ),
            ),
            environment={
                # Lambda Web Adapter wiring
                "AWS_LAMBDA_EXEC_WRAPPER": "/opt/bootstrap",
                "AWS_LWA_INVOKE_MODE": "response_stream",
                "PORT": "8080",
                # App config
                "USERS_TABLE": users_table.table_name,
                "USAGE_TABLE": usage_table.table_name,
                "METRICS_NAMESPACE": METRICS_NAMESPACE,
                "ADMIN_KEY_SECRET_ARN": admin_secret.secret_arn,
                # Single price source shared with the reconciler (prevents drift).
                "MODEL_PRICES_JSON": model_prices_json,
                # JWT auth
                "JWT_ISSUER": jwt_issuer,
                "JWT_AUDIENCE": jwt_audience,
                "JWT_JWKS_URL": jwt_jwks_url,
                "JWT_USER_CLAIM": jwt_user_claim,
            },
        )

        users_table.grant_read_write_data(gateway_fn)
        usage_table.grant_read_write_data(gateway_fn)
        admin_secret.grant_read(gateway_fn)
        # Permissions to mint short-term Bedrock API keys from the role and
        # call the bedrock-mantle endpoint. Scope the project resource down
        # if you use dedicated mantle Projects.
        gateway_fn.role.add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name("AmazonBedrockMantleInferenceAccess")
        )

        # ------------------------------------------------------------------
        # Per-user vended role (the API-agnostic enforcement path)
        #
        # The broker assumes this role on behalf of an in-budget user, with
        # RoleSessionName + SourceIdentity = a sanitized id derived from the
        # identity claim (reverse-mapped for metering). The user then calls
        # Bedrock NATIVELY (InvokeModel / Converse / streaming, any provider)
        # with the short-lived creds. Scoped to Bedrock invoke actions on all
        # models in this account/region; tighten `resources` to specific
        # model ARNs to restrict which models users may call.
        # ------------------------------------------------------------------
        bedrock_user_role = iam.Role(
            self, "BedrockUserRole",
            # Only the gateway (broker) Lambda role may assume this, and only
            # while setting a SourceIdentity + session tag it can't forge for
            # another user. Users never hold static Bedrock access (the
            # deny-direct policy below enforces "must go through the broker").
            assumed_by=iam.ArnPrincipal(gateway_fn.role.role_arn),
            # >= the vended TTL (and >= the STS 3600s floor), so the broker's
            # AssumeRole DurationSeconds can never exceed the role's ceiling.
            max_session_duration=Duration.seconds(max_session_seconds),
            description="Short-lived, per-user Bedrock access vended by the quota broker.",
        )
        # STS requires sts:SetSourceIdentity and sts:TagSession on BOTH the
        # caller's identity policy (granted below) AND this role's trust
        # policy. `assumed_by` only emits sts:AssumeRole, so without these the
        # broker's assume_role(SourceIdentity=, Tags=) call fails with
        # AccessDenied for every user. Scope to the gateway principal only.
        bedrock_user_role.assume_role_policy.add_statements(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                principals=[iam.ArnPrincipal(gateway_fn.role.role_arn)],
                actions=["sts:SetSourceIdentity", "sts:TagSession"],
            )
        )
        bedrock_user_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                    "bedrock:Converse",
                    "bedrock:ConverseStream",
                ],
                # bedrock-runtime ONLY, deliberately. Model-invocation logging
                # (the reconciler's metering source) captures ONLY the
                # bedrock-runtime endpoint — mantle (OpenAI/Anthropic-compatible)
                # calls are NOT logged, so granting bedrock-mantle:* here would
                # let a user spend via vended creds with ZERO metering and no
                # enforcement. Mode A's native path must stay on the logged
                # endpoint. (Mode B proxies mantle in-band and meters there.)
                # All models in this account/region (sample default); tighten
                # `resources` to specific model ARNs to restrict which models
                # users may call.
                resources=["*"],
            )
        )
        # Let the gateway role assume the vended role AND stamp the per-user
        # identity/tag. SetSourceIdentity + TagSession must be granted on the
        # *caller* side too, not just allowed by the trust policy.
        gateway_fn.role.add_to_principal_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["sts:AssumeRole", "sts:SetSourceIdentity", "sts:TagSession"],
                resources=[bedrock_user_role.role_arn],
            )
        )
        gateway_fn.add_environment("BEDROCK_USER_ROLE_ARN", bedrock_user_role.role_arn)
        # Same knob that sized max_session_duration above (kept in lockstep).
        gateway_fn.add_environment("VENDED_CREDENTIAL_TTL_SECONDS", str(vended_ttl_seconds))

        # ------------------------------------------------------------------
        # Bedrock model-invocation logging -> CloudWatch Logs.
        #
        # This is the source of per-call token counts across every bedrock-
        # runtime invoke path (InvokeModel/Converse/streaming, all providers);
        # each record's identity.arn carries the RoleSessionName (= sanitized
        # JWT sub) so the reconciler can meter per user.
        #
        # IMPORTANT: model-invocation logging is a SINGLE ACCOUNT + REGION-WIDE
        # Bedrock setting (one config per region). Managing it from this stack
        # therefore OVERWRITES any existing configuration on deploy (e.g. a
        # security team's central sink). Management requires explicit opt-in,
        # and the resulting configuration, log group, and writer role are
        # RETAINED on `cdk destroy` because the stack cannot restore whatever
        # was configured before.
        #
        # In a shared account, deploy with:
        #   -c manage_invocation_logging=false
        #   -c invocation_log_group_name=/your/existing/bedrock/log-group
        # and this stack will read your existing group instead of touching the
        # account-wide setting. That group must already receive model-
        # invocation logs whose identity.arn carries the vended session name.
        # ------------------------------------------------------------------
        manage_logging_context = self.node.try_get_context("manage_invocation_logging")
        manage_logging = str(manage_logging_context).lower() == "true"
        existing_log_group_name = self.node.try_get_context("invocation_log_group_name")

        if existing_log_group_name:
            # Bring-your-own group: never mutate the account-wide setting.
            invocation_log_group = logs.LogGroup.from_log_group_name(
                self, "BedrockInvocationLogs", existing_log_group_name
            )
            manage_logging = False
        elif manage_logging_context is None:
            raise ValueError(
                "Bedrock model-invocation logging is an account + region-wide "
                "setting, so this stack will not change it without explicit "
                "consent. Use -c manage_invocation_logging=true to let this "
                "sample manage it, or use -c manage_invocation_logging=false "
                "-c invocation_log_group_name=<existing-group>."
            )
        elif not manage_logging:
            # No group given AND not managing the account-wide config: the
            # reconciler would query a log group that receives nothing, so
            # Mode A native spend is metered as zero and never enforced. Fail
            # loudly rather than deploy a gateway that silently meters nothing.
            raise ValueError(
                "manage_invocation_logging=false requires "
                "-c invocation_log_group_name=<an existing log group that "
                "already receives Bedrock model-invocation logs>. Without it "
                "the reconciler has no usage source and Mode A budgets would "
                "not be enforced. To let this stack manage logging itself, "
                "set -c manage_invocation_logging=true explicitly."
            )
        else:
            invocation_log_group = logs.LogGroup(
                self, "BedrockInvocationLogs",
                log_group_name="/bedrock/quota-gateway/model-invocations",
                retention=logs.RetentionDays.TWO_WEEKS,
                removal_policy=RemovalPolicy.RETAIN,
            )

        if manage_logging:
            cdk.Annotations.of(self).add_warning(
                "This stack manages the ACCOUNT + REGION-WIDE Bedrock model-"
                "invocation logging configuration: deploy OVERWRITES any "
                "existing config. The configuration, log group, and writer "
                "role are RETAINED on `cdk destroy` because the prior "
                "configuration cannot be restored automatically. In a shared "
                "account, redeploy with -c manage_invocation_logging=false "
                "and -c invocation_log_group_name=<your existing group>."
            )
            # Role Bedrock uses to write the invocation logs.
            bedrock_logging_role = iam.Role(
                self, "BedrockLoggingRole",
                assumed_by=iam.ServicePrincipal(
                    "bedrock.amazonaws.com",
                    conditions={
                        "StringEquals": {"aws:SourceAccount": self.account},
                        "ArnLike": {
                            "aws:SourceArn": self.format_arn(
                                service="bedrock", resource="*"
                            )
                        },
                    },
                ),
                inline_policies={
                    "WriteInvocationLogs": iam.PolicyDocument(statements=[
                        iam.PolicyStatement(
                            actions=["logs:CreateLogStream", "logs:PutLogEvents"],
                            resources=[
                                self.format_arn(
                                    service="logs",
                                    resource="log-group",
                                    resource_name=(
                                        f"{invocation_log_group.log_group_name}:"
                                        "log-stream:aws/bedrock/modelinvocations"
                                    ),
                                    arn_format=cdk.ArnFormat.COLON_RESOURCE_NAME,
                                ),
                            ],
                        )
                    ])
                },
            )
            bedrock_logging_role.apply_removal_policy(RemovalPolicy.RETAIN)
            # Enable model-invocation logging account/region-wide.
            invocation_logging_config = cr.AwsCustomResource(
                self, "EnableBedrockInvocationLogging",
                on_create=cr.AwsSdkCall(
                    service="Bedrock",
                    action="putModelInvocationLoggingConfiguration",
                    parameters={
                        "loggingConfig": {
                            "cloudWatchConfig": {
                                "logGroupName": invocation_log_group.log_group_name,
                                "roleArn": bedrock_logging_role.role_arn,
                            },
                            "textDataDeliveryEnabled": False,
                            "imageDataDeliveryEnabled": False,
                            "embeddingDataDeliveryEnabled": False,
                        }
                    },
                    physical_resource_id=cr.PhysicalResourceId.of("bedrock-invocation-logging"),
                ),
                policy=cr.AwsCustomResourcePolicy.from_statements([
                    iam.PolicyStatement(
                        actions=[
                            "bedrock:PutModelInvocationLoggingConfiguration",
                            "bedrock:GetModelInvocationLoggingConfiguration",
                        ],
                        resources=["*"],
                    ),
                    # Required so Bedrock can validate it may pass the logging role.
                    iam.PolicyStatement(actions=["iam:PassRole"],
                                        resources=[bedrock_logging_role.role_arn]),
                ]),
                install_latest_aws_sdk=False,
            )
            invocation_logging_config.node.default_child.apply_removal_policy(
                RemovalPolicy.RETAIN
            )

        # SnapStart only applies to published versions, so with it enabled
        # the Function URL targets a "live" alias of the current version;
        # otherwise it targets $LATEST directly.
        if use_snapstart:
            url_target = lambda_.Alias(
                self, "GatewayLiveAlias",
                alias_name="live",
                version=gateway_fn.current_version,
            )
        else:
            url_target = gateway_fn

        # The Function URL enforces IAM (SigV4) auth: callers must be
        # signed AWS principals, so the URL is not anonymously reachable
        # (required by the Palisade "world accessible Lambda" slat —
        # AuthType NONE is a Sev-2 finding). The end-user's JWT still rides
        # in the Authorization/x-api-key header and drives the per-user
        # quota: SigV4 at the edge (who may call the gateway) + JWT in the
        # app (which user is spending). Defense in depth.
        fn_url = url_target.add_function_url(
            auth_type=lambda_.FunctionUrlAuthType.AWS_IAM,
            invoke_mode=lambda_.InvokeMode.RESPONSE_STREAM,
        )

        # Principals allowed to invoke the URL (your app-server/backend
        # roles, or your own role for the demo). Grant via
        # -c invoker_principal_arns=arn1,arn2 ; defaults to this account's
        # root so any IAM principal in the account can be granted normally.
        invoker_arns = self.node.try_get_context("invoker_principal_arns")
        if invoker_arns:
            for arn in [a.strip() for a in invoker_arns.split(",") if a.strip()]:
                fn_url.grant_invoke_url(iam.ArnPrincipal(arn))
        else:
            fn_url.grant_invoke_url(iam.AccountRootPrincipal())

        # ------------------------------------------------------------------
        # Lockdown helper: attach this policy to every role that should NOT
        # be able to bypass the gateway (dev roles, notebook roles, CI, ...).
        # For org-wide enforcement use the SCP in the README instead.
        # ------------------------------------------------------------------
        deny_direct = iam.ManagedPolicy(
            self, "DenyDirectBedrockInvocation",
            managed_policy_name="deny-direct-bedrock-invocation",
            description=(
                "Denies direct Bedrock model invocation so that inference must "
                "go through the quota gateway. Attach to non-gateway roles."
            ),
            statements=[
                iam.PolicyStatement(
                    effect=iam.Effect.DENY,
                    actions=[
                        "bedrock-mantle:CreateInference",
                        "bedrock-mantle:CallWithBearerToken",
                        "bedrock:InvokeModel",
                        "bedrock:InvokeModelWithResponseStream",
                        "bedrock:Converse",
                        "bedrock:ConverseStream",
                    ],
                    resources=["*"],
                ),
            ],
        )

        # ------------------------------------------------------------------
        # Reconciler + alerting
        # ------------------------------------------------------------------
        alert_topic = sns.Topic(self, "QuotaAlerts", display_name="Bedrock quota gateway alerts")
        if alert_email:
            alert_topic.add_subscription(subs.EmailSubscription(alert_email))

        reconciler_fn = lambda_.Function(
            self, "ReconcilerFn",
            runtime=lambda_.Runtime.PYTHON_3_12,
            memory_size=256,
            timeout=Duration.minutes(2),
            reserved_concurrent_executions=1,
            handler="handler.handler",
            code=lambda_.Code.from_asset("../reconciler"),
            environment={
                "USERS_TABLE": users_table.table_name,
                "USAGE_TABLE": usage_table.table_name,
                "SNS_TOPIC_ARN": alert_topic.topic_arn,
                "WARN_THRESHOLD": "0.8",
                # Native-vended usage EMF is emitted under the same namespace
                # as the gateway's so the dashboard reflects Mode A traffic too.
                "METRICS_NAMESPACE": METRICS_NAMESPACE,
                # Same price source as the gateway so the authoritative
                # reconciler cost can't drift from the gateway's settle cost.
                "MODEL_PRICES_JSON": model_prices_json,
                # Source of per-user token counts (any Bedrock API/provider).
                "INVOCATION_LOG_GROUP": invocation_log_group.log_group_name,
            },
        )
        users_table.grant_read_write_data(reconciler_fn)
        # Reconciler now WRITES authoritative metered usage (was read-only).
        usage_table.grant_read_write_data(reconciler_fn)
        alert_topic.grant_publish(reconciler_fn)
        # Query the model-invocation logs via CloudWatch Logs Insights.
        reconciler_fn.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["logs:StartQuery", "logs:GetQueryResults", "logs:StopQuery"],
                resources=["*"],  # Insights StartQuery does not support ARN scoping well
            )
        )

        events.Rule(
            self, "ReconcilerSchedule",
            schedule=events.Schedule.rate(Duration.minutes(5)),
            targets=[targets.LambdaFunction(reconciler_fn)],
        )

        # ------------------------------------------------------------------
        # Dashboard
        # ------------------------------------------------------------------
        dashboard = cw.Dashboard(self, "Dashboard", dashboard_name="bedrock-per-user-quota-gateway")

        def search_widget(title: str, metric: str, stat: str = "Sum") -> cw.GraphWidget:
            return cw.GraphWidget(
                title=title,
                width=12,
                left=[cw.MathExpression(
                    expression=(
                        f"SEARCH('{{{METRICS_NAMESPACE},UserId}} "
                        f"MetricName=\"{metric}\"', '{stat}')"
                    ),
                    using_metrics={},
                    label="",
                    period=Duration.minutes(5),
                )],
            )

        dashboard.add_widgets(
            search_widget("Estimated spend (USD) per user", "EstimatedCostUSD"),
            search_widget("Requests per user", "Requests"),
        )
        dashboard.add_widgets(
            search_widget("Quota throttles (429) per user", "Throttles"),
            cw.GraphWidget(
                title="Tokens (all users)",
                width=12,
                left=[
                    cw.Metric(namespace=METRICS_NAMESPACE, metric_name="InputTokens",
                              statistic="Sum", period=Duration.minutes(5)),
                    cw.Metric(namespace=METRICS_NAMESPACE, metric_name="OutputTokens",
                              statistic="Sum", period=Duration.minutes(5)),
                ],
            ),
        )

        # ------------------------------------------------------------------
        # Outputs
        # ------------------------------------------------------------------
        cdk.CfnOutput(self, "GatewayUrl", value=fn_url.url,
                      description="Set this (plus /v1) as base_url in OpenAI/Anthropic SDKs")
        cdk.CfnOutput(self, "AdminKeySecretArn", value=admin_secret.secret_arn)
        cdk.CfnOutput(self, "UsersTableName", value=users_table.table_name)
        cdk.CfnOutput(self, "UsageTableName", value=usage_table.table_name)
        cdk.CfnOutput(self, "AlertTopicArn", value=alert_topic.topic_arn)
        cdk.CfnOutput(self, "BedrockUserRoleArn", value=bedrock_user_role.role_arn,
                      description="Role the broker vends to users for native Bedrock calls.")
        cdk.CfnOutput(self, "InvocationLogGroup", value=invocation_log_group.log_group_name,
                      description="Bedrock model-invocation logs used for per-user metering.")
        cdk.CfnOutput(self, "JwtIssuer", value=jwt_issuer,
                      description="OIDC issuer whose JWTs the gateway accepts")
        cdk.CfnOutput(self, "DenyDirectBedrockPolicyArn", value=deny_direct.managed_policy_arn,
                      description="Attach to non-gateway roles to prevent bypassing the gateway")
        cdk.CfnOutput(self, "GatewayRoleArn", value=gateway_fn.role.role_arn,
                      description="The only principal that should be allowed to invoke Bedrock directly")
        cdk.CfnOutput(
            self, "ModelPriceSnapshot",
            value=model_prices_json,
            description="Standard on-demand USD-per-MTok prices captured at stack deployment",
        )
        if user_pool is not None:
            cdk.CfnOutput(self, "DemoUserPoolId", value=user_pool.user_pool_id)
            cdk.CfnOutput(self, "DemoUserPoolClientId",
                          value=user_pool_client.user_pool_client_id,
                          description="App client for the demo notebook to obtain JWTs")
