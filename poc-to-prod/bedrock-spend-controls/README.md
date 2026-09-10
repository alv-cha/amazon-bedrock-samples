# Bedrock Spend Controls

Per-user, per-tenant, and per-workload quotas for Amazon Bedrock Runtime.

This sample adds simultaneous daily, weekly, and monthly calendar quotas to
applications that call the Amazon Bedrock Runtime endpoint. Quotas can be
assigned per user, tenant, or directly invoking workload.

The application authenticates to a small credential broker with its existing
OIDC JWT. If the configured identity is active and under quota, the broker
returns a short-lived STS session restricted to approved Bedrock model or
inference-profile ARNs. The application then calls `bedrock-runtime` directly.

There is no inference proxy and no second Bedrock endpoint.

## Calendar quota periods

Each subject can independently enable daily, weekly, and monthly limits for
estimated USD, input tokens, and output tokens. Every enabled period is
enforced concurrently; reaching any finite limit blocks the subject. `0`
means Unlimited for that one dimension, while `null` disables the entire
period.

Windows are fixed UTC calendars, not rolling intervals:

- Daily: 00:00 UTC through the following day.
- Weekly: Monday 00:00 UTC through the following Monday.
- Monthly: the first day at 00:00 UTC through the first of the next month.

Daily usage rows remain the canonical ledger. Current weekly and monthly totals
are derived with a strongly consistent query over at most 37 retained daily
rows. This makes enabling a longer-period limit mid-period include usage that
was recorded before the limit existed, with no backfill or dual-write cutover.
Deployments must retain at least 31 days of usage. Subjects with only daily
limits retain the same accounting semantics; longer-period evaluation adds a
small DynamoDB read cost on each vend and metered invocation.

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

Enforcement is layered — there are no modes to choose. Every deployment
carries all three layers, and each can only shorten effective access:

| Layer | Always on | What it does |
|---|---|---|
| Permission lease | yes | Every vended credential embeds an immutable session-policy deadline (60, 300, or 900 s — a **runtime dial**, changed via `PUT /admin/enforcement` with no redeploy) |
| Active-session revocation | yes | Managed-policy shards deny blocked `aws:SourceIdentity` values after IAM propagation, cutting in-flight sessions before their lease ends |
| Emergency stop | yes | Operator-confirmed break-glass: closes vending and applies a role-wide deny |

When metering blocks an identity, two independent races start: the next
lease refresh is refused (bounded by the lease window), and the revocation
layer strips the identity's access (bounded by IAM propagation, usually
seconds). Whichever ends first wins; overspend is limited to
`metering lag + min(lease remainder, deny propagation)`. Both paths
converge from the same version-guarded user row, so they can never
contradict each other.

`AssumeRole` still has a 15-minute minimum. The keys last at least 15
minutes but their embedded session policy contains an earlier
`aws:CurrentTime` deadline. Extending that permission requires another broker
check and STS session. The lazy credential provider caches one set for all
Bedrock calls in the interval; it does not call `AssumeRole` per inference.

The Lambda broker itself uses role chaining, so `vended_ttl_seconds` is limited
to 3,600 seconds. An eight-hour session is rejected rather than synthesizing a
configuration that fails at runtime.

IAM updates are eventually consistent. The revocation layer reports
propagation and capacity failures and falls back to the lease deadline. An
operator-confirmed emergency stop closes new vending before applying a
separate role-wide deny. Already-authorized streams may finish.

If a workload requires a strict decision before every inference request, it
must place an enforcement component in the inference data path. That remains
outside this direct-to-Runtime architecture.

## Components

| Component | Responsibility |
|---|---|
| Broker/admin Lambda | JWT validation, quota check, logical lease, STS vending, admin API |
| BedrockUserRole | Runtime-only permissions restricted by model ARN and permissions boundary |
| Users table | Identity, status, limits, logical leases, session maps, control state |
| Usage table | Canonical daily ledger and invocation idempotency markers |
| Admin audit table | Routine mutation audit events and idempotency records (365-day retention) |
| Invocation logging | Trusted principal ARN, model, request ID, and tokens |
| Usage processor | Event-driven pricing, deduplication, counters, blocking, detection-lag metric |
| Revocation processor | Always-on sharded `SourceIdentity` deny reconciliation |
| Emergency processor | Operator-controlled role-wide deny state machine |
| CloudWatch/SNS | Operational metrics, alarms, warnings, and block notifications |
| Admin UI | Overview with per-model usage charts, user management, runtime enforcement controls, live leases, emergency stop, alarms, and audit history |

The admin UI uses Cognito managed login with authorization-code + PKCE, then
exchanges the current ID token through the Identity Pool for temporary AWS
credentials. It remains an `AWS_IAM`/SigV4 client; the browser never receives
the shared routine admin secret. The break-glass key is entered by an operator
only when confirming an emergency action, is sent with that one request, and
is never persisted by the console. The same secretless User Pool client and
JWT audience retain `USER_SRP_AUTH` and `USER_PASSWORD_AUTH` for the
notebook/CLI programmatic flows.

The Users view uses 25-row server pagination and server-side search/status
filters. It includes a conditional-create wizard, reasoned block/unblock,
versioned calendar-limit editing with optional audit reasons, and a detail
drawer for current daily/weekly/monthly usage, retained history, and per-user
changes. A non-empty reason is required when a period is enabled/disabled, a
positive limit becomes Unlimited, or a submitted finite limit is below current
period usage; other limit reasons remain optional. A separate global Audit
view shows period-qualified administrative changes. Independent failures leave
only the affected view visibly stale.

The Operations tab changes the audited 60/300/900-second permission-lease dial
without a redeploy and exposes the emergency stop behind a separate
break-glass key plus exact confirmation phrase. The broker reads CloudWatch
metrics and alarm state server-side with `GetMetricData` and `DescribeAlarms`;
the browser receives no CloudWatch permissions, secret ARN, or IAM policy
controls. Missing metrics and `INSUFFICIENT_DATA` are displayed as unknown
rather than healthy.

CloudWatch is the observability system. DynamoDB remains necessary because the
broker needs a low-latency quota decision when credentials are requested.

An admin API limit of `0` disables that one dimension and is displayed as
**Unlimited**. A `null` period is disabled. The UI requires explicit
confirmation and a non-empty reason before a positive limit becomes Unlimited,
when changing period enablement, or when a finite limit is below current-period
usage. Reasons are optional for ordinary increases. Daily deployment defaults
are finite; weekly and monthly defaults are disabled.

Routine creates are conditional: an existing identity returns
`409 user_already_exists` and is never overwritten. Every routine mutation
uses a UUID `Idempotency-Key`; limit and status changes also use `If-Match`
with the reviewed ETag/version. Successful writes return the complete canonical
user and a new ETag. The conditional/idempotency headers and complete response
fields are additive for compatible update/status clients, while duplicate
create is intentionally no longer an upsert. Safe clients surface every
version, duplicate, and idempotency conflict for review.
Routine create/limit/status audit begins when the audit-table deployment is
installed, is currently retained for 365 days, and has no historical backfill.

Temporary per-user overrides, bulk operations, user delete, and usage reset
are explicitly outside this MVP.

## Identity

`jwt_user_claim` selects the quota identity:

- `sub` gives each human or workload a separate quota.
- A tenant, team, or project claim shares one quota across all members.

One governance layer supports three quota granularities on the same tables,
admin API, and dashboard:

| Granularity | Mechanism | Target |
|---|---|---|
| Per user | JWT claim (`sub`) via the credential broker or proxy | any app with an IdP |
| Per tenant | JWT with a tenant/team claim (`jwt_user_claim`) | ISV per-tenant caps |
| Per workload | Application inference profile + IAM Deny ([workload mode](#workload-mode)) | SMB and ISV internal workloads with no JWT |

The broker derives a collision-resistant STS session name from the claim.
Bedrock invocation logging captures that session in `identity.arn`; a temporary
DynamoDB reverse map resolves it to the original claim value. Routine JWT
administrative audit uses the verified token `sub` as the human actor when it
is a non-empty string, independent of a tenant/team/project quota claim; it
falls back to the configured quota identity only when `sub` is unavailable.

`requestMetadata` is useful for analysis but is caller-controlled and is not
trusted for enforcement attribution.

## Workload mode

Workload mode puts a budget on applications that call `bedrock-runtime`
directly with their own IAM credentials — no JWT, no vend flow, zero client
code change. Each configured workload gets:

- A dedicated **application inference profile**. The app invokes with the
  profile ARN as `modelId`; invocation-log records preserve that ARN, which
  is the attribution key (verified empirically against the log schema).
- An **invoke policy** that pins the workload's IAM role to its own profile
  (an allow on the profile plus a `bedrock:InferenceProfileArn` condition on
  the routed models). With `role_arn` configured the stack attaches it
  directly; without it the policy is emitted as a stack output to attach
  manually.
- A quota row `workload:<name>` in the same users table, with the same
  limits schema, windows, admin endpoints, and dashboard as JWT identities.
- **Enforcement**: when the budget is exhausted the metering processor blocks
  the row, and the workload enforcer Lambda attaches an inline
  `bedrock:InvokeModel*` Deny to the workload's role (users-table stream fast
  path plus a 5-minute repair schedule). The Deny applies to already-issued
  STS sessions after IAM propagation. On window reset the enforcer lifts
  automatic blocks only after every enabled current period is under quota, then
  removes the Deny. Manual admin blocks never auto-lift.

Configure workloads in `cdk/config/workloads.json` (see
`cdk/config/workloads.example.json`) and pass `-c workloads=config/workloads.json`
or the `workloads` key of `deployment_config`. Workloads without `role_arn`
are metered and alerted but cannot be hard-blocked; they surface as
`enforcement_ready: false` in the admin API and as "metering only" in the UI.

Enforcement latency is metering lag plus IAM propagation or the remaining
permission lease, whichever cuts access first. It remains bounded overspend;
there is no selectable enforcement mode.

## Metering

CloudWatch Logs invokes the usage processor for each Bedrock invocation log.
The processor:

1. Accepts records from the vended role (session attribution) and from
   managed application inference profiles (workload attribution).
2. Resolves the STS session to the quota identity, or the profile ARN to its
   `workload:<name>` row (auto-provisioned with deploy defaults on first
   usage and priced by the profile's underlying model).
3. Prices input and output tokens.
4. Creates a `requestId` idempotency marker.
5. Updates the canonical daily ledger in the same DynamoDB transaction.
6. Derives all current enabled calendar totals from retained daily rows.
7. Emits CloudWatch EMF metrics.
8. Sends period-qualified warnings or blocks when any limit is reached.

There is no periodic Logs Insights scan or EventBridge reconciler.

Invocation logging delivery is at-least-once. Transactional deduplication
prevents retries from charging a request twice.

## USD estimates

Prices are captured once during deployment from AWS Price List. There is no
periodic price refresh.

- GPT OSS standard on-demand prices are resolved dynamically for the stack
  Region. In `us-east-1`, the official Price List API currently returns
  `$0.07/$0.30` per MTok for 20B and `$0.15/$0.60` for 120B
  (input/output).
- Claude Opus 4.7 is billed through AWS Marketplace and uses explicit profile
  mappings: direct/global `$5/$25` per MTok and US geographic profile
  `us.anthropic.claude-opus-4-7` `$5.50/$27.50` per MTok.
- Model and inference-profile IDs are matched literally. A known profile must
  have its exact ID in the snapshot; prefixes such as `us.` and `global.` are
  not stripped or inferred.
- Unknown model IDs use the configured conservative `$15/$75` fallback. This
  is a conservative estimate, not the advertised price of Opus 4.7 or a
  guaranteed upper bound.

Authoritative references: [Amazon Bedrock pricing](https://aws.amazon.com/bedrock/pricing/),
[Claude Opus 4.7 model card](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-anthropic-claude-opus-4-7.html),
and [Global cross-Region inference pricing behavior](https://docs.aws.amazon.com/bedrock/latest/userguide/global-cross-region-inference.html).

### Export the complete regional price catalog

`tools/bedrock_price_catalog.py` downloads the official public AWS Price List
bulk offer and exports every model-related price dimension, grouped by Region.
It requires no AWS credentials and preserves token, image, video, request,
hourly, and model-month units instead of assuming that every price is per
input/output token.

```bash
# All model prices in all published Regions.
python3 tools/bedrock_price_catalog.py \
  --output /tmp/bedrock-prices.json

# One Region, as CSV.
python3 tools/bedrock_price_catalog.py \
  --region eu-south-2 \
  --format csv \
  --output /tmp/bedrock-prices-eu-south-2.csv

# Only rows that can feed the current input/output token calculator.
python3 tools/bedrock_price_catalog.py \
  --region us-east-1 \
  --metering-compatible \
  --output /tmp/bedrock-metering-prices-us-east-1.json
```

The export includes the AWS catalog version and publication timestamp. The
`metering_compatible` marker means that a row has a standard on-demand USD
input/output token rate; it does not establish a mapping from the Price List
model name to a Bedrock Runtime model or inference-profile ID. Keep those
aliases explicit in `cdk/config/model-pricing.json`.

At deployment, the fallback is raised to at least the highest input and output
rates in the known snapshot. It can therefore overestimate an unknown model
and block a user earlier than the final AWS bill. It is not a universal billing
upper bound: production must review it when allowing a more expensive model or
a modality that is not priced by input/output tokens. A daily scheduled
refresh keeps catalog prices current in an SSM parameter, and any request
priced by the fallback raises the `pricing_fallback` alarm instead of being
silently tarified. Updating prices
affects only future invocation events; existing daily DynamoDB aggregates and
blocked status are not repriced automatically.

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
cd poc-to-prod/bedrock-spend-controls/admin-ui
npm ci
npm run build

cd ../cdk
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
npm ci

export AWS_PROFILE=your-profile
export AWS_REGION=us-east-1
export AWS_DEFAULT_REGION=$AWS_REGION

aws sso login --profile "$AWS_PROFILE"       # only for SSO profiles
npx cdk bootstrap
npx cdk synth -c deployment_config=config/demo.json
npx cdk deploy -c deployment_config=config/demo.json
```

Use [DEPLOYMENT.md](DEPLOYMENT.md) for the complete demo and production
workflows, logging ownership, IdP configuration, UI setup, IAM/SCP decision,
smoke tests, and cleanup.

Use [DEMO.md](DEMO.md) as the presentation runbook and
[`notebook/spend_controls_demo.ipynb`](notebook/spend_controls_demo.ipynb) for
the executable capability walkthrough. The notebook uses GPT OSS 20B
`Converse`, displays actual response usage, waits for invocation-log metering,
proves automatic quota rejection, raises the limits, and proves credential
vending recovers. `CountTokens` is only an optional model-dependent
diagnostic; it is not part of enforcement.

## Administrative client

The SigV4 client manages every period/dimension limit. Routine writes generate a UUID
`Idempotency-Key`; `update-user`, `block-user`, and `unblock-user` first fetch
the exact user and send its ETag (or canonical integer version) as `If-Match`.
`update-user` accepts an optional `--reason` and omits the field when not
supplied. A concurrent change therefore returns `409 version_conflict` instead
of being silently overwritten. Create is conditional and returns `409
user_already_exists` without changing the existing row.

```bash
python examples/sigv4_gateway.py \
  --gateway-url "$BROKER_API_URL" \
  --profile "$AWS_PROFILE" \
  --region "$AWS_REGION" \
  --admin-key "$ADMIN_KEY" \
  create-user alice \
  --daily-usd 5 \
  --daily-input-tokens 1000000 \
  --daily-output-tokens 200000 \
  --weekly-usd 25 \
  --weekly-input-tokens 5000000 \
  --weekly-output-tokens 1000000
```

The notebook uses the same safe routine-write flow and performs an exact GET
before deciding whether to create, so rerunning it does not rely on duplicate
POST upsert behavior. Status changes and the baseline, low-quota, recovery,
and controlled stress limit writes carry explicit reasons. Limit reasons are
optional for compatible API and CLI clients; omitted or blank values use the
standardized legacy audit fallback.

The canonical exact-user route family is:

- `GET /admin/user?user_id=<encoded>` for detail.
- `PUT /admin/user/limits?user_id=<encoded>` for limits.
- `PUT /admin/user/status?user_id=<encoded>` for status.
- `GET /admin/user/usage?user_id=<encoded>&period=weekly&window=...` for a
  calendar usage window (`period` defaults to `daily`).
- `/admin/user/usage-history` and `/admin/user/audit` with the same `user_id`
  query parameter for retained history and per-user audit.

First-party clients pass the raw identity as a request parameter and let the
HTTP client encode it once before SigV4 signing. The legacy-compatible
`/admin/users/{id}` route and its suffixes remain available, but are ambiguous
for path-like identities containing values such as `/audit` or `/usage` and
are not the preferred first-party form.

User listing supports server cursors plus status/search filters; the UI fixes
its page size at 25. Successful mutations return the complete canonical user,
`ETag`, and `X-Request-Id`. The global Audit view has an explicit refresh and
its own last-successful freshness/error state. Routine audit retention is 365
days from event creation and begins at deployment—earlier changes are not
backfilled.

Commands: `create-user`, `list-users`, `update-user`, `block-user`,
`unblock-user`, `get-usage`, `emergency-stop`, and `emergency-recover`.
Emergency commands use a separate route and require the separate
`EmergencyKeySecretArn`, a reason, and the API's explicit confirmation phrase;
routine admin/conditional/idempotency headers are not sent to that route.
Triggering one affects every vended session and must be an operator break-glass
decision.

## Repository layout

| Path | Purpose |
|---|---|
| `cdk/` | Validated configuration and AWS infrastructure |
| `gateway/` | Broker and administrative control-plane API |
| `usage_processor/` | Invocation-log subscription consumer |
| `workload_enforcer/` | Workload-mode IAM Deny convergence Lambda |
| `revocation_processor/` | Optional sharded per-user IAM deny reconciler |
| `emergency_processor/` | Operator-controlled shared-role deny controller |
| `admin-ui/` | Static React administration console |
| `examples/` | SigV4 admin, lazy credential provider, and Runtime examples |
| `spikes/` | Guarded non-production qualification probes and results |
| `notebook/` | Complete deployed capability walkthrough |
| `tests/` | Unit, API, infrastructure, notebook, and pricing tests |
