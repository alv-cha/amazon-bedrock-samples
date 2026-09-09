"""CDK stack for runtime-only Amazon Bedrock per-user quotas.

Resources:
- DynamoDB: users table (limits/status per JWT subject), usage table (TTL)
- Broker/admin Lambda: FastAPI behind an AWS_IAM Lambda Function URL
- Short-lived STS role restricted to configured bedrock-runtime model ARNs
- JWT identity: bring your own OIDC issuer via ``-c jwt_issuer=...``
  (optionally ``-c jwt_audience=...``), or let the stack create a demo
  Cognito User Pool
- Admin key in Secrets Manager
- CloudWatch Logs subscription processor for event-driven metering + SNS
- CloudWatch dashboard over broker and metering EMF metrics
"""

import hashlib
import json
import os

import aws_cdk as cdk
from aws_cdk import (
    Duration,
    RemovalPolicy,
    Stack,
    aws_bedrock as bedrock,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as cloudfront_origins,
    aws_cloudwatch as cw,
    aws_cloudwatch_actions as cw_actions,
    aws_cognito as cognito,
    aws_cognito_identitypool as idpool,
    aws_dynamodb as ddb,
    aws_events as events,
    aws_events_targets as events_targets,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_lambda_event_sources as lambda_event_sources,
    aws_logs as logs,
    aws_logs_destinations as logs_destinations,
    aws_s3 as s3,
    aws_s3_deployment as s3deploy,
    aws_secretsmanager as sm,
    aws_sns as sns,
    aws_sns_subscriptions as subs,
    aws_sqs as sqs,
    aws_ssm as ssm,
    custom_resources as cr,
)
from constructs import Construct

from .configuration import DeploymentConfig

METRICS_NAMESPACE = "BedrockQuotaGateway"

# Tag key stamped on workload inference profiles (cost allocation + audit).
WORKLOAD_TAG_KEY = "bedrock-quota-workload"

# Cross-region inference-profile ID prefixes; must stay in sync with
# usage_processor/handler.py _PROFILE_PREFIXES.
_CR_PROFILE_PREFIXES = {
    "us", "eu", "apac", "jp", "au", "ca", "sa", "global", "us-gov",
}


def _model_source_arn(
    partition: str, region: str, account: str, model: str
) -> str:
    """ARN for CfnApplicationInferenceProfile.copy_from.

    Cross-region profile IDs (``us.anthropic...``) become account-scoped
    inference-profile ARNs; plain model IDs become foundation-model ARNs.
    """
    prefix = model.split(".", 1)[0]
    if prefix in _CR_PROFILE_PREFIXES:
        return (
            f"arn:{partition}:bedrock:{region}:{account}:"
            f"inference-profile/{model}"
        )
    return f"arn:{partition}:bedrock:{region}::foundation-model/{model}"


class QuotaGatewayStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        config = DeploymentConfig.from_node(self.node)
        if config.deprecated_options:
            cdk.Annotations.of(self).add_warning(
                "The runtime-only architecture ignores deprecated dual-mode "
                "options: " + ", ".join(config.deprecated_options) + ". "
                "Remove them from deployment configuration."
            )
        alert_email = config.alert_email
        jwt_issuer = config.jwt_issuer
        jwt_audience = config.jwt_audience
        jwt_jwks_url = config.jwt_jwks_url
        jwt_user_claim = config.jwt_user_claim
        vended_ttl_seconds = config.vended_ttl_seconds
        max_session_seconds = max(vended_ttl_seconds, 3600)
        use_snapstart = config.snapstart

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
                "CatalogModels": config.model_pricing.catalog_models,
                "PinnedPrices": config.model_pricing.price_overrides,
                "FallbackPrice": config.model_pricing.fallback_price,
            },
        )
        model_prices_json = price_snapshot.get_att_string("ModelPricesJson")
        fallback_price_json = price_snapshot.get_att_string("FallbackPriceJson")

        # Runtime price configuration. The deployment snapshot seeds the
        # parameter; a daily scheduled refresh keeps it aligned with the
        # Pricing API so catalog price changes do not require a redeploy.
        # Metering reads the parameter with a short cache and falls back to
        # the env snapshot if Parameter Store is unavailable.
        model_prices_parameter = ssm.StringParameter(
            self,
            "ModelPricesParameter",
            string_value=cdk.Fn.join(
                "",
                [
                    '{"models":',
                    model_prices_json,
                    ',"fallback":',
                    fallback_price_json,
                    "}",
                ],
            ),
            description=(
                "Bedrock model token prices used by quota metering; "
                "refreshed daily from the AWS Pricing API"
            ),
        )
        price_refresh_fn = lambda_.Function(
            self,
            "PriceRefreshFn",
            runtime=lambda_.Runtime.PYTHON_3_12,
            memory_size=256,
            timeout=Duration.minutes(1),
            handler="handler.scheduled_handler",
            code=lambda_.Code.from_asset("pricing_resolver"),
            environment={
                "PRICES_PARAMETER_NAME": (
                    model_prices_parameter.parameter_name
                ),
            },
        )
        price_refresh_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["pricing:GetProducts"],
                resources=["*"],
            )
        )
        model_prices_parameter.grant_write(price_refresh_fn)
        events.Rule(
            self,
            "ModelPriceRefreshSchedule",
            schedule=events.Schedule.rate(Duration.hours(24)),
            targets=[
                events_targets.LambdaFunction(
                    price_refresh_fn,
                    # Same properties shape as the deploy-time custom
                    # resource, so both entrypoints share one contract.
                    event=events.RuleTargetInput.from_object(
                        {
                            "RegionCode": self.region,
                            "CatalogModels": (
                                config.model_pricing.catalog_models
                            ),
                            "PinnedPrices": (
                                config.model_pricing.price_overrides
                            ),
                            "FallbackPrice": (
                                config.model_pricing.fallback_price
                            ),
                        }
                    ),
                )
            ],
        )

        # ------------------------------------------------------------------
        # Identity: BYO OIDC issuer, or a demo Cognito User Pool
        # ------------------------------------------------------------------
        user_pool = None
        ui_bucket = None
        ui_distribution = None
        cognito_domain_url = None
        if not jwt_issuer:
            user_pool = cognito.UserPool(
                self, "DemoUserPool",
                self_sign_up_enabled=False,
                sign_in_aliases=cognito.SignInAliases(username=True, email=True),
                removal_policy=RemovalPolicy.DESTROY,
            )

            # Build the UI origin before adding OAuth URLs to the existing app
            # client. The dependency direction is AppClient -> Distribution;
            # CloudFront must not reference the app client or Function URL.
            if config.admin_ui:
                domain_suffix = hashlib.sha256(
                    self.stack_name.encode("utf-8")
                ).hexdigest()[:10]
                cognito_domain_prefix = (
                    f"bedrock-quota-admin-{self.account}-{self.region}-"
                    f"{domain_suffix}"
                )
                ui_callback_url = None
                ui_logout_url = None
                ui_bucket = s3.Bucket(
                    self, "AdminUiBucket",
                    block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
                    encryption=s3.BucketEncryption.S3_MANAGED,
                    enforce_ssl=True,
                    removal_policy=RemovalPolicy.DESTROY,
                    auto_delete_objects=True,
                )
                ui_security_headers = cloudfront.ResponseHeadersPolicy(
                    self,
                    "AdminUiSecurityHeaders",
                    comment="Security headers for the Bedrock quota admin UI",
                    security_headers_behavior=cloudfront.ResponseSecurityHeadersBehavior(
                        content_security_policy=cloudfront.ResponseHeadersContentSecurityPolicy(
                            content_security_policy="; ".join(
                                [
                                    "default-src 'self'",
                                    "base-uri 'none'",
                                    "object-src 'none'",
                                    "frame-ancestors 'none'",
                                    "form-action 'self'",
                                    "img-src 'self' data:",
                                    (
                                        "connect-src 'self' "
                                        f"https://*.lambda-url.{self.region}.on.aws "
                                        f"https://cognito-identity.{self.region}.{self.url_suffix} "
                                        f"https://*.auth.{self.region}.amazoncognito.com "
                                        f"https://*.auth.{self.region}.amazoncognito.com.cn"
                                    ),
                                ]
                            ),
                            override=True,
                        ),
                        content_type_options=cloudfront.ResponseHeadersContentTypeOptions(
                            override=True
                        ),
                        frame_options=cloudfront.ResponseHeadersFrameOptions(
                            frame_option=cloudfront.HeadersFrameOption.DENY,
                            override=True,
                        ),
                        referrer_policy=cloudfront.ResponseHeadersReferrerPolicy(
                            referrer_policy=(
                                cloudfront.HeadersReferrerPolicy.STRICT_ORIGIN_WHEN_CROSS_ORIGIN
                            ),
                            override=True,
                        ),
                        strict_transport_security=cloudfront.ResponseHeadersStrictTransportSecurity(
                            access_control_max_age=Duration.days(365),
                            include_subdomains=True,
                            preload=True,
                            override=True,
                        ),
                    ),
                )
                ui_distribution = cloudfront.Distribution(
                    self, "AdminUiDistribution",
                    default_root_object="index.html",
                    default_behavior=cloudfront.BehaviorOptions(
                        origin=cloudfront_origins.S3BucketOrigin.with_origin_access_control(
                            ui_bucket
                        ),
                        viewer_protocol_policy=(
                            cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS
                        ),
                        response_headers_policy=ui_security_headers,
                    ),
                    # SPA: client-side routes resolve to index.html.
                    error_responses=[
                        cloudfront.ErrorResponse(
                            http_status=403,
                            response_http_status=200,
                            response_page_path="/index.html",
                        ),
                        cloudfront.ErrorResponse(
                            http_status=404,
                            response_http_status=200,
                            response_page_path="/index.html",
                        ),
                    ],
                )
                ui_origin = (
                    f"https://{ui_distribution.distribution_domain_name}"
                )
                ui_callback_url = f"{ui_origin}/auth/callback"
                ui_logout_url = f"{ui_origin}/"

            # Preserve this scope and construct ID: the notebook, broker JWT
            # audience, Identity Pool, and browser all reuse this public client.
            user_pool_client = user_pool.add_client(
                "DemoAppClient",
                auth_flows=cognito.AuthFlow(user_password=True, user_srp=True),
                generate_secret=False,
                id_token_validity=Duration.hours(12),
                disable_o_auth=not config.admin_ui,
                o_auth=(
                    cognito.OAuthSettings(
                        flows=cognito.OAuthFlows(
                            authorization_code_grant=True,
                            implicit_code_grant=False,
                        ),
                        scopes=[
                            cognito.OAuthScope.OPENID,
                            cognito.OAuthScope.EMAIL,
                            cognito.OAuthScope.PROFILE,
                        ],
                        callback_urls=[ui_callback_url],
                        default_redirect_uri=ui_callback_url,
                        logout_urls=[ui_logout_url],
                    )
                    if config.admin_ui
                    else None
                ),
            )
            if config.admin_ui:
                login_domain = user_pool.add_domain(
                    "AdminManagedLoginDomain",
                    cognito_domain=cognito.CognitoDomainOptions(
                        domain_prefix=cognito_domain_prefix
                    ),
                    managed_login_version=(
                        cognito.ManagedLoginVersion.NEWER_MANAGED_LOGIN
                    ),
                )
                cognito_domain_url = login_domain.base_url()
                managed_login_branding = cognito.CfnManagedLoginBranding(
                    self,
                    "AdminManagedLoginBranding",
                    user_pool_id=user_pool.user_pool_id,
                    client_id=user_pool_client.user_pool_client_id,
                    use_cognito_provided_values=True,
                )
                domain_resource = login_domain.node.default_child
                if not isinstance(domain_resource, cognito.CfnUserPoolDomain):
                    raise TypeError(
                        "Managed login domain has no AWS::Cognito::UserPoolDomain child"
                    )
                managed_login_branding.add_dependency(domain_resource)

            jwt_issuer = (
                f"https://cognito-idp.{self.region}.{self.url_suffix}/"
                f"{user_pool.user_pool_id}"
            )
            # ID tokens carry the app client id as `aud`.
            jwt_audience = user_pool_client.user_pool_client_id
            if config.admin_jwt_claim == "cognito:groups":
                group_parameters = {
                    "GroupName": config.admin_jwt_value,
                    "UserPoolId": user_pool.user_pool_id,
                    "Description": "Administrators of the Bedrock quota demo UI",
                }
                ensure_admin_group = cr.AwsCustomResource(
                    self,
                    "EnsureDemoAdminGroup",
                    on_create=cr.AwsSdkCall(
                        service="CognitoIdentityServiceProvider",
                        action="createGroup",
                        parameters=group_parameters,
                        physical_resource_id=cr.PhysicalResourceId.of(
                            "demo-admin-group"
                        ),
                        ignore_error_codes_matching="GroupExistsException",
                    ),
                    on_update=cr.AwsSdkCall(
                        service="CognitoIdentityServiceProvider",
                        action="createGroup",
                        parameters=group_parameters,
                        physical_resource_id=cr.PhysicalResourceId.of(
                            "demo-admin-group"
                        ),
                        ignore_error_codes_matching="GroupExistsException",
                    ),
                    policy=cr.AwsCustomResourcePolicy.from_statements(
                        [
                            iam.PolicyStatement(
                                actions=["cognito-idp:CreateGroup"],
                                resources=[user_pool.user_pool_arn],
                            )
                        ]
                    ),
                    install_latest_aws_sdk=False,
                )
                ensure_admin_group.node.add_dependency(user_pool)

        # ------------------------------------------------------------------
        # DynamoDB
        # ------------------------------------------------------------------
        table_removal_policy = (
            RemovalPolicy.RETAIN
            if config.retain_tables_on_delete
            else RemovalPolicy.DESTROY
        )
        users_table = ddb.Table(
            self, "UsersTable",
            partition_key=ddb.Attribute(name="user_id", type=ddb.AttributeType.STRING),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            # Emergency-stop state is always stream-driven. Revocation mode
            # reuses this stream for its separate status sentinels.
            stream=ddb.StreamViewType.NEW_AND_OLD_IMAGES,
            time_to_live_attribute="expires_at",
            removal_policy=table_removal_policy,
        )

        usage_table = ddb.Table(
            self, "UsageTable",
            partition_key=ddb.Attribute(name="user_id", type=ddb.AttributeType.STRING),
            sort_key=ddb.Attribute(name="window", type=ddb.AttributeType.STRING),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="expires_at",
            removal_policy=table_removal_policy,
        )

        admin_audit_table = ddb.Table(
            self,
            "AdminAuditTable",
            partition_key=ddb.Attribute(
                name="subject_id", type=ddb.AttributeType.STRING
            ),
            sort_key=ddb.Attribute(
                name="event_key", type=ddb.AttributeType.STRING
            ),
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="expires_at",
            removal_policy=table_removal_policy,
        )
        admin_audit_table.add_global_secondary_index(
            index_name="scope-event-key-index",
            partition_key=ddb.Attribute(
                name="scope", type=ddb.AttributeType.STRING
            ),
            sort_key=ddb.Attribute(
                name="event_key", type=ddb.AttributeType.STRING
            ),
            projection_type=ddb.ProjectionType.ALL,
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
        emergency_secret = sm.Secret(
            self,
            "EmergencyAdminKey",
            description=(
                "Break-glass key for role-wide Bedrock emergency stop"
            ),
            generate_secret_string=sm.SecretStringGenerator(
                exclude_punctuation=True, password_length=48,
            ),
        )

        # ------------------------------------------------------------------
        # Broker/admin Lambda (FastAPI + Lambda Web Adapter)
        # ------------------------------------------------------------------
        # AWS Lambda Web Adapter public layer (zip packaging). Name/version
        # per https://github.com/awslabs/aws-lambda-web-adapter — override
        # with -c adapter_layer_arn=... if a newer version ships.
        adapter_layer_arn = config.adapter_layer_arn or (
            f"arn:aws:lambda:{self.region}:753240598075:layer:LambdaAdapterLayerX86:28"
        )
        adapter_layer = lambda_.LayerVersion.from_layer_version_arn(
            self, "WebAdapterLayer", adapter_layer_arn,
        )

        broker_api_fn = lambda_.Function(
            # Keep the original construct ID so updating an existing
            # deployment does not replace the Lambda or its Function URL.
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
                "AWS_LWA_INVOKE_MODE": "buffered",
                "PORT": "8080",
                # App config
                "USERS_TABLE": users_table.table_name,
                "USAGE_TABLE": usage_table.table_name,
                "ADMIN_AUDIT_TABLE": admin_audit_table.table_name,
                "ADMIN_AUDIT_RETENTION_DAYS": "365",
                "METRICS_NAMESPACE": METRICS_NAMESPACE,
                "ADMIN_KEY_SECRET_ARN": admin_secret.secret_arn,
                "EMERGENCY_KEY_SECRET_ARN": emergency_secret.secret_arn,
                "AUTO_PROVISION_USERS": str(config.auto_provision_users).lower(),
                "DEFAULT_DAILY_USD": str(config.default_daily_usd),
                "DEFAULT_DAILY_INPUT_TOKENS": str(
                    config.default_daily_input_tokens
                ),
                "DEFAULT_DAILY_OUTPUT_TOKENS": str(
                    config.default_daily_output_tokens
                ),
                # Workload roster for the admin API: granularity labeling
                # and enforcement_ready surfacing (static config, no tokens).
                "WORKLOAD_ENFORCEMENT_JSON": json.dumps(
                    {
                        workload.workload_id: {
                            "name": workload.name,
                            "enforcement_ready": bool(workload.role_arn),
                        }
                        for workload in config.workloads
                    },
                    sort_keys=True,
                ),
                "USAGE_RETENTION_DAYS": str(config.usage_retention_days),
                # Credential lifetime and refresh controls. The runtime keeps
                # legacy behavior unless the lease/revocation mode is selected.
                "CREDENTIAL_ENFORCEMENT_MODE": (
                    config.credential_enforcement_mode
                ),
                "PERMISSION_LEASE_SECONDS": str(
                    config.permission_lease_seconds
                ),
                "REFRESH_OVERLAP_SECONDS": str(
                    config.refresh_overlap_seconds
                ),
                "REFRESH_JITTER_SECONDS": str(
                    config.refresh_jitter_seconds
                ),
                "VEND_RATE_LIMIT_PER_MINUTE": str(
                    config.vend_rate_limit_per_minute
                ),
                "REVOCATION_POLICY_SHARDS": str(
                    config.revocation_policy_shards
                ),
                "REVOCATION_RECONCILE_MINUTES": str(
                    config.revocation_reconcile_minutes
                ),
                "REVOCATION_POLICY_MAX_CHARACTERS": "6144",
                "QUALIFICATION_STATUS_JSON": json.dumps(
                    {
                        "legacy": "baseline_existing_behavior",
                        "lease": "pending_live_sandbox_probe",
                        "revocation": (
                            "experimental_pending_propagation_isolation_probe"
                        ),
                        "emergency": (
                            "pending_live_activation_recovery_exercise"
                        ),
                    },
                    separators=(",", ":"),
                ),
                # JWT auth
                "JWT_ISSUER": jwt_issuer,
                "JWT_AUDIENCE": jwt_audience,
                "JWT_JWKS_URL": jwt_jwks_url,
                "JWT_USER_CLAIM": jwt_user_claim,
                # Admin-by-JWT (empty claim = shared key only)
                "ADMIN_JWT_CLAIM": config.admin_jwt_claim,
                "ADMIN_JWT_VALUE": config.admin_jwt_value,
            },
        )

        users_table.grant_read_write_data(broker_api_fn)
        usage_table.grant_read_data(broker_api_fn)
        admin_audit_table.grant_read_write_data(broker_api_fn)
        broker_api_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["dynamodb:TransactWriteItems"],
                resources=[
                    users_table.table_arn,
                    admin_audit_table.table_arn,
                ],
            )
        )
        admin_secret.grant_read(broker_api_fn)
        emergency_secret.grant_read(broker_api_fn)

        # ------------------------------------------------------------------
        # Per-user vended role
        #
        # The broker assumes this role on behalf of an in-budget user, with
        # RoleSessionName + SourceIdentity = a sanitized id derived from the
        # identity claim (reverse-mapped for metering). The user then calls the
        # bedrock-runtime endpoint directly with the short-lived credentials.
        # ------------------------------------------------------------------
        # A permissions boundary caps the vended role even when the optional
        # revocation worker can update attached deny-policy versions. The
        # worker cannot turn that write capability into IAM or wider-model
        # permissions because those actions/resources are absent here.
        bedrock_permissions_boundary = iam.ManagedPolicy(
            self,
            "BedrockUserPermissionsBoundary",
            description=(
                "Maximum permissions for sessions vended by the quota broker"
            ),
            statements=[
                iam.PolicyStatement(
                    effect=iam.Effect.ALLOW,
                    actions=[
                        "bedrock:CountTokens",
                        "bedrock:InvokeModel",
                        "bedrock:InvokeModelWithResponseStream",
                    ],
                    resources=list(config.allowed_model_arns),
                )
            ],
        )
        bedrock_user_role = iam.Role(
            self, "BedrockUserRole",
            # Only the broker Lambda role may assume this. The broker assigns
            # SourceIdentity and the quota-user session tag; users never hold
            # static Bedrock access.
            assumed_by=iam.ArnPrincipal(broker_api_fn.role.role_arn),
            permissions_boundary=bedrock_permissions_boundary,
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
                principals=[iam.ArnPrincipal(broker_api_fn.role.role_arn)],
                actions=["sts:SetSourceIdentity", "sts:TagSession"],
            )
        )
        bedrock_user_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "bedrock:CountTokens",
                    "bedrock:InvokeModel",
                    "bedrock:InvokeModelWithResponseStream",
                ],
                # These IAM actions also authorize Converse and ConverseStream.
                # Bearer-token access is intentionally not granted because
                # bedrock:CallWithBearerToken is resource "*", which would
                # weaken this model/inference-profile allowlist.
                resources=list(config.allowed_model_arns),
            )
        )
        emergency_deny_policy = iam.ManagedPolicy(
            self,
            "EmergencyBedrockDenyPolicy",
            description=(
                "Operator-controlled role-wide Bedrock emergency stop"
            ),
            statements=[
                iam.PolicyStatement(
                    effect=iam.Effect.DENY,
                    actions=[
                        "bedrock:CountTokens",
                        "bedrock:InvokeModel",
                        "bedrock:InvokeModelWithResponseStream",
                    ],
                    resources=["*"],
                    # No-op until the emergency processor replaces this
                    # version with an unconditional deny.
                    conditions={
                        "StringEquals": {
                            "aws:SourceIdentity": [
                                "__emergency_stop_inactive__"
                            ]
                        }
                    },
                )
            ],
            roles=[bedrock_user_role],
        )
        # Let the gateway role assume the vended role AND stamp the per-user
        # identity/tag. SetSourceIdentity + TagSession must be granted on the
        # *caller* side too, not just allowed by the trust policy.
        broker_api_fn.role.add_to_principal_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["sts:AssumeRole", "sts:SetSourceIdentity", "sts:TagSession"],
                resources=[bedrock_user_role.role_arn],
            )
        )
        broker_api_fn.add_environment(
            "BEDROCK_USER_ROLE_ARN", bedrock_user_role.role_arn
        )
        broker_api_fn.add_environment(
            "VENDED_CREDENTIAL_TTL_SECONDS", str(vended_ttl_seconds)
        )

        # ------------------------------------------------------------------
        # Bedrock model-invocation logging -> CloudWatch Logs.
        #
        # This is the source of per-call token counts across every bedrock-
        # runtime invoke path (InvokeModel/Converse/streaming, all providers);
        # each record's identity.arn carries the RoleSessionName (= sanitized
        # JWT claim) so the subscription processor can meter per user.
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
        manage_logging = config.manage_invocation_logging
        existing_log_group_name = config.invocation_log_group_name

        if existing_log_group_name:
            # Bring-your-own group: never mutate the account-wide setting.
            invocation_log_group = logs.LogGroup.from_log_group_name(
                self, "BedrockInvocationLogs", existing_log_group_name
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
                # Preserve the existing alias logical ID on stack updates.
                self, "GatewayLiveAlias",
                alias_name="live",
                version=broker_api_fn.current_version,
            )
        else:
            url_target = broker_api_fn

        # The Function URL enforces IAM (SigV4) auth: callers must be
        # signed AWS principals, so the URL is not anonymously reachable
        # (required by the Palisade "world accessible Lambda" slat —
        # AuthType NONE is a Sev-2 finding). The end-user's JWT still rides
        # in the Authorization/x-api-key header and drives the per-user
        # quota: SigV4 at the edge (who may call the gateway) + JWT in the
        # app (which user is spending). Defense in depth.
        fn_url = url_target.add_function_url(
            auth_type=lambda_.FunctionUrlAuthType.AWS_IAM,
            invoke_mode=lambda_.InvokeMode.BUFFERED,
        )

        # Principals allowed to invoke the URL (your app-server/backend
        # roles, or your own role for the demo). Grant via
        # -c invoker_principal_arns=arn1,arn2 ; defaults to this account's
        # root so any IAM principal in the account can be granted normally.
        if config.invoker_principal_arns:
            for arn in config.invoker_principal_arns:
                fn_url.grant_invoke_url(iam.ArnPrincipal(arn))
        else:
            fn_url.grant_invoke_url(iam.AccountRootPrincipal())

        # ------------------------------------------------------------------
        # Admin UI (opt-in: -c admin_ui=true). Static React on S3 + CloudFront,
        # authenticated with the demo Cognito pool via a Cognito Identity Pool.
        # The browser gets temporary AWS creds from the Identity Pool's
        # authenticated role and SigV4-signs its calls to the AWS_IAM Function
        # URL — no admin secret ever reaches the browser (admin-by-JWT does the
        # /admin authorization; see ADMIN_JWT_CLAIM). Only wired when the stack
        # created the demo pool; with a BYO issuer, see DEPLOYMENT.md for the
        # manual Identity Pool + OIDC-provider path.
        # ------------------------------------------------------------------
        if config.admin_ui and user_pool is not None:
            if ui_bucket is None or ui_distribution is None or cognito_domain_url is None:
                raise RuntimeError("Admin UI resources were not initialized")
            # The browser calls the IAM-authenticated Function URL from the
            # CloudFront origin. Configure CORS at the Function URL so Lambda
            # handles unsigned preflight requests before FastAPI, and restrict
            # it to this distribution rather than allowing every website.
            cfn_function_url = fn_url.node.default_child
            if not isinstance(cfn_function_url, lambda_.CfnUrl):
                raise TypeError("Function URL has no AWS::Lambda::Url child")
            cfn_function_url.cors = lambda_.CfnUrl.CorsProperty(
                allow_credentials=False,
                allow_headers=[
                    "authorization",
                    "content-type",
                    "idempotency-key",
                    "if-match",
                    "x-amz-content-sha256",
                    "x-amz-date",
                    "x-amz-security-token",
                    "x-quota-user-token",
                ],
                allow_methods=["GET", "POST", "PUT"],
                allow_origins=[
                    f"https://{ui_distribution.distribution_domain_name}"
                ],
                expose_headers=[
                    "etag",
                    "x-quota-limit-usd",
                    "x-quota-window",
                    "x-request-id",
                ],
                max_age=3600,
            )
            admin_identity_pool = idpool.IdentityPool(
                self, "AdminIdentityPool",
                allow_unauthenticated_identities=False,
                authentication_providers=idpool.IdentityPoolAuthenticationProviders(
                    user_pools=[idpool.UserPoolAuthenticationProvider(
                        user_pool=user_pool,
                        user_pool_client=user_pool_client,
                    )],
                ),
            )
            # The authenticated browser identity may invoke the Function URL;
            # the JWT it presents (admin group) authorizes the /admin routes.
            fn_url.grant_invoke_url(admin_identity_pool.authenticated_role)
            # dist/ is a generated Vite bundle (gitignored), resolved relative
            # to this file so it works regardless of the synth CWD. Fail with an
            # actionable message rather than a cryptic asset error if it is
            # missing — the operator must build the SPA before deploying.
            ui_dist = os.path.join(
                os.path.dirname(__file__), "..", "..", "admin-ui", "dist"
            )
            if not os.path.isdir(ui_dist):
                raise FileNotFoundError(
                    "admin_ui=true but admin-ui/dist is missing. Run "
                    "'npm install && npm run build' in admin-ui/ before "
                    "'cdk deploy -c admin_ui=true'."
                )
            ui_deployment = s3deploy.BucketDeployment(
                self, "AdminUiDeployment",
                sources=[s3deploy.Source.asset(ui_dist)],
                destination_bucket=ui_bucket,
                distribution=ui_distribution,
                distribution_paths=["/*"],
                # config.js is deployment-specific and is written by the
                # custom resource below. Excluding it from sync also prevents
                # later UI asset updates from pruning or overwriting it.
                exclude=["config.js"],
            )
            # Write deployment-specific public identifiers after the static
            # bundle. No secret is included; the browser still obtains
            # temporary AWS credentials from the Identity Pool and an ID token
            # from Cognito. Using a custom resource lets CloudFormation resolve
            # generated IDs instead of baking unresolved CDK tokens at synth.
            ui_config_body = self.to_json_string(
                {
                    "gatewayUrl": fn_url.url,
                    "region": self.region,
                    "userPoolId": user_pool.user_pool_id,
                    "userPoolClientId": user_pool_client.user_pool_client_id,
                    "identityPoolId": admin_identity_pool.identity_pool_id,
                    "cognitoDomain": cognito_domain_url,
                    "cognitoIssuer": jwt_issuer,
                }
            )
            ui_config_writer = cr.AwsCustomResource(
                self,
                "AdminUiRuntimeConfig",
                on_create=cr.AwsSdkCall(
                    service="S3",
                    action="putObject",
                    parameters={
                        "Bucket": ui_bucket.bucket_name,
                        "Key": "config.js",
                        "Body": cdk.Fn.join(
                            "",
                            [
                                "window.QUOTA_ADMIN_CONFIG = ",
                                ui_config_body,
                                ";\n",
                            ],
                        ),
                        "ContentType": "application/javascript",
                        "CacheControl": "no-store",
                    },
                    physical_resource_id=cr.PhysicalResourceId.of(
                        "admin-ui-runtime-config"
                    ),
                ),
                on_update=cr.AwsSdkCall(
                    service="S3",
                    action="putObject",
                    parameters={
                        "Bucket": ui_bucket.bucket_name,
                        "Key": "config.js",
                        "Body": cdk.Fn.join(
                            "",
                            [
                                "window.QUOTA_ADMIN_CONFIG = ",
                                ui_config_body,
                                ";\n",
                            ],
                        ),
                        "ContentType": "application/javascript",
                        "CacheControl": "no-store",
                    },
                    physical_resource_id=cr.PhysicalResourceId.of(
                        "admin-ui-runtime-config"
                    ),
                ),
                policy=cr.AwsCustomResourcePolicy.from_statements(
                    [
                        iam.PolicyStatement(
                            actions=["s3:PutObject"],
                            resources=[ui_bucket.arn_for_objects("config.js")],
                        )
                    ]
                ),
                install_latest_aws_sdk=False,
            )
            ui_config_writer.node.add_dependency(ui_deployment)
            cdk.CfnOutput(self, "AdminUiUrl",
                          value=f"https://{ui_distribution.distribution_domain_name}",
                          description="Admin console (CloudFront). Sign in with the demo Cognito pool.")
            cdk.CfnOutput(self, "AdminIdentityPoolId",
                          value=admin_identity_pool.identity_pool_id,
                          description="Cognito Identity Pool the admin UI exchanges tokens with.")

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
                        "bedrock:CallWithBearerToken",
                        "bedrock:InvokeModel",
                        "bedrock:InvokeModelWithResponseStream",
                    ],
                    resources=["*"],
                ),
            ],
        )

        # ------------------------------------------------------------------
        # Event-driven usage processor + alerting
        # ------------------------------------------------------------------
        alert_topic = sns.Topic(self, "QuotaAlerts", display_name="Bedrock quota gateway alerts")
        if alert_email:
            alert_topic.add_subscription(subs.EmailSubscription(alert_email))

        # ------------------------------------------------------------------
        # Workload mode: one application inference profile per directly-
        # invoking app. The profile ARN in invocation-log records is the
        # attribution key (verified empirically: the log ``modelId`` field
        # preserves the application-inference-profile ARN). With a role ARN
        # the invoke policy is attached directly (paved road); without one
        # the policy document is emitted as an output for the customer to
        # attach, and the workload is metered but not hard-enforced.
        # ------------------------------------------------------------------
        workload_profiles: dict[str, dict[str, object]] = {}
        workload_enforcement: dict[str, dict[str, object]] = {}
        workload_role_arns: list[str] = []
        for workload in config.workloads:
            logical = "".join(
                part.capitalize() for part in workload.name.split("-")
            )
            profile = bedrock.CfnApplicationInferenceProfile(
                self,
                f"WorkloadProfile{logical}",
                inference_profile_name=f"bedrock-quota-{workload.name}",
                # CloudFormation restricts descriptions to
                # ^([0-9a-zA-Z:.][ _-]?)+$ -- letters, digits, colons, dots.
                description=(
                    f"Quota-gateway workload {workload.name}: "
                    "cost attribution and budget enforcement"
                ),
                model_source=(
                    bedrock.CfnApplicationInferenceProfile
                    .InferenceProfileModelSourceProperty(
                        copy_from=_model_source_arn(
                            self.partition,
                            self.region,
                            self.account,
                            workload.model,
                        )
                    )
                ),
                tags=[
                    cdk.CfnTag(
                        key=WORKLOAD_TAG_KEY, value=workload.name
                    )
                ],
            )
            profile_arn = profile.attr_inference_profile_arn
            invoke_statements = [
                iam.PolicyStatement(
                    sid="InvokeOwnQuotaProfile",
                    actions=[
                        "bedrock:InvokeModel",
                        "bedrock:InvokeModelWithResponseStream",
                    ],
                    resources=[profile_arn],
                ),
                iam.PolicyStatement(
                    sid="InvokeRoutedModelsViaQuotaProfileOnly",
                    actions=[
                        "bedrock:InvokeModel",
                        "bedrock:InvokeModelWithResponseStream",
                    ],
                    resources=[
                        f"arn:{self.partition}:bedrock:*::foundation-model/*",
                        (
                            f"arn:{self.partition}:bedrock:*:{self.account}:"
                            "inference-profile/*"
                        ),
                    ],
                    conditions={
                        "StringEquals": {
                            "bedrock:InferenceProfileArn": profile_arn
                        }
                    },
                ),
            ]
            if workload.role_arn:
                workload_role = iam.Role.from_role_arn(
                    self,
                    f"WorkloadRole{logical}",
                    workload.role_arn,
                    mutable=True,
                )
                iam.Policy(
                    self,
                    f"WorkloadInvokePolicy{logical}",
                    policy_name=f"bedrock-quota-workload-{workload.name}",
                    statements=invoke_statements,
                    roles=[workload_role],
                )
                workload_role_arns.append(workload.role_arn)
            else:
                cdk.CfnOutput(
                    self,
                    f"WorkloadPolicySnippet{logical}",
                    description=(
                        f"Attach to workload '{workload.name}' IAM role to "
                        "restrict it to its inference profile (enforcement "
                        "requires role_arn in the workloads config)"
                    ),
                    value=cdk.Fn.to_json_string(
                        {
                            "Version": "2012-10-17",
                            "Statement": [
                                statement.to_json()
                                for statement in invoke_statements
                            ],
                        }
                    ),
                )
            cdk.CfnOutput(
                self,
                f"WorkloadProfileArn{logical}",
                description=(
                    f"Application inference profile for workload "
                    f"'{workload.name}' (invoke with this as modelId)"
                ),
                value=profile_arn,
            )
            workload_profiles[workload.name] = {
                "workload_id": workload.workload_id,
                "profile_arn": profile_arn,
                "model": workload.model,
            }
            workload_enforcement[workload.workload_id] = {
                "name": workload.name,
                "profile_arn": profile_arn,
                "role_arn": workload.role_arn,
            }

        usage_processor_fn = lambda_.Function(
            self, "UsageProcessorFn",
            runtime=lambda_.Runtime.PYTHON_3_12,
            memory_size=256,
            timeout=Duration.minutes(2),
            handler="handler.handler",
            code=lambda_.Code.from_asset("../usage_processor"),
            environment={
                "USERS_TABLE": users_table.table_name,
                "USAGE_TABLE": usage_table.table_name,
                "SNS_TOPIC_ARN": alert_topic.topic_arn,
                "WARN_THRESHOLD": str(config.warn_threshold),
                "USAGE_RETENTION_DAYS": str(config.usage_retention_days),
                "METRICS_NAMESPACE": METRICS_NAMESPACE,
                "MODEL_PRICES_JSON": model_prices_json,
                "MODEL_FALLBACK_PRICE_JSON": fallback_price_json,
                "PRICES_PARAMETER_NAME": (
                    model_prices_parameter.parameter_name
                ),
                "BEDROCK_USER_ROLE_NAME": bedrock_user_role.role_name,
                "WORKLOAD_PROFILES_JSON": (
                    cdk.Fn.to_json_string(workload_profiles)
                    if workload_profiles
                    else "{}"
                ),
                "DEFAULT_DAILY_USD": str(config.default_daily_usd),
                "DEFAULT_DAILY_INPUT_TOKENS": str(
                    config.default_daily_input_tokens
                ),
                "DEFAULT_DAILY_OUTPUT_TOKENS": str(
                    config.default_daily_output_tokens
                ),
            },
        )
        users_table.grant_read_write_data(usage_processor_fn)
        usage_table.grant_read_write_data(usage_processor_fn)
        alert_topic.grant_publish(usage_processor_fn)
        model_prices_parameter.grant_read(usage_processor_fn)

        # CloudWatch Logs subscriptions are at-least-once. The processor uses
        # the Bedrock requestId as a DynamoDB idempotency key and updates the
        # daily aggregate in the same transaction.
        logs.SubscriptionFilter(
            self, "InvocationUsageSubscription",
            log_group=invocation_log_group,
            destination=logs_destinations.LambdaDestination(
                usage_processor_fn
            ),
            filter_pattern=logs.FilterPattern.string_value(
                "$.identity.arn",
                "=",
                f"*assumed-role/{bedrock_user_role.role_name}/*",
            ),
        )
        if config.workloads:
            # Workload traffic authenticates with the customer's own
            # principal, so it never matches the vended-role subscription
            # above. Attribution key: the invocation-log ``modelId`` field
            # preserves the application-inference-profile ARN. The processor
            # drops profiles it does not manage.
            logs.SubscriptionFilter(
                self, "WorkloadUsageSubscription",
                log_group=invocation_log_group,
                destination=logs_destinations.LambdaDestination(
                    usage_processor_fn
                ),
                filter_pattern=logs.FilterPattern.string_value(
                    "$.modelId",
                    "=",
                    "*:application-inference-profile/*",
                ),
            )

        # ------------------------------------------------------------------
        # Operator-confirmed emergency stop. The admin API closes the strongly
        # consistent vending gate first; this worker then applies/removes the
        # shared-role deny and marks the control state stable.
        # ------------------------------------------------------------------
        operations_alarms: dict[str, cw.Alarm] = {}
        emergency_dlq = sqs.Queue(
            self,
            "EmergencyStopDeadLetterQueue",
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            retention_period=Duration.days(14),
        )
        emergency_fn = lambda_.Function(
            self,
            "EmergencyStopProcessorFn",
            runtime=lambda_.Runtime.PYTHON_3_12,
            memory_size=256,
            timeout=Duration.minutes(2),
            reserved_concurrent_executions=1,
            handler="handler.handler",
            code=lambda_.Code.from_asset("../emergency_processor"),
            environment={
                "USERS_TABLE": users_table.table_name,
                "SNS_TOPIC_ARN": alert_topic.topic_arn,
                "METRICS_NAMESPACE": METRICS_NAMESPACE,
                "EMERGENCY_POLICY_ARN": (
                    emergency_deny_policy.managed_policy_arn
                ),
            },
        )
        emergency_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["dynamodb:GetItem", "dynamodb:UpdateItem"],
                resources=[users_table.table_arn],
                conditions={
                    "ForAllValues:StringEquals": {
                        "dynamodb:LeadingKeys": [
                            "CONFIG#EMERGENCY_STOP"
                        ]
                    }
                },
            )
        )
        alert_topic.grant_publish(emergency_fn)
        emergency_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "iam:GetPolicy",
                    "iam:GetPolicyVersion",
                    "iam:ListPolicyVersions",
                    "iam:CreatePolicyVersion",
                    "iam:DeletePolicyVersion",
                ],
                resources=[emergency_deny_policy.managed_policy_arn],
            )
        )
        emergency_fn.add_event_source(
            lambda_event_sources.DynamoEventSource(
                users_table,
                starting_position=lambda_.StartingPosition.LATEST,
                batch_size=10,
                max_batching_window=Duration.seconds(1),
                bisect_batch_on_error=True,
                retry_attempts=10,
                on_failure=lambda_event_sources.SqsDlq(emergency_dlq),
                filters=[
                    lambda_.FilterCriteria.filter(
                        {
                            "dynamodb": {
                                "Keys": {
                                    "user_id": {
                                        "S": ["CONFIG#EMERGENCY_STOP"]
                                    }
                                }
                            }
                        }
                    )
                ],
            )
        )
        events.Rule(
            self,
            "EmergencyStopReconciliationSchedule",
            schedule=events.Schedule.rate(Duration.minutes(1)),
            targets=[
                events_targets.LambdaFunction(
                    emergency_fn,
                    event=events.RuleTargetInput.from_object(
                        {"source": "aws.events"}
                    ),
                )
            ],
        )
        emergency_failure_alarm = cw.Alarm(
            self,
            "EmergencyStopFailureAlarm",
            metric=cw.Metric(
                namespace=METRICS_NAMESPACE,
                metric_name="EmergencyStopFailure",
                statistic="Sum",
                period=Duration.minutes(5),
            ),
            threshold=1,
            evaluation_periods=1,
        )
        emergency_failure_alarm.add_alarm_action(
            cw_actions.SnsAction(alert_topic)
        )
        operations_alarms["emergency_failure"] = emergency_failure_alarm
        emergency_dlq_alarm = cw.Alarm(
            self,
            "EmergencyStopDlqAlarm",
            metric=emergency_dlq.metric_approximate_number_of_messages_visible(
                period=Duration.minutes(5)
            ),
            threshold=1,
            evaluation_periods=1,
        )
        emergency_dlq_alarm.add_alarm_action(cw_actions.SnsAction(alert_topic))
        operations_alarms["emergency_dlq"] = emergency_dlq_alarm

        # ------------------------------------------------------------------
        # Optional active-session revocation. IAM updates are isolated from
        # usage accounting and serialized at concurrency one. This path stays
        # opt-in until the non-production propagation probe qualifies it.
        # ------------------------------------------------------------------
        revocation_policies: list[iam.ManagedPolicy] = []
        if config.credential_enforcement_mode == "revocation":
            no_blocked_identity = "__no_blocked_quota_identity__"
            for index in range(config.revocation_policy_shards):
                policy = iam.ManagedPolicy(
                    self,
                    f"QuotaRevocationPolicy{index}",
                    description=(
                        "Dynamic SourceIdentity deny shard for Bedrock quota "
                        "sessions"
                    ),
                    statements=[
                        iam.PolicyStatement(
                            effect=iam.Effect.DENY,
                            actions=[
                                "bedrock:CountTokens",
                                "bedrock:InvokeModel",
                                "bedrock:InvokeModelWithResponseStream",
                            ],
                            resources=["*"],
                            conditions={
                                "StringEquals": {
                                    "aws:SourceIdentity": [
                                        no_blocked_identity
                                    ]
                                }
                            },
                        )
                    ],
                    roles=[bedrock_user_role],
                )
                revocation_policies.append(policy)

            revocation_dlq = sqs.Queue(
                self,
                "RevocationDeadLetterQueue",
                encryption=sqs.QueueEncryption.SQS_MANAGED,
                retention_period=Duration.days(14),
            )
            revocation_fn = lambda_.Function(
                self,
                "RevocationProcessorFn",
                runtime=lambda_.Runtime.PYTHON_3_12,
                memory_size=256,
                timeout=Duration.minutes(2),
                reserved_concurrent_executions=1,
                handler="handler.handler",
                code=lambda_.Code.from_asset("../revocation_processor"),
                environment={
                    "USERS_TABLE": users_table.table_name,
                    "SNS_TOPIC_ARN": alert_topic.topic_arn,
                    "METRICS_NAMESPACE": METRICS_NAMESPACE,
                    "REVOCATION_POLICY_ARNS_JSON": cdk.Fn.to_json_string(
                        [
                            policy.managed_policy_arn
                            for policy in revocation_policies
                        ]
                    ),
                    "REVOCATION_POLICY_MAX_CHARACTERS": "6144",
                },
            )
            users_table.grant_read_data(revocation_fn)
            alert_topic.grant_publish(revocation_fn)
            revocation_fn.add_to_role_policy(
                iam.PolicyStatement(
                    actions=[
                        "iam:GetPolicy",
                        "iam:GetPolicyVersion",
                        "iam:ListPolicyVersions",
                        "iam:CreatePolicyVersion",
                        "iam:DeletePolicyVersion",
                    ],
                    resources=[
                        policy.managed_policy_arn
                        for policy in revocation_policies
                    ],
                )
            )
            revocation_fn.add_event_source(
                lambda_event_sources.DynamoEventSource(
                    users_table,
                    starting_position=lambda_.StartingPosition.LATEST,
                    batch_size=100,
                    max_batching_window=Duration.seconds(5),
                    bisect_batch_on_error=True,
                    retry_attempts=10,
                    on_failure=lambda_event_sources.SqsDlq(
                        revocation_dlq
                    ),
                    filters=[
                        lambda_.FilterCriteria.filter(
                            {
                                "dynamodb": {
                                    "Keys": {
                                        "user_id": {
                                            "S": [
                                                {"prefix": "REVOCATION#"}
                                            ]
                                        }
                                    }
                                }
                            }
                        )
                    ],
                )
            )
            events.Rule(
                self,
                "RevocationReconciliationSchedule",
                schedule=events.Schedule.rate(
                    Duration.minutes(
                        config.revocation_reconcile_minutes
                    )
                ),
                targets=[
                    events_targets.LambdaFunction(
                        revocation_fn,
                        event=events.RuleTargetInput.from_object(
                            {"source": "aws.events"}
                        ),
                    )
                ],
            )
            revocation_failure_alarm = cw.Alarm(
                self,
                "RevocationSyncFailureAlarm",
                metric=cw.Metric(
                    namespace=METRICS_NAMESPACE,
                    metric_name="RevocationSyncFailure",
                    statistic="Sum",
                    period=Duration.minutes(5),
                ),
                threshold=1,
                evaluation_periods=1,
            )
            revocation_failure_alarm.add_alarm_action(
                cw_actions.SnsAction(alert_topic)
            )
            operations_alarms["revocation_failure"] = (
                revocation_failure_alarm
            )
            revocation_overflow_alarm = cw.Alarm(
                self,
                "RevocationPolicyOverflowAlarm",
                metric=cw.Metric(
                    namespace=METRICS_NAMESPACE,
                    metric_name="RevocationPolicyOverflow",
                    statistic="Sum",
                    period=Duration.minutes(5),
                ),
                threshold=1,
                evaluation_periods=1,
            )
            revocation_overflow_alarm.add_alarm_action(
                cw_actions.SnsAction(alert_topic)
            )
            operations_alarms["revocation_overflow"] = (
                revocation_overflow_alarm
            )
            revocation_dlq_alarm = cw.Alarm(
                self,
                "RevocationDlqAlarm",
                metric=revocation_dlq.metric_approximate_number_of_messages_visible(
                    period=Duration.minutes(5)
                ),
                threshold=1,
                evaluation_periods=1,
            )
            revocation_dlq_alarm.add_alarm_action(
                cw_actions.SnsAction(alert_topic)
            )
            operations_alarms["revocation_dlq"] = revocation_dlq_alarm
            revocation_iterator_age_alarm = cw.Alarm(
                self,
                "RevocationIteratorAgeAlarm",
                metric=revocation_fn.metric(
                    "IteratorAge",
                    statistic="Maximum",
                    period=Duration.minutes(5),
                ),
                threshold=300_000,
                evaluation_periods=1,
            )
            revocation_iterator_age_alarm.add_alarm_action(
                cw_actions.SnsAction(alert_topic)
            )
            operations_alarms["revocation_iterator_age"] = (
                revocation_iterator_age_alarm
            )

        # ------------------------------------------------------------------
        # Workload enforcement: converge each workload row's status onto its
        # IAM principal. Blocked => attach an inline Deny on the workload
        # role; active => remove it. Fast path is the users-table stream
        # (status transitions written by the metering processor); the
        # schedule repairs drift. PutRolePolicy is an idempotent upsert and
        # DeleteRolePolicy tolerates absence, so repeats are safe.
        # ------------------------------------------------------------------
        if config.workloads:
            workload_dlq = sqs.Queue(
                self,
                "WorkloadEnforcementDeadLetterQueue",
                encryption=sqs.QueueEncryption.SQS_MANAGED,
                retention_period=Duration.days(14),
            )
            workload_enforcer_fn = lambda_.Function(
                self,
                "WorkloadEnforcerFn",
                runtime=lambda_.Runtime.PYTHON_3_12,
                memory_size=256,
                timeout=Duration.minutes(2),
                reserved_concurrent_executions=1,
                handler="handler.handler",
                code=lambda_.Code.from_asset("../workload_enforcer"),
                environment={
                    "USERS_TABLE": users_table.table_name,
                    "USAGE_TABLE": usage_table.table_name,
                    "SNS_TOPIC_ARN": alert_topic.topic_arn,
                    "METRICS_NAMESPACE": METRICS_NAMESPACE,
                    "WORKLOADS_JSON": cdk.Fn.to_json_string(
                        workload_enforcement
                    ),
                    "DENY_POLICY_NAME": "bedrock-quota-workload-deny",
                },
            )
            users_table.grant_read_write_data(workload_enforcer_fn)
            usage_table.grant_read_data(workload_enforcer_fn)
            alert_topic.grant_publish(workload_enforcer_fn)
            if workload_role_arns:
                workload_enforcer_fn.add_to_role_policy(
                    iam.PolicyStatement(
                        actions=[
                            "iam:PutRolePolicy",
                            "iam:DeleteRolePolicy",
                            "iam:GetRolePolicy",
                        ],
                        resources=sorted(set(workload_role_arns)),
                    )
                )
            workload_enforcer_fn.add_event_source(
                lambda_event_sources.DynamoEventSource(
                    users_table,
                    starting_position=lambda_.StartingPosition.LATEST,
                    batch_size=100,
                    max_batching_window=Duration.seconds(5),
                    bisect_batch_on_error=True,
                    retry_attempts=10,
                    on_failure=lambda_event_sources.SqsDlq(workload_dlq),
                    filters=[
                        lambda_.FilterCriteria.filter(
                            {
                                "dynamodb": {
                                    "Keys": {
                                        "user_id": {
                                            "S": [
                                                {"prefix": "workload:"}
                                            ]
                                        }
                                    }
                                }
                            }
                        )
                    ],
                )
            )
            events.Rule(
                self,
                "WorkloadEnforcementSchedule",
                schedule=events.Schedule.rate(Duration.minutes(5)),
                targets=[
                    events_targets.LambdaFunction(
                        workload_enforcer_fn,
                        event=events.RuleTargetInput.from_object(
                            {"source": "aws.events"}
                        ),
                    )
                ],
            )
            workload_failure_alarm = cw.Alarm(
                self,
                "WorkloadEnforcementFailureAlarm",
                metric=cw.Metric(
                    namespace=METRICS_NAMESPACE,
                    metric_name="WorkloadEnforcementFailure",
                    statistic="Sum",
                    period=Duration.minutes(5),
                ),
                threshold=1,
                evaluation_periods=1,
                treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
            )
            workload_failure_alarm.add_alarm_action(
                cw_actions.SnsAction(alert_topic)
            )
            operations_alarms["workload_enforcement_failure"] = (
                workload_failure_alarm
            )
            workload_dlq_alarm = cw.Alarm(
                self,
                "WorkloadEnforcementDlqAlarm",
                metric=workload_dlq.metric_approximate_number_of_messages_visible(
                    period=Duration.minutes(5)
                ),
                threshold=1,
                evaluation_periods=1,
            )
            workload_dlq_alarm.add_alarm_action(
                cw_actions.SnsAction(alert_topic)
            )
            operations_alarms["workload_enforcement_dlq"] = (
                workload_dlq_alarm
            )

        # A fallback-priced request means an invocation was metered with the
        # synthetic conservative rate instead of a resolved model price.
        # That is an operational event (missing snapshot/profile mapping),
        # never silent tarification: see the 2026-09-01 Opus incident.
        pricing_fallback_alarm = cw.Alarm(
            self,
            "PricingFallbackAlarm",
            metric=cw.Metric(
                namespace=METRICS_NAMESPACE,
                metric_name="FallbackPricedRequests",
                statistic="Sum",
                period=Duration.minutes(5),
            ),
            threshold=1,
            evaluation_periods=1,
            treat_missing_data=cw.TreatMissingData.NOT_BREACHING,
        )
        pricing_fallback_alarm.add_alarm_action(
            cw_actions.SnsAction(alert_topic)
        )
        operations_alarms["pricing_fallback"] = pricing_fallback_alarm

        broker_api_fn.add_environment(
            "OPERATIONS_ALARM_NAMES_JSON",
            cdk.Fn.to_json_string(
                {
                    key: alarm.alarm_name
                    for key, alarm in operations_alarms.items()
                }
            ),
        )
        broker_api_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "cloudwatch:GetMetricData",
                    "cloudwatch:DescribeAlarms",
                ],
                resources=["*"],
            )
        )

        # ------------------------------------------------------------------
        # Dashboard
        # ------------------------------------------------------------------
        dashboard = cw.Dashboard(
            self,
            "Dashboard",
            # Preserve the deployed dashboard name for in-place upgrades.
            dashboard_name="bedrock-per-user-quota-gateway",
        )

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
            search_widget("Credential vends per user", "CredentialsVended"),
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
        dashboard.add_widgets(
            cw.GraphWidget(
                title="Invocation-to-detection lag (p95)",
                width=12,
                left=[
                    cw.Metric(
                        namespace=METRICS_NAMESPACE,
                        metric_name="DetectionLagMilliseconds",
                        statistic="p95",
                        period=Duration.minutes(5),
                    )
                ],
            ),
            cw.GraphWidget(
                title="Permission lease lifecycle",
                width=12,
                left=[
                    cw.Metric(
                        namespace=METRICS_NAMESPACE,
                        metric_name=metric_name,
                        statistic="Sum",
                        period=Duration.minutes(5),
                    )
                    for metric_name in (
                        "LeaseStarted",
                        "LeaseRefreshed",
                        "LeaseRetried",
                    )
                ],
            ),
        )

        if config.credential_enforcement_mode == "revocation":
            dashboard.add_widgets(
                cw.GraphWidget(
                    title="Revocation reconciliation",
                    width=12,
                    left=[
                        cw.Metric(
                            namespace=METRICS_NAMESPACE,
                            metric_name=metric_name,
                            statistic="Sum",
                            period=Duration.minutes(5),
                        )
                        for metric_name in (
                            "RevocationSyncSuccess",
                            "RevocationSyncFailure",
                            "RevocationPolicyOverflow",
                        )
                    ],
                )
            )

        # ------------------------------------------------------------------
        # Outputs
        # ------------------------------------------------------------------
        cdk.CfnOutput(
            self,
            "BrokerApiUrl",
            value=fn_url.url,
            description="AWS_IAM Function URL for credential vending and administration",
        )
        # Backwards-compatible output name for existing scripts and notebooks.
        cdk.CfnOutput(
            self,
            "GatewayUrl",
            value=fn_url.url,
            description="Deprecated alias of BrokerApiUrl",
        )
        cdk.CfnOutput(self, "AdminKeySecretArn", value=admin_secret.secret_arn)
        cdk.CfnOutput(
            self,
            "EmergencyKeySecretArn",
            value=emergency_secret.secret_arn,
            description="Break-glass key; never embed in the admin UI",
        )
        cdk.CfnOutput(self, "UsersTableName", value=users_table.table_name)
        cdk.CfnOutput(self, "UsageTableName", value=usage_table.table_name)
        cdk.CfnOutput(self, "AlertTopicArn", value=alert_topic.topic_arn)
        cdk.CfnOutput(self, "BedrockUserRoleArn", value=bedrock_user_role.role_arn,
                      description="Role the broker vends to users for native Bedrock calls.")
        cdk.CfnOutput(self, "InvocationLogGroup", value=invocation_log_group.log_group_name,
                      description="Bedrock model-invocation logs used for per-user metering.")
        cdk.CfnOutput(self, "JwtIssuer", value=jwt_issuer,
                      description="OIDC issuer whose JWTs the broker accepts")
        cdk.CfnOutput(self, "DenyDirectBedrockPolicyArn", value=deny_direct.managed_policy_arn,
                      description="Attach to non-vended roles to prevent quota bypass")
        cdk.CfnOutput(
            self,
            "EmergencyDenyPolicyArn",
            value=emergency_deny_policy.managed_policy_arn,
            description="Operator-controlled shared-role emergency deny policy",
        )
        cdk.CfnOutput(
            self,
            "BrokerApiRoleArn",
            value=broker_api_fn.role.role_arn,
            description="Control-plane role; it cannot invoke Bedrock models",
        )
        cdk.CfnOutput(
            self,
            "GatewayRoleArn",
            value=broker_api_fn.role.role_arn,
            description="Deprecated alias of BrokerApiRoleArn",
        )
        cdk.CfnOutput(
            self, "ModelPriceSnapshot",
            value=model_prices_json,
            description="Standard on-demand USD-per-MTok prices captured at stack deployment",
        )
        cdk.CfnOutput(
            self, "ModelFallbackPrice",
            value=fallback_price_json,
            description="Conservative USD-per-MTok fallback for unknown model IDs",
        )
        if user_pool is not None:
            cdk.CfnOutput(self, "DemoUserPoolId", value=user_pool.user_pool_id)
            cdk.CfnOutput(self, "DemoUserPoolClientId",
                          value=user_pool_client.user_pool_client_id,
                          description="App client for the demo notebook to obtain JWTs")
