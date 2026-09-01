# Per-user quotas for Amazon Bedrock Runtime

This sample adds per-user or per-tenant daily quotas to applications that call
the Amazon Bedrock Runtime endpoint.

The application authenticates to a small credential broker with its existing
OIDC JWT. If the configured identity is active and under quota, the broker
returns a short-lived STS session restricted to approved Bedrock model or
inference-profile ARNs. The application then calls `bedrock-runtime` directly.

There is no inference proxy and no second Bedrock endpoint.

## Architecture

[Editable architecture diagram](assets/architecture.drawio)

```text
User JWT -> AWS_IAM broker -> DynamoDB quota check -> short-lived STS
                                                        |
Application --------------------------------------------+
             direct SigV4 call to bedrock-runtime
                                                        |
Bedrock invocation logs -> CloudWatch Logs subscription -> usage processor
                                                        |
                                 DynamoDB + CloudWatch + SNS
```

The solution supports the Runtime APIs authorized by
`bedrock:InvokeModel` and `bedrock:InvokeModelWithResponseStream`, including
Converse, InvokeModel, token counting, and streaming where supported by the
selected model. Runtime API/model compatibility remains model- and
Region-specific.

## Enforcement guarantee

This remains **bounded-overspend enforcement**, not a synchronous hard cap.
The broker never inspects inference requests. Exposure is:

```text
invocation-log delivery latency
+ usage-processing latency
+ post-detection permission cutoff
+ concurrent or already-authorized requests
```

The credential broker supports three explicit modes:

| Mode | STS session | Post-detection behavior | Qualification |
|---|---:|---|---|
| `legacy` | 15–60 minutes | Blocks renewal; existing access ends at STS expiry | Default, existing behavior |
| `lease` | 15 minutes | An immutable session policy ends Bedrock permission after 1, 5, or 15 minutes | Implemented; keep opt-in until the sandbox probe passes |
| `revocation` | 60 minutes | Managed-policy shards deny blocked `aws:SourceIdentity` values after IAM propagation | Experimental; requires sandbox latency/capacity qualification |

`AssumeRole` still has a 15-minute minimum. In lease mode, the keys last 15
minutes but their embedded session policy contains an earlier
`aws:CurrentTime` deadline. Extending that permission requires another broker
check and STS session. The lazy credential provider caches one set for all
Bedrock calls in the interval; it does not call `AssumeRole` per inference.

The Lambda broker itself uses role chaining, so `vended_ttl_seconds` is limited
to 3,600 seconds. An eight-hour session is rejected rather than synthesizing a
configuration that fails at runtime.

IAM updates are eventually consistent. Revocation mode reports propagation and
capacity failures and falls back to the 60-minute session deadline. An
operator-confirmed emergency stop closes new vending before applying a separate
role-wide deny. Already-authorized streams may finish in every mode.

If a workload requires a strict decision before every inference request, it
must place an enforcement component in the inference data path. That remains
outside this direct-to-Runtime architecture.

## Components

| Component | Responsibility |
|---|---|
| Broker/admin Lambda | JWT validation, quota check, logical lease, STS vending, admin API |
| BedrockUserRole | Runtime-only permissions restricted by model ARN and permissions boundary |
| Users table | Identity, status, limits, logical leases, session maps, control state |
| Usage table | Daily aggregates and invocation idempotency markers |
| Invocation logging | Trusted principal ARN, model, request ID, and tokens |
| Usage processor | Event-driven pricing, deduplication, counters, blocking, detection-lag metric |
| Revocation processor | Optional sharded `SourceIdentity` deny reconciliation |
| Emergency processor | Operator-controlled role-wide deny state machine |
| CloudWatch/SNS | Operational metrics, alarms, warnings, and block notifications |
| Admin UI | User controls plus read-only enforcement, emergency, revocation, alarm/DLQ, and qualification status |

The admin UI's Operations panel is read-only. The broker reads CloudWatch
metrics and alarm state server-side with `GetMetricData` and `DescribeAlarms`;
the browser receives no CloudWatch permissions, emergency key, secret ARN, IAM
policy controls, or emergency/revocation mutation buttons. Missing metrics and
`INSUFFICIENT_DATA` are displayed as unknown rather than healthy.

CloudWatch is the observability system. DynamoDB remains necessary because the
broker needs a low-latency quota decision when credentials are requested.

An admin API limit of `0` disables that one dimension. Declarative deployment
defaults must be positive so auto-provisioning cannot create unlimited users.

## Identity

`jwt_user_claim` selects the quota identity:

- `sub` gives each human or workload a separate quota.
- A tenant, team, or project claim shares one quota across all members.

The broker derives a collision-resistant STS session name from the claim.
Bedrock invocation logging captures that session in `identity.arn`; a temporary
DynamoDB reverse map resolves it to the original claim value.

`requestMetadata` is useful for analysis but is caller-controlled and is not
trusted for enforcement attribution.

## Metering

CloudWatch Logs invokes the usage processor for each Bedrock invocation log.
The processor:

1. Accepts only records from the vended role.
2. Resolves the STS session to the quota identity.
3. Prices input and output tokens.
4. Creates a `requestId` idempotency marker.
5. Updates the daily aggregate in the same DynamoDB transaction.
6. Emits CloudWatch EMF metrics.
7. Sends a warning or blocks the identity when a limit is reached.

There is no periodic Logs Insights scan or EventBridge reconciler.

Invocation logging delivery is at-least-once. Transactional deduplication
prevents retries from charging a request twice.

## USD estimates

Prices are captured once during deployment from AWS Price List. There is no
periodic price refresh.

- GPT OSS standard on-demand prices are resolved dynamically.
- Models that cannot be resolved can use explicit overrides.
- Unknown model IDs use the configured conservative fallback.

At deployment, the fallback is raised to at least the highest input and output
rates in the known snapshot. It can therefore overestimate an unknown model
and block a user earlier than the final AWS bill. It is not a universal billing
upper bound: production must review it when allowing a more expensive model or
a modality that is not priced by input/output tokens.

USD quotas are estimates against the deployed catalog, not a billing
guarantee. Prompt caching, service tiers, provisioned throughput, tools, and
other separately billed features can require additional pricing logic. Token
quotas remain independent of the USD estimate.

## Security boundaries

- The broker Function URL always uses `AWS_IAM`.
- The broker role cannot invoke a model; only the vended role can.
- The vended role receives only the required Runtime actions.
- A managed permissions boundary caps the vended role at the configured
  Bedrock actions and model resources, including when optional policy-writing
  controllers are enabled.
- Lease session policies can only reduce the role's model allowlist; their
  `Resource: "*"` cannot grant access absent from the role and boundary.
- Revocation and emergency processors can version only their designated
  pre-attached policies. They cannot edit the role, trust policy, or boundary.
- `allowed_model_arns` controls models and inference profiles at IAM.
- Long-term and short-term Bedrock bearer keys are not vended. The
  `bedrock:CallWithBearerToken` permission requires `Resource: "*"` and would
  weaken the model ARN allowlist.
- Production must restrict `invoker_principal_arns`.
- Production must prevent other principals from retaining direct Bedrock
  permissions.

The stack creates `DenyDirectBedrockPolicyArn` as an attachment helper.
Organizations can instead enforce an SCP or permission boundary. Account
administrators can still change IAM/SCP policy; this sample cannot constrain
the management plane.

## Quickstart

```bash
cd poc-to-prod/bedrock-per-user-quotas/admin-ui
npm ci
npm run build

cd ../cdk
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
npm ci

export AWS_PROFILE=your-profile
export AWS_REGION=us-east-1
export AWS_DEFAULT_REGION=$AWS_REGION
export CDK_DOCKER=finch

aws sso login --profile "$AWS_PROFILE"       # only for SSO profiles
finch vm start
npx cdk bootstrap
npx cdk synth -c deployment_config=config/demo.json
npx cdk deploy -c deployment_config=config/demo.json
```

Use [DEPLOYMENT.md](DEPLOYMENT.md) for the complete demo and production
workflows, logging ownership, IdP configuration, UI setup, IAM/SCP decision,
smoke tests, and cleanup.

Use [DEMO.md](DEMO.md) as the presentation runbook and
[`notebook/per_user_quota_demo.ipynb`](notebook/per_user_quota_demo.ipynb) for
the executable capability walkthrough. The notebook uses GPT OSS 20B
`Converse`, displays actual response usage, waits for invocation-log metering,
proves automatic quota rejection, raises the limits, and proves credential
vending recovers. `CountTokens` is only an optional model-dependent
diagnostic; it is not part of enforcement.

## Administrative client

The existing SigV4 client manages all three limits:

```bash
python examples/sigv4_gateway.py \
  --gateway-url "$BROKER_API_URL" \
  --profile "$AWS_PROFILE" \
  --region "$AWS_REGION" \
  --admin-key "$ADMIN_KEY" \
  create-user alice \
  --daily-usd 5 \
  --daily-input-tokens 1000000 \
  --daily-output-tokens 200000
```

Commands: `create-user`, `list-users`, `update-user`, `block-user`,
`unblock-user`, `get-usage`, `emergency-stop`, and `emergency-recover`.
Emergency commands require the separate `EmergencyKeySecretArn`, a reason,
and the API's explicit confirmation phrase; routine admin keys/JWTs cannot
invoke them. Triggering one affects every vended session and must be an
operator break-glass decision.

## Repository layout

| Path | Purpose |
|---|---|
| `cdk/` | Validated configuration and AWS infrastructure |
| `gateway/` | Broker and administrative control-plane API |
| `usage_processor/` | Invocation-log subscription consumer |
| `revocation_processor/` | Optional sharded per-user IAM deny reconciler |
| `emergency_processor/` | Operator-controlled shared-role deny controller |
| `admin-ui/` | Static React administration console |
| `examples/` | SigV4 admin, lazy credential provider, and Runtime examples |
| `spikes/` | Guarded non-production qualification probes and results |
| `notebook/` | Complete deployed capability walkthrough |
| `tests/` | Unit, API, infrastructure, notebook, and pricing tests |
