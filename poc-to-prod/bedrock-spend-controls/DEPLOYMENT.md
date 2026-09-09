# Deployment guide

This is the canonical deployment guide for the runtime-only per-user quota
sample.

## Decisions before deployment

| Decision | Demo/personal account | Production/shared account |
|---|---|---|
| Quota identity | JWT `sub` | Stable user, tenant, team, or project claim |
| IdP | Stack-created Cognito | Existing OIDC IdP |
| Enforcement | `legacy` until lease probe passes | Qualified lease mode; revocation remains experimental until propagation tests pass |
| Credential lifetime | 900-second STS; optional 60/300/900-second permission lease | 900–3600-second STS; no role-chained session above one hour |
| Runtime models | `*` for exploration | Explicit model and inference-profile ARNs |
| Invocation logging | Stack managed | Reuse centrally managed logging |
| Log subscription | Dedicated demo group | Confirm subscription-filter capacity and ownership |
| Auto-provisioning | Enabled | Usually disabled |
| Function URL callers | Account default | Explicit backend/admin role ARNs |
| Usage retention | 35 days | Policy-defined value |
| Routine admin audit | Fixed 365 days from deployment; no backfill | Fixed 365 days from deployment; no backfill |
| DynamoDB deletion | `DESTROY` | `RETAIN` |
| Alerts | Personal email | Operations topic/email |
| Admin UI | Stack-created Cognito path | Integrate corporate IdP and Identity Pool |
| Direct Bedrock access | Optional demo deny policy | Required SCP, boundary, or equivalent deny |
| Price fallback | Conservative default | Review against most expensive allowed model |

The hard architectural decision is the enforcement guarantee. This sample
does not inspect each inference request. `legacy` sessions remain usable until
STS expiry; `lease` mode embeds an earlier immutable permission deadline; and
experimental `revocation` mode depends on eventually consistent IAM policy
propagation. Already-authorized streams may finish.

## Runtime-only request flow

1. The application obtains a JWT from Cognito or its own OIDC IdP.
2. An authorized AWS principal SigV4-signs `POST /v1/credentials` to the
   broker's `AWS_IAM` Function URL and sends the JWT in
   `X-Quota-User-Token`.
3. The broker verifies issuer, audience, signature, expiry, and the configured
   identity claim.
4. DynamoDB provides status, limits, and the latest daily usage.
5. The broker reserves one logical lease, assumes `BedrockUserRole`, and
   stamps a collision-resistant session identity. STS keys last at least 15
   minutes; lease mode can end Bedrock permission after 1, 5, or 15 minutes.
6. A lazy refresh-aware provider caches that credential set for all Runtime
   calls until the refresh window. It calls the broker/STS once per lease, not
   once per inference.
7. The application calls `bedrock-runtime` directly with those credentials.
8. Bedrock sends model invocation logs to CloudWatch Logs.
9. A subscription invokes the usage processor.
10. The processor deduplicates by Bedrock `requestId`, prices tokens, updates
    DynamoDB, emits metrics, and sends warnings/blocks.
11. A blocked identity cannot obtain another logical lease. Optional revocation
    reconciles existing sessions after IAM propagation.

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
| `vended_ttl_seconds` | `900` | 900–3600; Lambda broker role chaining rejects longer sessions |
| `credential_enforcement_mode` | `legacy` | `legacy`, `lease`, or opt-in `revocation` |
| `permission_lease_seconds` | `300` | `60`, `300`, or `900`; effective Bedrock permission, not STS lifetime |
| `refresh_overlap_seconds` | `10` | Positive and less than permission lease |
| `refresh_jitter_seconds` | `5` | Non-negative and less than refresh overlap |
| `vend_rate_limit_per_minute` | `6` | Positive per-user attempts, including retries |
| `revocation_policy_shards` | `19` | Immutable in revocation mode; plus emergency policy = 20 role attachments |
| `revocation_reconcile_minutes` | `5` | Positive periodic repair interval in revocation mode |
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
| `workloads` | empty | Workload-mode roster (inline JSON or file path); see [Workload mode](#workload-mode-per-workload-quotas) |

### Safe routine administration

The routine API remains behind the `AWS_IAM` Function URL. Browser requests are
SigV4-signed with temporary Identity Pool credentials and carry the current ID
token in `X-Quota-User-Token`; trusted programmatic clients may use the shared
routine key. The demo browser authenticates with Cognito managed login using an
authorization-code + PKCE flow. Its secretless app client still enables
`USER_SRP_AUTH` and `USER_PASSWORD_AUTH` for the notebook/CLI and keeps the
same client ID as the gateway JWT audience.

Routine create is conditional. `POST /admin/users` returns `409
user_already_exists`, the current user, and its ETag rather than overwriting an
existing row. Every safe mutation sends a UUID `Idempotency-Key`; limit and
status changes also send `If-Match` with the reviewed ETag/version. Successful
writes return the complete canonical user plus `ETag` and `X-Request-Id`.
Duplicate-content replay is safe; changed content under the same key,
duplicate create, and stale version each have a distinct `409` code. The new
headers and complete response fields are additive for compatible clients, but
clients that want concurrency/retry safety must send and retain them.

The dedicated `AdminAuditTable` stores routine create, limit, and status audit
events plus mutation idempotency records. Limit events persist a trimmed
operator reason when supplied; omitted or blank reasons from compatible legacy
clients use a standardized fallback. The broker currently sets their TTL to
365 days. Collection starts when this deployment is installed; earlier
administrative changes do not exist and are not backfilled. This is distinct
from `usage_retention_days`, which bounds usage history.

`GET /admin/users` supports bounded server pages, opaque cursors, status, and
search filters. Canonical exact-user operations use query routes:

- `GET /admin/user?user_id=<encoded>` for detail.
- `PUT /admin/user/limits?user_id=<encoded>` for limits.
- `PUT /admin/user/status?user_id=<encoded>` for status.
- `GET /admin/user/usage?user_id=<encoded>&window=...` for usage.
- `GET /admin/user/usage-history?user_id=<encoded>` for retained history.
- `GET /admin/user/audit?user_id=<encoded>` for per-user audit.

First-party clients pass the raw identity through request parameters so it is
encoded once before signing. `/admin/users/{id}` and its exact-user suffixes
remain legacy-compatible, but path-like identities containing values such as
`/audit` or `/usage` are ambiguous in that form. Global audit remains
`GET /admin/audit`. The UI uses 25-row pages, gives global Audit an explicit
refresh, and preserves independent last-successful freshness/error state for
each surface.

A limit of `0` means **Unlimited** for that one dimension. The UI requires
explicit confirmation plus a non-empty reason when a positive limit becomes
Unlimited. It also requires a reason for a submitted finite limit below
current usage; other limit reasons are optional. Deployment defaults remain
positive. Temporary overrides, bulk operations, browser emergency mutation,
user delete, and usage reset are not part of this MVP.

### Read-only Operations panel

`GET /admin/operations` uses the routine admin authorization path and powers a
read-only GUI panel. It combines deployed credential configuration, normalized
emergency convergence state, conservative qualification metadata, p95
`DetectionLagMilliseconds`, revocation freshness/failure/overflow metrics, and
CloudWatch alarm states—including the emergency/revocation DLQ alarms.

CloudWatch reads are performed by the broker role using only
`cloudwatch:GetMetricData` and `cloudwatch:DescribeAlarms`. The browser still
has only Function URL invocation permission. The response and UI never include
the emergency key, secret ARN/value, IAM policy ARNs, incident reason, request
ID, or mutation controls. If CloudWatch is denied or has no data, the endpoint
returns local state with `unavailable`, `unknown`, `not_applicable`, or
`INSUFFICIENT_DATA`; it does not label missing telemetry healthy and does not
break user quota administration.

Qualification shown in the panel is reviewed deployment metadata, not inferred
from selected mode or alarm health. `legacy` remains baseline, while lease,
revocation, and emergency live qualification remain pending until recorded in
`spikes/QUALIFICATION.md`.

Legacy `mode_a_allowed_model_arns` is accepted as an alias for
`allowed_model_arns`. Former dual-mode keys synthesize only for migration and
are ignored with a warning. Remove them.

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

There is no hard pre-spend cap after this migration. Every mode remains
bounded overspend. Keep `credential_enforcement_mode=legacy` until the guarded
sandbox probe in `spikes/lease_revocation_probe.py` validates lease expiration
for the selected models/Region. Keep `revocation` experimental until targeted
isolation and IAM propagation remain below the required cutoff across the
recorded sample set.

### Lease and revocation qualification

A one- or five-minute **permission lease** is not a shorter STS credential. The
broker passes an inline session policy with `DateLessThan` on
`aws:CurrentTime`; the role policy, permissions boundary, and session policy
are intersected. Renewal requires a new broker quota check and STS session.
Use `examples/refreshable_bedrock.py` so normal Bedrock calls share the cached
credentials and botocore coalesces concurrent refreshes.

At a 50-second refresh interval, 1,000 continuously active users average 20
STS requests/second, 3,000 average 60, and 10,000 average 200. These are
absolute averages against the documented 600 requests/second regional default,
which is shared with other STS operations. Use lazy refresh, jitter, rate
limits, load testing, and actual account quota measurements.

The guarded probe is dry-run by default:

```bash
cdk/.venv/bin/python spikes/lease_revocation_probe.py \
  --profile YOUR_SANDBOX_PROFILE \
  --role-arn arn:aws:iam::111122223333:role/YOUR_DEDICATED_SANDBOX_ROLE \
  --managed-policy-arn arn:aws:iam::111122223333:policy/YOUR_PREATTACHED_SANDBOX_DENY \
  --region us-east-1 \
  --model-id YOUR_COUNT_TOKENS_MODEL
```

Live mode temporarily versions IAM policy and invokes Bedrock. Run it only
after reviewing the printed account/role and explicitly approving those exact
non-production resources. Record results in `spikes/QUALIFICATION.md`.

The current broker's caller is a Lambda execution-role session, so
`AssumeRole` is role chaining and cannot exceed 3,600 seconds. Eight-hour
configuration is rejected. Supporting it requires a separate first-hop
federation/token-issuer design with bypass and replay analysis.

Revocation mode uses an immutable 19-shard layout plus one emergency managed
policy: 20 role policy attachments in total. Verify that account quota before
deployment. Changing the shard count in place is rejected because rehashing
active identities can create a transient authorization gap; use a separately
reviewed two-policy-set migration instead.

### Emergency stop

`POST /admin/emergency-stop` is an operator action, never an automatic quota
reaction. Activation first puts the DynamoDB control state into `activating`,
which makes the broker return `503`; a separate worker then applies the
unconditional shared-role Bedrock deny and marks the state `active`. Recovery
keeps vending closed in `recovering` until the deny has been replaced by its
no-op policy version.

Retrieve the separate `EmergencyKeySecretArn` only through the approved
break-glass procedure, then export it as `EMERGENCY_ADMIN_KEY`. Routine admin
keys and admin UI JWTs cannot invoke these operations.

```bash
python examples/sigv4_gateway.py \
  --gateway-url "$BROKER_API_URL" --profile "$AWS_PROFILE" \
  --region "$AWS_REGION" --emergency-key "$EMERGENCY_ADMIN_KEY" \
  emergency-stop --reason "security incident"

python examples/sigv4_gateway.py \
  --gateway-url "$BROKER_API_URL" --profile "$AWS_PROFILE" \
  --region "$AWS_REGION" --emergency-key "$EMERGENCY_ADMIN_KEY" \
  emergency-recover --reason "incident resolved"
```

Both commands supply the API's explicit confirmation phrase. Activation can
interrupt every user after IAM propagation; recovery is also eventually
consistent. Review SNS alarms and the emergency-state endpoint before and
after either action.

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

### Workload mode (per-workload quotas)

One governance layer, three quota granularities, one set of tables and
dashboards:

| Granularity | Mechanism | Target |
|---|---|---|
| Per user | JWT (`sub`) via broker or proxy | any app with an IdP |
| Per tenant | JWT with a tenant claim (`jwt_user_claim`) | ISV per-tenant caps |
| Per workload | Application inference profile + IAM Deny | SMB, ISV internal workloads |

Workload mode covers applications that call `bedrock-runtime` directly with
their own IAM role — no JWT, no vend flow, zero client code change. Create
`cdk/config/workloads.json` from the example:

```json
{
  "workloads": [
    {
      "name": "payments-batch",
      "model": "us.anthropic.claude-opus-4-7",
      "role_arn": "arn:aws:iam::111122223333:role/payments-batch-app"
    },
    { "name": "reports-generator", "model": "anthropic.claude-haiku-4-5-20251001-v1:0" }
  ]
}
```

and deploy with `-c workloads=config/workloads.json` (or the `workloads` key
in `deployment_config`). `name` must match `[a-z0-9][a-z0-9-]{0,47}`;
`model` is a foundation-model ID or cross-region inference-profile ID (never
an ARN); `role_arn` is optional but strongly recommended.

Per workload the stack creates an application inference profile named
`spend-controls-<name>` (tagged `bedrock-spend-controls-workload=<name>` for cost
allocation) and outputs its ARN (`WorkloadProfileArn<Name>`). The
application invokes with that ARN as `modelId` — the only change on the
workload side, and it is a configuration value, not code:

```python
bedrock_runtime.converse(modelId="<WorkloadProfileArn output>", ...)
```

**Direct attach (paved road).** With `role_arn`, the stack attaches the
invoke policy to the role: an allow on the workload's own profile plus a
`bedrock:InferenceProfileArn`-conditioned allow on the routed models, so the
role cannot invoke anything except through its profile. The enforcement
Lambda's IAM permissions are scoped to exactly the enrolled role ARNs —
never a wildcard.

**Snippet fallback.** Without `role_arn`, the policy document is emitted as
the `WorkloadPolicySnippet<Name>` output for the customer to attach. The
workload is metered, alerted, and visible in the admin UI, but cannot be
hard-blocked: it reports `enforcement_ready: false` in the admin API and
"metering only" in the UI, and blocked-without-enforcement runs raise the
`WorkloadEnforcementSkipped` metric and an SNS alert. Add `role_arn` and
redeploy to promote it.

**Runtime behavior.** Usage attributed by the profile ARN in invocation-log
records flows into the same `workload:<name>` row, counters, and windows as
JWT identities (auto-provisioned with the deploy default limits on first
usage; priced by the profile's underlying model). On budget exhaustion the
metering processor blocks the row; the workload enforcer (users-table stream
fast path plus a 5-minute schedule) attaches an inline
`bedrock-spend-controls-workload-deny` policy to the role. Expect metering lag
(about 15 s) plus IAM propagation (seconds to about a minute) of bounded
overspend. The Deny applies to sessions the role has already issued. When
the daily window resets, the enforcer lifts automatic blocks and removes the
Deny; admin-origin blocks never lift automatically. All admin operations use
the standard endpoints with `user_id=workload:<name>`, and
`GET /admin/users?granularity=workload` filters the roster.

Scale envelope: 1,000 application inference profiles per account
(adjustable), 1,000 IAM roles per account (adjustable); the deny document is
about 300 bytes against the 10,240-character inline policy limit.

### Price configuration

`config/model-pricing.json` contains:

```json
{
  "catalog_models": {
    "price-list-model-name": ["runtime-model-id"]
  },
  "price_overrides": {
    "anthropic.claude-opus-4-7": {
      "input_per_mtok": 5,
      "output_per_mtok": 25
    },
    "global.anthropic.claude-opus-4-7": {
      "input_per_mtok": 5,
      "output_per_mtok": 25
    },
    "us.anthropic.claude-opus-4-7": {
      "input_per_mtok": 5.5,
      "output_per_mtok": 27.5
    }
  },
  "fallback_price": {
    "input_per_mtok": 15,
    "output_per_mtok": 75
  }
}
```

AWS Price List is queried during deployment and the resulting snapshot seeds
an SSM parameter plus a usage-processor environment fallback. A daily
EventBridge schedule re-resolves catalog prices and rewrites the parameter,
so Pricing API changes reach metering without a redeploy; if Parameter Store
or a refresh fails, metering continues on the deployment snapshot and the
refresh failure surfaces through the Lambda error metric. Current official
standard rates in `us-east-1` are resolved dynamically for GPT OSS
20B (`$0.07/$0.30` input/output per MTok) and 120B (`$0.15/$0.60`). Claude
Opus 4.7 is billed through Marketplace, so this sample pins its exact runtime
IDs: direct/global `$5/$25`, and US geographic profile `$5.50/$27.50` per
MTok. Every `price_overrides` entry must document a `reason` explaining why
the Pricing API cannot price it; synthesis fails otherwise. See
[Amazon Bedrock pricing](https://aws.amazon.com/bedrock/pricing/),
the [Opus 4.7 model card](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-opus-4-7.html),
and [global CRIS pricing behavior](https://docs.aws.amazon.com/bedrock/latest/userguide/global-cross-region-inference.html).

The usage processor first performs an exact lookup of the `modelId` emitted
by invocation logging, so an explicit profile entry (for example the US
geographic uplift) always wins. An unmatched geographic or cross-Region
profile ID (`us.`, `eu.`, `apac.`, `global.`, ...) then resolves to its base
foundation-model price before the conservative fallback applies. Requests
priced by the fallback emit the `FallbackPricedRequests` metric and raise the
`pricing_fallback` alarm shown in the admin console Operations panel.

#### Discover all published regional prices

Use the credential-free exporter from the repository root:

```bash
# Complete JSON catalog, grouped by all Regions published by AWS Price List.
python3 tools/bedrock_price_catalog.py \
  --output /tmp/bedrock-prices.json

# Review the standard token rows relevant to this metering implementation.
python3 tools/bedrock_price_catalog.py \
  --region us-east-1 \
  --metering-compatible \
  --format csv \
  --output /tmp/bedrock-metering-us-east-1.csv
```

The source is the [official AWS Price List bulk offer](https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonBedrock/current/index.json).
The exported metadata records its immutable catalog version and publication
time. Each normalized row retains Region, model, provider, SKU, feature,
service tier, inference/token type, routing, term, unit, currency, unit price,
and the original product attributes.

Do not inject the complete catalog into `MODEL_PRICES_JSON`: it contains many
Regions and incompatible units and can exceed CloudFormation and Lambda
environment limits. Use it for discovery/audit, then map only the exact Runtime
model and inference-profile IDs needed by the deployed Region. `--metering-compatible`
selects the standard on-demand input/output USD token rows and computes
`price_per_million_tokens`, but it deliberately does not guess Runtime IDs.

An unknown ID is charged at the fallback, which deployment raises to at least
the highest input and output rates in the known snapshot. The configured
`$15/$75` fallback is deliberately conservative and is not the Opus 4.7 list
price. It can make USD usage higher than the final bill. It is not a universal
upper bound for a more expensive model or a modality that is not billed by
input/output tokens. Review pricing before adding models, inference profiles,
service tiers, prompt caching, provisioned throughput, image/video generation,
or separately billed tools. Updating pricing changes future events only;
existing DynamoDB daily aggregates and blocked status are not repriced.

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
cd poc-to-prod/bedrock-spend-controls

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
export STACK_NAME=BedrockSpendControls

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

Open the `AdminUiUrl` output. For an admin-UI deployment, expect
`DemoUserPoolId`, `DemoUserPoolClientId`, `AdminIdentityPoolId`, and
`AdminUiUrl` outputs plus an `AdminAuditTable` CloudFormation resource. The
routine audit table is not a substitute for historical records: it begins
collecting at this deployment and retains new events for 365 days.

The deployment writes `config.js` with public identifiers only: broker URL,
Region, User Pool/client, Identity Pool, Cognito managed-login domain, and
Cognito issuer. It contains no client secret, JWT, shared admin key, emergency
key, or AWS credentials. The registered callback is
`AdminUiUrl/auth/callback` and logout returns to `AdminUiUrl/`. Sign in as
`quota-admin` or its verified `admin@example.com` alias; the browser starts an
authorization-code + PKCE managed-login flow. The UI content security policy
must allow both the regional Lambda Function URL destination
`https://*.lambda-url.<region>.on.aws` and the regional Cognito Identity
endpoint `https://cognito-identity.<region>.<AWS URL suffix>` in `connect-src`.
The first permits the signed broker request; the second permits credential
bootstrap before that request.

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

Use a new identity for the create smoke test. Rerunning the same create is an
expected `409 user_already_exists`; it never resets the existing status or
limits. The client automatically adds UUID idempotency and calls
`GET /admin/user?user_id=...` before update/block/unblock. It then uses
`PUT /admin/user/limits?user_id=...` or
`PUT /admin/user/status?user_id=...` with the current `If-Match` value.
`get-usage` uses `GET /admin/user/usage?user_id=...&window=...`. User IDs are
supplied as raw query values and encoded once by the signed HTTP client. Do not
treat duplicate, version, or idempotency conflicts as success.

In the UI, verify 25-row server pagination/search, explicit Unlimited
confirmation, required reasons for Unlimited and finite-below-usage changes,
optional reasons for ordinary limit edits, reasoned status changes, the detail
Usage/Changes views, and the global Audit log. Operations and emergency state
remain read-only.

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
The notebook intentionally uses `USER_PASSWORD_AUTH` with the same secretless
app client/audience used by managed login. It reads exact quota users through
`GET /admin/user?user_id=...`, so reruns load an existing user and only POST on
an explicit 404. Limit/status writes and usage reads use the canonical singular
query routes with raw identities supplied through request parameters. Every
routine write uses UUID idempotency, and each PUT refreshes ETag/version before
sending `If-Match`. Its baseline, low-quota, recovery, and controlled stress
limit writes include explicit audit reasons.

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
cd poc-to-prod/bedrock-spend-controls/cdk
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
  "credential_enforcement_mode": "legacy",
  "permission_lease_seconds": 300,
  "refresh_overlap_seconds": 10,
  "refresh_jitter_seconds": 5,
  "vend_rate_limit_per_minute": 6,
  "revocation_policy_shards": 19,
  "revocation_reconcile_minutes": 5,
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
cd poc-to-prod/bedrock-spend-controls

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
  userPoolId: "YOUR_COGNITO_USER_POOL_ID",
  userPoolClientId: "YOUR_CLIENT_ID",
  identityPoolId: "YOUR_IDENTITY_POOL_ID",
  cognitoDomain: "https://YOUR_DOMAIN.auth.us-east-1.amazoncognito.com",
  cognitoIssuer: "https://cognito-idp.us-east-1.amazonaws.com/YOUR_USER_POOL_ID"
};
```

The current React login implementation targets Cognito User Pools managed
login with authorization-code + PKCE and requires exact HTTPS callback/logout
URLs. Its public `config.js` must contain the Cognito domain and issuer shown
above, but no client secret or other secret. Replacing `admin-ui/src/auth.ts`
with the customer's OIDC login is an integration task, not a change to quota
enforcement. The separately hosted UI also requires the Function URL CORS policy to allow its exact HTTPS origin and the SigV4 headers;
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
9. Blocked identities cannot renew logical leases.
10. In lease mode, new Bedrock authorization fails after the effective
    permission deadline even though `sts_expiration` is later.
11. Detection lag is reported separately from the post-detection cutoff.
12. Revocation mode, when enabled, denies only the targeted `SourceIdentity`,
    reports p50/p95/max propagation, and alarms/falls back on failure.
13. Emergency activation closes vending before its role-wide deny; recovery
    removes the deny before reopening vending.
14. Unknown models use the configured conservative fallback.
15. Destroy testing confirms production tables are retained.
16. Managed login completes authorization-code + PKCE while the same app
    client still issues the notebook's `USER_PASSWORD_AUTH` token/audience.
17. Duplicate create preserves the existing user; stale `If-Match` and changed
    idempotency reuse return distinct `409` errors; an exact replay is stable.
18. Successful routine writes return the complete canonical user/new ETag and
    appear in per-user/global audit. Confirm a supplied limit reason is trimmed
    and persisted, an omitted reason uses the compatibility fallback, and the
    audit window begins at this deployment and expires after the configured
    fixed 365-day period.
19. Server-paginated user search and retained usage/audit history preserve
    cursors and independent stale states.

## Administrative commands

The client generates a new UUID idempotency key for each routine mutation.
Update/block/unblock automatically call `GET /admin/user?user_id=...` first
and send the returned ETag/version as `If-Match` to the canonical limit/status
query route; `get-usage` calls
`GET /admin/user/usage?user_id=...&window=...`. A race still surfaces as an
HTTP failure with conflict guidance. Status commands send a reason. `update-user`
accepts an optional `--reason`, includes it only when supplied, and the backend
stores its trimmed value with the immutable limit audit event. Omitted or blank
reasons use the standardized legacy fallback.

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
  --daily-input-tokens 12000000 --daily-output-tokens 2500000 \
  --reason "Reviewed annual allocation"

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
