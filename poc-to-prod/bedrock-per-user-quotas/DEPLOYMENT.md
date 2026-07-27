# Deployment guide

This is the canonical deployment guide for the Amazon Bedrock per-user quota
sample. The stack always keeps the Lambda Function URL private with
`AWS_IAM`; callers use SigV4 and send the end-user JWT separately in
`X-Quota-User-Token`.

## Decisions before deployment

Make these decisions before running `cdk deploy`:

| Decision | Demo/personal account | Production/shared account |
|---|---|---|
| Identity provider | Stack-created Cognito demo pool | Existing OIDC IdP |
| Quota identity | `sub` (one budget per user) | IdP-controlled user or tenant claim |
| Enforcement mode | Mode A, Mode B, or both | Choose per workload; understand their different guarantees |
| User creation | Auto-provision with defaults | Usually provision during onboarding |
| Default limits | Small demo limits | Limits approved by the platform owner |
| Mode B models | Model IDs enforced by the app | Explicit `mode_b_allowed_model_ids` |
| Mode A models | `*` is convenient | Explicit Bedrock resource ARNs |
| Function URL callers | Account principals for convenience | Explicit backend role ARNs |
| Invocation logging | Let the stack manage it | Reuse the account's existing configuration |
| Usage TTL | 35 days | Retention required by operations/compliance |
| Stack deletion | `DESTROY` quota tables | `RETAIN` quota tables |
| Direct Bedrock access | Acceptable for an isolated demo | Deny direct access with IAM/SCP or accept quota bypass |
| Alerts | Optional email | Owned SNS subscription and response process |
| Unknown model pricing | Conservative fallback | Review fallback against the most expensive enabled model |

### Mode A versus Mode B

- **Mode A** vends short-lived STS credentials for native
  `bedrock-runtime` APIs. Bedrock model-invocation logs are reconciled every
  five minutes. A user may continue spending until the current credentials
  expire and telemetry is reconciled, so this mode has bounded overspend.
- **Mode B** proxies `bedrock-mantle`, reserves before the request, and settles
  from actual usage. A request that cannot fit is rejected with HTTP 429
  before inference, providing the hard pre-spend cap.

Mode A requires model-invocation logging. Do not grant mantle actions to the
vended role: mantle traffic is not the reconciler's metering source. Mode B
does not weaken Mode A's logging requirements.

### User claim versus tenant claim

`jwt_user_claim` is the budget key:

- `sub` gives every end user an independent budget.
- An IdP-controlled claim such as `custom:tenant_id` shares one budget among
  all users in that tenant.

The claim must be signed, always present, and not editable by the end user.
The gateway trusts one issuer per deployment. Deploy separate stacks or add
issuer namespacing before accepting multiple issuers.

### Cognito demo versus your IdP

When `jwt_issuer` is empty, the stack creates a Cognito user pool and app
client for demonstration. It is not an application onboarding system.
Production should set `jwt_issuer`, normally set `jwt_audience`, and optionally
set `jwt_jwks_url` only when OIDC discovery does not expose `jwks_uri`.

## Prerequisites

- Python 3.12 or newer, Node.js, the AWS CDK CLI, AWS CLI v2, `jq`, and a
  running Docker-compatible container engine for Lambda asset bundling.
- AWS credentials for the target account and region, plus permission to
  bootstrap/deploy CloudFormation, IAM, Lambda, DynamoDB, CloudWatch, SNS,
  Secrets Manager, Cognito when used, and `pricing:GetProducts`.
- Bedrock model access for every configured model and access to the
  `bedrock-mantle` endpoint when using Mode B.
- In shared accounts, an existing model-invocation log group and confirmation
  from its owner that Bedrock is already delivering the required records.

Install the CDK CLI if it is not already available:

```bash
npm install --global aws-cdk
cdk --version
aws --version
```

## Configuration

Pass a validated JSON file with:

```bash
cdk synth -c deployment_config=config/demo.json
```

Individual CDK contexts take precedence over the file, preserving existing
commands such as `-c jwt_issuer=...` and
`-c invoker_principal_arns=arn1,arn2`.

| Key | Default without a file | Validation and effect |
|---|---:|---|
| `auto_provision_users` | `true` | Boolean; first valid JWT creates a quota row |
| `default_daily_usd` | `1.0` | Positive number for new users |
| `default_daily_input_tokens` | `1000000` | Positive integer for new users |
| `default_daily_output_tokens` | `200000` | Positive integer for new users |
| `warn_threshold` | `0.8` | Number greater than 0 and less than 1 |
| `usage_retention_days` | `35` | Positive integer used for usage-row TTL |
| `retain_tables_on_delete` | `false` | Boolean; maps tables to `RETAIN` or `DESTROY` |
| `manage_invocation_logging` | no implicit choice | Must be explicitly true, or false with an existing group |
| `invocation_log_group_name` | empty | Existing group; incompatible with managed logging |
| `mode_b_allowed_model_ids` | `[]` (allow all) | Application model IDs, never ARNs |
| `mode_a_allowed_model_arns` | `["*"]` | Bedrock IAM resource ARNs or `*`, never model IDs |
| `invoker_principal_arns` | `[]` (account root grant) | IAM principals allowed to invoke the Function URL |
| `model_config` | `config/model-pricing.json` | JSON price/catalog configuration |
| `jwt_user_claim` | `sub` | Non-empty signed JWT claim |
| `vended_ttl_seconds` | `900` | 900 through 43200 seconds |
| `snapstart` | `false` | Strict boolean |
| `reconciler_interval_minutes` | `5` | Positive integer; EventBridge cadence (1 = demo, 5 = default, 15 = heavy log volume — shorter re-scans more Logs Insights data) |
| `default_mantle_project_id` | `default` | Bedrock Project injected on Mode B when a user has no `mantle_project_id` |
| `admin_jwt_claim` | empty | JWT claim that authorizes the `/admin` API (e.g. `cognito:groups`); empty = shared key only |
| `admin_jwt_value` | empty | Required value in `admin_jwt_claim` (e.g. `quota-admins`) |
| `admin_ui` | `false` | Boolean; deploy the S3+CloudFront admin console (demo Cognito pool only) |
| `experimental_native_session_deny` | `false` | Boolean; reconciler revokes blocked users' vended sessions early via a SourceIdentity Deny — grants it `iam:PutRolePolicy` on the vended role (AppSec sign-off) |

The CDK configuration requires positive default limits. The admin API permits
zero for an individual limit, where zero means that dimension is unlimited.
Block a user with the status endpoint rather than setting all limits to zero.

### Auto-provisioning and defaults

With auto-provisioning enabled, any identity carrying a valid JWT from the
configured issuer receives the deployment defaults on first use. This is
convenient for a demo but grants spend automatically. With it disabled, create
the user or tenant through the admin API as part of onboarding.

### TTL and table retention are different

`usage_retention_days` writes a DynamoDB TTL timestamp on daily usage rows.
DynamoDB deletion after that timestamp is asynchronous. It does not control
the 00:00 UTC quota reset. User records do not expire; short-lived
session-to-user map rows expire separately after two days.

`retain_tables_on_delete` controls CloudFormation deletion:

- `false`: demo mode; `cdk destroy` deletes both quota tables.
- `true`: production mode; stack deletion or table replacement retains data.
  Retained tables are no longer managed by the deleted stack and require an
  explicit migration/import decision before redeployment.

### Models, allowlists, and prices

`cdk/config/model-pricing.json` has three independent sections:

```json
{
  "catalog_models": {
    "gpt-oss-120b": [
      "openai.gpt-oss-120b",
      "openai.gpt-oss-120b-1:0"
    ]
  },
  "price_overrides": {
    "anthropic.claude-opus-4-7": {
      "input_per_mtok": 15.0,
      "output_per_mtok": 75.0
    }
  },
  "fallback_price": {
    "input_per_mtok": 15.0,
    "output_per_mtok": 75.0
  }
}
```

- `catalog_models` maps an AWS Price List model name to every application or
  invocation-log model ID that should share that price. GPT OSS prices are
  resolved dynamically this way.
- `price_overrides` pins models not yet available from Price List. Claude Opus
  4.7 uses this mechanism.
- `fallback_price` prices unknown model IDs.

The custom resource queries AWS Price List once on stack create/update. There
is no timer, background refresh, or request-time Pricing call. The exact same
`MODEL_PRICES_JSON` token and fallback JSON are injected into gateway and
reconciler.

The default unknown-model fallback is deliberately expensive. At deployment,
the resolver raises each fallback dimension to at least the highest known
price in the captured snapshot. It prevents an unmapped model from appearing
free, but it can overstate USD usage, reject Mode B requests earlier, or block
Mode A users earlier than the provider's actual charge. Token quotas remain
based on measured tokens. Review the fallback whenever enabling a more
expensive model, and add a catalog mapping or override when accurate USD
quotas matter.

The allowlists are not interchangeable:

- `mode_b_allowed_model_ids` contains request-body IDs such as
  `openai.gpt-oss-120b`. The proxy rejects other IDs and filters `/v1/models`.
- `mode_a_allowed_model_arns` becomes the vended role's IAM `Resource` list.
  Use foundation-model, inference-profile, or provisioned-model ARNs that
  match the native APIs in your region.

An empty Mode B list and Mode A `*` preserve the sample's historical behavior,
but production should restrict both.

## Walkthrough: Demo/personal account

This path creates the demo Cognito pool, lets the stack manage regional
invocation logging, auto-provisions users, and destroys quota tables on
teardown.

From the sample root:

```bash
export AWS_PROFILE=your-demo-profile
export AWS_REGION=us-east-1
export ALERT_EMAIL=you@example.com
export ACCOUNT_ID=$(
  aws --profile "$AWS_PROFILE" sts get-caller-identity \
    --query Account --output text
)

python3 -m venv cdk/.venv
source cdk/.venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r cdk/requirements.txt
python -m pip install -r gateway/requirements.txt pytest

cd cdk
cdk bootstrap "aws://${ACCOUNT_ID}/${AWS_REGION}" \
  --profile "$AWS_PROFILE"

cdk synth \
  --profile "$AWS_PROFILE" \
  -c deployment_config=config/demo.json \
  -c alert_email="$ALERT_EMAIL"

cdk deploy \
  --profile "$AWS_PROFILE" \
  -c deployment_config=config/demo.json \
  -c alert_email="$ALERT_EMAIL"
cd ..
```

The deploy captures the current configured price snapshot. Confirm the SNS
subscription sent to `ALERT_EMAIL`; alerts are not delivered until confirmed.

Inspect and export outputs:

```bash
aws --profile "$AWS_PROFILE" --region "$AWS_REGION" \
  cloudformation describe-stacks \
  --stack-name BedrockPerUserQuotaGateway \
  --query 'Stacks[0].Outputs' --output table

export GATEWAY_URL=$(
  aws --profile "$AWS_PROFILE" --region "$AWS_REGION" \
    cloudformation describe-stacks \
    --stack-name BedrockPerUserQuotaGateway \
    --query "Stacks[0].Outputs[?OutputKey=='GatewayUrl'].OutputValue | [0]" \
    --output text
)
export ADMIN_SECRET_ARN=$(
  aws --profile "$AWS_PROFILE" --region "$AWS_REGION" \
    cloudformation describe-stacks \
    --stack-name BedrockPerUserQuotaGateway \
    --query "Stacks[0].Outputs[?OutputKey=='AdminKeySecretArn'].OutputValue | [0]" \
    --output text
)
export ADMIN_KEY=$(
  aws --profile "$AWS_PROFILE" --region "$AWS_REGION" \
    secretsmanager get-secret-value \
    --secret-id "$ADMIN_SECRET_ARN" \
    --query SecretString --output text
)
```

Smoke-test the IAM-protected URL and admin path:

```bash
AWS_PROFILE="$AWS_PROFILE" AWS_REGION="$AWS_REGION" python -c '
import os
from examples.sigv4_gateway import signed_request
r = signed_request("GET", os.environ["GATEWAY_URL"] + "/healthz")
r.raise_for_status()
print(r.json())
'

python examples/sigv4_gateway.py \
  --profile "$AWS_PROFILE" \
  --region "$AWS_REGION" \
  create-user demo-user \
  --daily-usd 1 \
  --daily-input-tokens 1000000 \
  --daily-output-tokens 200000

python examples/sigv4_gateway.py \
  --profile "$AWS_PROFILE" \
  --region "$AWS_REGION" \
  list-users
```

For an inference smoke test, create a demo Cognito user and obtain its ID
token with the notebook, then call Mode A or Mode B using that token. The
demo runbook is in [DEMO.md](DEMO.md).

## Walkthrough: Production/shared account

This path uses an existing IdP and invocation log group, disables automatic
user creation, restricts callers/models, retains DynamoDB tables, and makes
bypass prevention an explicit rollout step.

First define deployment-specific values. The example ARNs and issuer must be
replaced:

```bash
export AWS_PROFILE=your-production-deployer-profile
export AWS_INVOKER_PROFILE=your-gateway-invoker-profile
export AWS_REGION=us-east-1
export ALERT_EMAIL=platform-alerts@example.com
export JWT_ISSUER=https://idp.example.com
export JWT_AUDIENCE=bedrock-quota-gateway
export JWT_USER_CLAIM=custom:tenant_id
export INVOCATION_LOG_GROUP=/aws/bedrock/modelinvocations
export INVOKER_PRINCIPAL_ARN=arn:aws:iam::111122223333:role/BedrockQuotaGatewayInvoker
export MODE_A_MODEL_ARNS_JSON='[
  "arn:aws:bedrock:us-east-1::foundation-model/openai.gpt-oss-120b-1:0",
  "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-opus-4-7"
]'
export MODE_B_MODEL_IDS_JSON='[
  "openai.gpt-oss-120b",
  "anthropic.claude-opus-4-7"
]'
export ACCOUNT_ID=$(
  aws --profile "$AWS_PROFILE" sts get-caller-identity \
    --query Account --output text
)
export MODEL_CONFIG_PATH="$(pwd)/cdk/config/model-pricing.json"
```

The existing log group must already receive Bedrock model-invocation logs for
this account and region. Verify that its records include the assumed-role
session identity used by Mode A before relying on USD or token enforcement.

Generate a local deployment file from the reviewed production baseline:

```bash
jq \
  --arg alert_email "$ALERT_EMAIL" \
  --arg jwt_issuer "$JWT_ISSUER" \
  --arg jwt_audience "$JWT_AUDIENCE" \
  --arg jwt_user_claim "$JWT_USER_CLAIM" \
  --arg log_group "$INVOCATION_LOG_GROUP" \
  --arg invoker "$INVOKER_PRINCIPAL_ARN" \
  --arg model_config "$MODEL_CONFIG_PATH" \
  --argjson mode_a "$MODE_A_MODEL_ARNS_JSON" \
  --argjson mode_b "$MODE_B_MODEL_IDS_JSON" \
  '
    .alert_email = $alert_email
    | .jwt_issuer = $jwt_issuer
    | .jwt_audience = $jwt_audience
    | .jwt_user_claim = $jwt_user_claim
    | .invocation_log_group_name = $log_group
    | .invoker_principal_arns = [$invoker]
    | .mode_a_allowed_model_arns = $mode_a
    | .mode_b_allowed_model_ids = $mode_b
    | .model_config = $model_config
  ' cdk/config/production.json > /tmp/bedrock-quota-production.json
```

Review `/tmp/bedrock-quota-production.json`, the IdP claim ownership, all
model ARNs, the fallback price, and the invoking backend role before
continuing.

Install, bootstrap, synth, and deploy:

```bash
python3 -m venv cdk/.venv
source cdk/.venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r cdk/requirements.txt
python -m pip install -r gateway/requirements.txt pytest

cd cdk
cdk bootstrap "aws://${ACCOUNT_ID}/${AWS_REGION}" \
  --profile "$AWS_PROFILE"

cdk synth \
  --profile "$AWS_PROFILE" \
  -c deployment_config=/tmp/bedrock-quota-production.json

cdk deploy \
  --profile "$AWS_PROFILE" \
  -c deployment_config=/tmp/bedrock-quota-production.json
cd ..
```

Confirm the SNS subscription; alerts are not delivered until an owner accepts
it. Export the production outputs:

```bash
aws --profile "$AWS_PROFILE" --region "$AWS_REGION" \
  cloudformation describe-stacks \
  --stack-name BedrockPerUserQuotaGateway \
  --query 'Stacks[0].Outputs' --output table

export GATEWAY_URL=$(
  aws --profile "$AWS_PROFILE" --region "$AWS_REGION" \
    cloudformation describe-stacks \
    --stack-name BedrockPerUserQuotaGateway \
    --query "Stacks[0].Outputs[?OutputKey=='GatewayUrl'].OutputValue | [0]" \
    --output text
)
export ADMIN_SECRET_ARN=$(
  aws --profile "$AWS_PROFILE" --region "$AWS_REGION" \
    cloudformation describe-stacks \
    --stack-name BedrockPerUserQuotaGateway \
    --query "Stacks[0].Outputs[?OutputKey=='AdminKeySecretArn'].OutputValue | [0]" \
    --output text
)
export ADMIN_KEY=$(
  aws --profile "$AWS_PROFILE" --region "$AWS_REGION" \
    secretsmanager get-secret-value \
    --secret-id "$ADMIN_SECRET_ARN" \
    --query SecretString --output text
)
```

The following smoke tests require AWS credentials for a principal listed in
`invoker_principal_arns`; the deployer is not automatically granted access in
production. `AWS_INVOKER_PROFILE` must resolve to that role. Check the
IAM-protected health endpoint:

```bash
AWS_PROFILE="$AWS_INVOKER_PROFILE" AWS_REGION="$AWS_REGION" python -c '
import os
from examples.sigv4_gateway import signed_request
r = signed_request("GET", os.environ["GATEWAY_URL"] + "/healthz")
r.raise_for_status()
print(r.json())
'
```

Provision a tenant before presenting its JWT:

```bash
python examples/sigv4_gateway.py \
  --profile "$AWS_INVOKER_PROFILE" \
  --region "$AWS_REGION" \
  create-user tenant-acme \
  --name "ACME" \
  --daily-usd 25 \
  --daily-input-tokens 10000000 \
  --daily-output-tokens 2000000
```

With a real ID token whose configured claim resolves to `tenant-acme`:

```bash
export USER_JWT='replace-with-a-short-lived-id-token'

AWS_PROFILE="$AWS_INVOKER_PROFILE" AWS_REGION="$AWS_REGION" python -c '
import os
from examples.sigv4_gateway import signed_request
r = signed_request(
    "POST",
    os.environ["GATEWAY_URL"] + "/v1/credentials",
    user_token=os.environ["USER_JWT"],
)
r.raise_for_status()
body = r.json()
print({"region": body["region"], "expiration": body["expiration"], "user_id": body["user_id"]})
'
```

This checks JWT verification, claim extraction, quota lookup, IAM invocation,
and Mode A credential vending without making an inference call.

## Administrative commands

The client uses `AWS_PROFILE`/`--profile` for SigV4 and reads `GATEWAY_URL`,
`ADMIN_KEY`, and `AWS_REGION` from the environment when flags are omitted.
For the commands below, select the authorized caller profile:

```bash
export GATEWAY_INVOKER_PROFILE="${AWS_INVOKER_PROFILE:-$AWS_PROFILE}"
```

Create a user or tenant with all three daily limits:

```bash
python examples/sigv4_gateway.py \
  --profile "$GATEWAY_INVOKER_PROFILE" --region "$AWS_REGION" \
  create-user tenant-acme \
  --name "ACME" \
  --daily-usd 25 \
  --daily-input-tokens 10000000 \
  --daily-output-tokens 2000000
```

List users, limits, status, and today's usage:

```bash
python examples/sigv4_gateway.py \
  --profile "$GATEWAY_INVOKER_PROFILE" --region "$AWS_REGION" \
  list-users
```

Update any or all limits:

```bash
python examples/sigv4_gateway.py \
  --profile "$GATEWAY_INVOKER_PROFILE" --region "$AWS_REGION" \
  update-user tenant-acme \
  --daily-usd 30 \
  --daily-input-tokens 12000000 \
  --daily-output-tokens 2500000
```

Block and unblock:

```bash
python examples/sigv4_gateway.py \
  --profile "$GATEWAY_INVOKER_PROFILE" --region "$AWS_REGION" \
  block-user tenant-acme --reason "manual cost review"

python examples/sigv4_gateway.py \
  --profile "$GATEWAY_INVOKER_PROFILE" --region "$AWS_REGION" \
  unblock-user tenant-acme --reason "review complete"
```

Query the current or a specific UTC quota window:

```bash
python examples/sigv4_gateway.py \
  --profile "$GATEWAY_INVOKER_PROFILE" --region "$AWS_REGION" \
  get-usage tenant-acme

python examples/sigv4_gateway.py \
  --profile "$GATEWAY_INVOKER_PROFILE" --region "$AWS_REGION" \
  get-usage tenant-acme --window 2026-07-14
```

## Invocation logging ownership

Bedrock model-invocation logging is one account-and-region-wide setting:

- `manage_invocation_logging=true` lets this stack overwrite that setting.
  The generated logging configuration, role, and log group are retained on
  `cdk destroy` because the prior setting cannot be restored automatically.
- `manage_invocation_logging=false` requires
  `invocation_log_group_name`. The stack reads the group and does not mutate
  regional logging.
- Supplying an existing group together with managed logging is rejected.

In a shared account, the security/logging owner should manage the regional
configuration and grant this stack read/query access to its destination.

## Preventing quota bypass

Deployment is not complete until the account owner decides how direct Bedrock
permissions are handled. Any principal that retains `bedrock:InvokeModel*`,
`bedrock:Converse*`, or `bedrock-mantle:*` can bypass per-user quotas.

The stack outputs `DenyDirectBedrockPolicyArn`. Attach it to every governed
role in a single-account setup. For organization-wide enforcement, deploy an
SCP equivalent to:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Sid": "OnlyQuotaGatewayMayInvokeBedrock",
    "Effect": "Deny",
    "Action": [
      "bedrock-mantle:CreateInference",
      "bedrock-mantle:CallWithBearerToken",
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
      "bedrock:Converse",
      "bedrock:ConverseStream"
    ],
    "Resource": "*",
    "Condition": {
      "ArnNotLike": {
        "aws:PrincipalArn": [
          "<GatewayRoleArn>",
          "<BedrockUserRoleArn>"
        ]
      }
    }
  }]
}
```

Both output roles are exceptions: the gateway role serves Mode B and the
vended role serves Mode A. Validate an SCP in a non-production OU first and
account for break-glass, service, and deployment roles. Choosing not to apply
a deny is valid only when bypass is explicitly accepted.

Restrict `invoker_principal_arns` independently. It controls who may reach the
IAM-authenticated Function URL; it does not grant or deny direct Bedrock
access.

## Verification and teardown

Run local verification without AWS changes:

```bash
cdk/.venv/bin/python -m pytest tests -q
git diff --check
```

Demo teardown:

```bash
cd cdk
cdk destroy \
  --profile "$AWS_PROFILE" \
  -c deployment_config=config/demo.json
```

The demo tables are deleted. Managed invocation-logging resources remain for
manual review.

For production, `retain_tables_on_delete=true` keeps both tables. Before
destroying the stack, record their names, ownership, backup, and import or
decommission plan. `cdk destroy` is not a data lifecycle policy.
