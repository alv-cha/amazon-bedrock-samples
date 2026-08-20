# Deployment guide

This is the canonical deployment guide for the runtime-only per-user quota
sample.

## Decisions before deployment

| Decision | Demo/personal account | Production/shared account |
|---|---|---|
| Quota identity | JWT `sub` | Stable user, tenant, team, or project claim |
| IdP | Stack-created Cognito | Existing OIDC IdP |
| Enforcement | Bounded overspend | Accept and quantify bounded overspend |
| Credential lifetime | 900 seconds | 900 seconds unless refresh load justifies more |
| Runtime models | `*` for exploration | Explicit model and inference-profile ARNs |
| Invocation logging | Stack managed | Reuse centrally managed logging |
| Log subscription | Dedicated demo group | Confirm subscription-filter capacity and ownership |
| Auto-provisioning | Enabled | Usually disabled |
| Function URL callers | Account default | Explicit backend/admin role ARNs |
| Usage retention | 35 days | Policy-defined value |
| DynamoDB deletion | `DESTROY` | `RETAIN` |
| Alerts | Personal email | Operations topic/email |
| Admin UI | Stack-created Cognito path | Integrate corporate IdP and Identity Pool |
| Direct Bedrock access | Optional demo deny policy | Required SCP, boundary, or equivalent deny |
| Price fallback | Conservative default | Review against most expensive allowed model |

The hard architectural decision is the enforcement guarantee. This sample
does not inspect each inference request. A user can continue spending with an
already issued STS session until it expires.

## Runtime-only request flow

1. The application obtains a JWT from Cognito or its own OIDC IdP.
2. An authorized AWS principal SigV4-signs `POST /v1/credentials` to the
   broker's `AWS_IAM` Function URL and sends the JWT in
   `X-Quota-User-Token`.
3. The broker verifies issuer, audience, signature, expiry, and the configured
   identity claim.
4. DynamoDB provides status, limits, and the latest daily usage.
5. The broker assumes `BedrockUserRole` for 15 minutes and stamps a
   collision-resistant session identity.
6. The application calls `bedrock-runtime` directly with those credentials.
7. Bedrock sends model invocation logs to CloudWatch Logs.
8. A subscription invokes the usage processor.
9. The processor deduplicates by Bedrock `requestId`, prices tokens, updates
   DynamoDB, emits metrics, and sends warnings/blocks.
10. A blocked identity cannot obtain another session.

## Configuration

Pass a JSON file using:

```bash
npx cdk synth -c deployment_config=config/demo.json
```

Direct `-c key=value` values override the file.

| Key | Default | Validation and meaning |
|---|---:|---|
| `auto_provision_users` | `true` | Boolean; create a quota row on first valid JWT |
| `default_daily_usd` | `1.0` | Positive number |
| `default_daily_input_tokens` | `1000000` | Positive integer |
| `default_daily_output_tokens` | `200000` | Positive integer |
| `warn_threshold` | `0.8` | Greater than 0 and less than 1 |
| `usage_retention_days` | `35` | Positive integer; DynamoDB TTL retention |
| `retain_tables_on_delete` | `false` | `true` maps tables to `RETAIN` |
| `vended_ttl_seconds` | `900` | Between 900 and 43200 seconds |
| `allowed_model_arns` | `["*"]` | Non-empty Bedrock resource ARN list or `*` |
| `invoker_principal_arns` | `[]` | IAM principals allowed to invoke the Function URL |
| `manage_invocation_logging` | none | Explicit `true` or `false` required |
| `invocation_log_group_name` | empty | Required when logging is externally managed |
| `model_config` | `config/model-pricing.json` | Validated catalog, overrides, and fallback |
| `jwt_issuer` | empty | Empty creates demo Cognito |
| `jwt_audience` | empty | Required value depends on the IdP |
| `jwt_jwks_url` | discovery | Explicit JWKS URL when discovery is unavailable |
| `jwt_user_claim` | `sub` | Claim used as the quota key |
| `admin_jwt_claim` | empty | Claim used for browser admin authorization |
| `admin_jwt_value` | empty | Required claim value or group |
| `admin_ui` | `false` | Demo Cognito only; requires both admin JWT fields |
| `alert_email` | empty | Creates an SNS email subscription |
| `snapstart` | `false` | Enable Python Lambda SnapStart for the broker |
| `adapter_layer_arn` | regional default | Override Lambda Web Adapter layer |

Legacy `mode_a_allowed_model_arns` is accepted as an alias for
`allowed_model_arns`. Former dual-mode keys synthesize only for migration and
are ignored with a warning. Remove them.

For limits changed through the admin API, `0` disables that individual quota
dimension. Deployment defaults must remain positive so auto-provisioned users
never become unlimited accidentally.

### Upgrading the former dual-mode stack

The runtime-only update preserves the existing `GatewayFn` construct,
Function URL, `GatewayUrl`, `GatewayRoleArn`, and CloudWatch dashboard name.
`BrokerApiUrl` and `BrokerApiRoleArn` are the canonical output names after the
update.

The update deliberately removes the inference proxy routes, Mantle
permissions, scheduled reconciler, and its EventBridge rule. Existing
DynamoDB tables and daily rows remain compatible. Review the CloudFormation
change set before deployment and update clients to:

1. Call `POST /v1/credentials`.
2. Build a normal `bedrock-runtime` client from the returned credentials.
3. Stop using the old OpenAI/Anthropic proxy base URLs.

There is no hard pre-spend cap after this migration. The sole guarantee is the
bounded-overspend behavior described above.

### Model and inference-profile IAM

Production should list exact resources:

```json
{
  "allowed_model_arns": [
    "arn:aws:bedrock:us-east-1::foundation-model/PROVIDER.MODEL-ID",
    "arn:aws:bedrock:us-east-1:111122223333:inference-profile/PROFILE-ID"
  ]
}
```

Inference profiles can also require permission to their underlying foundation
model resources. Validate the complete policy for every profile used.

The vended role does not grant `bedrock:CallWithBearerToken`. Bedrock API keys
are therefore intentionally outside this sample. Applications use the
temporary STS credentials and SigV4.

### Price configuration

`config/model-pricing.json` contains:

```json
{
  "catalog_models": {
    "price-list-model-name": ["runtime-model-id"]
  },
  "price_overrides": {
    "model-without-resolvable-standard-price": {
      "input_per_mtok": 15,
      "output_per_mtok": 75
    }
  },
  "fallback_price": {
    "input_per_mtok": 15,
    "output_per_mtok": 75
  }
}
```

AWS Price List is queried once during deployment. The resulting snapshot is
injected only into the usage processor. No scheduled refresh exists.

An unknown ID is charged at the fallback, which deployment raises to at least
the highest input and output rates in the known snapshot. This can make USD
usage higher than the final bill. It is not a universal upper bound for a more
expensive model or a modality that is not billed by input/output tokens.
Review pricing before adding models, inference profiles, service tiers, prompt
caching, provisioned throughput, image/video generation, or separately billed
tools. Add every model ID or profile ARN emitted by invocation logging to the
pricing mapping when accurate USD enforcement is required.

### Invocation logging ownership

Bedrock model invocation logging is an account- and Region-level setting.

`manage_invocation_logging=true`:

- Creates a log group and Bedrock writer role.
- Overwrites the Region's existing invocation logging configuration.
- Disables prompt/response payload delivery.
- Retains the logging configuration, role, and group on stack deletion because
  the previous configuration cannot be reconstructed.

Use this only in a demo or account where the stack owns the setting.

`manage_invocation_logging=false`:

- Requires `invocation_log_group_name`.
- Does not change the account-wide setting.
- Adds a subscription filter for the vended role.

In a shared account, confirm that the log group already receives Runtime
invocation logs and has available subscription-filter capacity. This stack
must not displace a security or central logging subscription.

### TTL and reset

The quota window changes at `00:00 UTC`; TTL does not reset quotas. TTL only
removes old usage, request-id markers, and session mappings asynchronously.

An identity automatically blocked in an earlier window is reactivated when it
next requests credentials and the current window is under quota. A manually
blocked identity is never automatically reactivated.

## Demo/personal account

### 1. Prerequisites

- AWS CLI and an authorized profile.
- Node.js and npm.
- Python 3.12 or later.
- Finch. Docker is not required.
- Bedrock model access in the selected Region.

```bash
cd poc-to-prod/bedrock-per-user-quotas

export AWS_PROFILE=your-profile
export AWS_REGION=us-east-1
export AWS_DEFAULT_REGION=$AWS_REGION
export CDK_DOCKER=finch
export ALERT_EMAIL=you@example.com

aws sso login --profile "$AWS_PROFILE"   # omit for non-SSO credentials
aws sts get-caller-identity
finch vm start
finch info
```

### 2. Build dependencies

```bash
cd admin-ui
npm ci
npm run build

cd ../cdk
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
npm ci
```

### 3. Bootstrap, synthesize, and deploy

```bash
npx cdk bootstrap

npx cdk synth \
  -c deployment_config=config/demo.json \
  -c alert_email="$ALERT_EMAIL"

npx cdk deploy \
  -c deployment_config=config/demo.json \
  -c alert_email="$ALERT_EMAIL"
```

Confirm the SNS subscription from the email AWS sends. Until confirmed,
warnings and block notifications are not delivered to that address.

### 4. Read outputs

```bash
export STACK_NAME=BedrockPerUserQuotaGateway

export BROKER_API_URL=$(
  aws cloudformation describe-stacks \
    --stack-name "$STACK_NAME" \
    --query "Stacks[0].Outputs[?OutputKey=='BrokerApiUrl'].OutputValue | [0]" \
    --output text
)

export ADMIN_SECRET_ARN=$(
  aws cloudformation describe-stacks \
    --stack-name "$STACK_NAME" \
    --query "Stacks[0].Outputs[?OutputKey=='AdminKeySecretArn'].OutputValue | [0]" \
    --output text
)

export ADMIN_KEY=$(
  aws secretsmanager get-secret-value \
    --secret-id "$ADMIN_SECRET_ARN" \
    --query SecretString \
    --output text
)
```

### 5. Configure the demo administrator

`config/demo.json` deploys the UI, creates the Cognito group `quota-admins`,
and authorizes that group. Create an administrator and add it to the group:

```bash
export USER_POOL_ID=$(
  aws cloudformation describe-stacks \
    --stack-name "$STACK_NAME" \
    --query "Stacks[0].Outputs[?OutputKey=='DemoUserPoolId'].OutputValue | [0]" \
    --output text
)

aws cognito-idp admin-create-user \
  --user-pool-id "$USER_POOL_ID" \
  --username quota-admin \
  --user-attributes \
    Name=email,Value=admin@example.com \
    Name=email_verified,Value=true \
  --message-action SUPPRESS

aws cognito-idp admin-set-user-password \
  --user-pool-id "$USER_POOL_ID" \
  --username quota-admin \
  --password 'Demo-only-Change-Me-42!' \
  --permanent

aws cognito-idp admin-add-user-to-group \
  --user-pool-id "$USER_POOL_ID" \
  --username quota-admin \
  --group-name quota-admins
```

Open the `AdminUiUrl` output. The deployment writes `config.js` with the
generated broker URL, Region, user pool/client, and identity pool. It contains
no secret. Sign in as `quota-admin` or its verified `admin@example.com` alias.

### 6. Administrative smoke test

```bash
cd ..

cdk/.venv/bin/python examples/sigv4_gateway.py \
  --gateway-url "$BROKER_API_URL" \
  --profile "$AWS_PROFILE" \
  --region "$AWS_REGION" \
  --admin-key "$ADMIN_KEY" \
  create-user demo-user \
  --daily-usd 2 \
  --daily-input-tokens 1000000 \
  --daily-output-tokens 200000

cdk/.venv/bin/python examples/sigv4_gateway.py \
  --gateway-url "$BROKER_API_URL" \
  --profile "$AWS_PROFILE" \
  --region "$AWS_REGION" \
  --admin-key "$ADMIN_KEY" \
  list-users
```

### 7. Runtime smoke test

Obtain a JWT for the Cognito test user or use the notebook, then:

```bash
export GATEWAY_URL="$BROKER_API_URL"
export USER_JWT='your-test-user-jwt'
export USER_ID='value-of-the-configured-jwt-claim'

cdk/.venv/bin/python examples/demo_native_calls.py \
  --model openai.gpt-oss-20b-1:0 \
  --api converse \
  --prompt "Reply with exactly: runtime quota demo"
```

The inference goes directly to `bedrock-runtime`. Allow invocation-log
delivery time before checking usage:

```bash
cdk/.venv/bin/python examples/sigv4_gateway.py \
  --gateway-url "$BROKER_API_URL" \
  --profile "$AWS_PROFILE" \
  --region "$AWS_REGION" \
  --admin-key "$ADMIN_KEY" \
  get-usage "$USER_ID"
```

For the complete enforcement smoke test, run
[`notebook/per_user_quota_demo.ipynb`](notebook/per_user_quota_demo.ipynb).
Its main path:

1. Reads the identity's current daily aggregate.
2. Sets each token limit to current usage plus one token.
3. Vends an STS session and invokes GPT OSS 20B with `Converse`.
4. Displays the actual `Converse` response `usage`.
5. Polls until invocation logging updates DynamoDB.
6. Proves that a new credential request is rejected.
7. Raises all limits through the admin API and proves vending recovers.
8. Locates the matching request ID in the per-user CloudWatch EMF event.

The notebook does not depend on `CountTokens`; support varies by model and it
does not participate in enforcement. A separate optional diagnostic cell is
disabled by default and reports a skip for unsupported models.

## Production/shared account

### 1. Create a private deployment file

Keep the private copy beside `model-pricing.json` so relative paths remain
valid. `config/*.local.json` is ignored by Git:

```bash
cd poc-to-prod/bedrock-per-user-quotas/cdk
cp config/production.json config/production.local.json
```

Edit `config/production.local.json` and replace:

- `alert_email`
- `jwt_issuer`, `jwt_audience`, and `jwt_user_claim`
- `invocation_log_group_name`
- `invoker_principal_arns`
- account, Region, model, and inference-profile ARNs
- quota defaults, retention, and fallback prices

Recommended shape:

```json
{
  "alert_email": "platform-alerts@example.com",
  "jwt_issuer": "https://your-idp.example.com",
  "jwt_audience": "bedrock-runtime-quota-broker",
  "jwt_user_claim": "tenant_id",
  "admin_ui": false,
  "admin_jwt_claim": "",
  "admin_jwt_value": "",
  "auto_provision_users": false,
  "default_daily_usd": 25,
  "default_daily_input_tokens": 10000000,
  "default_daily_output_tokens": 2000000,
  "warn_threshold": 0.75,
  "usage_retention_days": 90,
  "retain_tables_on_delete": true,
  "vended_ttl_seconds": 900,
  "manage_invocation_logging": false,
  "invocation_log_group_name": "/central/bedrock/model-invocations",
  "invoker_principal_arns": [
    "arn:aws:iam::111122223333:role/QuotaBrokerInvoker"
  ],
  "allowed_model_arns": [
    "arn:aws:bedrock:us-east-1::foundation-model/PROVIDER.MODEL-ID"
  ],
  "model_config": "model-pricing.json"
}
```

### 2. Validate logging before deployment

```bash
aws bedrock get-model-invocation-logging-configuration

aws logs describe-subscription-filters \
  --log-group-name /central/bedrock/model-invocations
```

Confirm with the central logging owner that adding this stack's subscription is
acceptable.

### 3. Build, bootstrap, synthesize, and deploy

```bash
cd poc-to-prod/bedrock-per-user-quotas

export AWS_PROFILE=your-production-profile
export AWS_REGION=us-east-1
export AWS_DEFAULT_REGION=$AWS_REGION
export CDK_DOCKER=finch
export DEPLOYMENT_CONFIG=config/production.local.json

aws sso login --profile "$AWS_PROFILE"
aws sts get-caller-identity
finch vm start

cd cdk
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
npm ci

npx cdk bootstrap
npx cdk synth -c deployment_config="$DEPLOYMENT_CONFIG"
npx cdk deploy -c deployment_config="$DEPLOYMENT_CONFIG"
```

Confirm the SNS subscription and record all CloudFormation outputs in the
deployment system.

### 4. Corporate IdP and UI

The backend accepts any OIDC issuer/JWKS configuration. The browser UI must
also obtain temporary AWS credentials to SigV4-sign the `AWS_IAM` Function
URL. The sample automatically creates that Identity Pool only for its demo
Cognito pool.

Keep `admin_ui=false` in this stack when `jwt_issuer` is configured. For a
corporate IdP, build and host `admin-ui/` separately, create or reuse a Cognito
Identity Pool that trusts the OIDC provider, grant its authenticated admin role
`lambda:InvokeFunctionUrl`, and provide a deployment-specific `config.js`:

```javascript
window.QUOTA_ADMIN_CONFIG = {
  gatewayUrl: "BROKER_API_URL",
  region: "us-east-1",
  userPoolId: "YOUR_AUTH_PROVIDER_CONFIGURATION",
  userPoolClientId: "YOUR_CLIENT_ID",
  identityPoolId: "YOUR_IDENTITY_POOL_ID"
};
```

The current React login implementation targets Cognito User Pools. Replacing
`admin-ui/src/auth.ts` with the customer's OIDC login is an integration task,
not a change to quota enforcement. The separately hosted UI also requires the
Function URL CORS policy to allow its exact HTTPS origin and the SigV4 headers;
the stack configures this automatically only for its own demo CloudFront
distribution. Do not use a wildcard production origin. The shared admin key is
for trusted CLI or backend use and must never be embedded in the browser.

### 5. Prevent bypass

The quotas have no effect if application users retain another principal that
can call Bedrock directly.

Choose one:

- Attach the stack's `DenyDirectBedrockPolicyArn` to every non-vended role.
- Apply a permission boundary.
- Apply an organizational SCP.

Example SCP decision, with the deployed `BedrockUserRoleArn` as the only
inference exception:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "RequireQuotaBrokerForBedrockRuntime",
      "Effect": "Deny",
      "Action": [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream",
        "bedrock:CallWithBearerToken"
      ],
      "Resource": "*",
      "Condition": {
        "ArnNotEquals": {
          "aws:PrincipalArn": "BEDROCK_USER_ROLE_ARN"
        }
      }
    }
  ]
}
```

Validate any SCP in a non-production OU. Account administrators and
organization administrators remain capable of changing the policy.

### 6. Production acceptance tests

Verify all of the following:

1. Unsigned broker request returns `401` or `403`.
2. Allowed invoker role plus valid JWT receives STS credentials.
3. Invalid issuer, audience, signature, expiry, or claim is rejected.
4. Vended credentials can invoke only approved Runtime resources.
5. A normal application role cannot invoke Bedrock directly.
6. An invocation appears in DynamoDB through the log subscription.
7. Re-delivering the same `requestId` does not increment usage twice.
8. Warning and block SNS notifications arrive.
9. Blocked identities cannot renew credentials.
10. Previously issued credentials work only until STS expiry.
11. Unknown models use the configured conservative fallback.
12. Destroy testing confirms production tables are retained.

## Administrative commands

```bash
# Create
python examples/sigv4_gateway.py \
  --gateway-url "$BROKER_API_URL" --profile "$AWS_PROFILE" \
  --region "$AWS_REGION" --admin-key "$ADMIN_KEY" \
  create-user tenant-acme --name "ACME" \
  --daily-usd 25 --daily-input-tokens 10000000 \
  --daily-output-tokens 2000000

# List
python examples/sigv4_gateway.py \
  --gateway-url "$BROKER_API_URL" --profile "$AWS_PROFILE" \
  --region "$AWS_REGION" --admin-key "$ADMIN_KEY" list-users

# Update all quota dimensions
python examples/sigv4_gateway.py \
  --gateway-url "$BROKER_API_URL" --profile "$AWS_PROFILE" \
  --region "$AWS_REGION" --admin-key "$ADMIN_KEY" \
  update-user tenant-acme --daily-usd 30 \
  --daily-input-tokens 12000000 --daily-output-tokens 2500000

# Block
python examples/sigv4_gateway.py \
  --gateway-url "$BROKER_API_URL" --profile "$AWS_PROFILE" \
  --region "$AWS_REGION" --admin-key "$ADMIN_KEY" \
  block-user tenant-acme --reason "security review"

# Query today's usage
python examples/sigv4_gateway.py \
  --gateway-url "$BROKER_API_URL" --profile "$AWS_PROFILE" \
  --region "$AWS_REGION" --admin-key "$ADMIN_KEY" \
  get-usage tenant-acme

# Unblock
python examples/sigv4_gateway.py \
  --gateway-url "$BROKER_API_URL" --profile "$AWS_PROFILE" \
  --region "$AWS_REGION" --admin-key "$ADMIN_KEY" \
  unblock-user tenant-acme
```

## Destruction

Demo:

```bash
cd cdk
npx cdk destroy -c deployment_config=config/demo.json
```

Production tables use `RETAIN` and survive stack deletion. Managed invocation
logging resources are also retained because the stack cannot restore a prior
account-wide logging configuration. Review and remove retained resources only
through an explicit data-retention and logging-owner decision.
