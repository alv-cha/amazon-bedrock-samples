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
enforced concurrently; reaching a period's **block threshold** on any finite
limit blocks the subject. `0` means Unlimited for that one dimension, while
`null` disables the entire period.

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

### Thresholds and alert-only budgets

Each enabled period carries an ordered `thresholds` list of
`{"at": <ratio>, "action": "warn" | "block"}` entries. `at` is utilization
(`1.0` = 100 %, up to `10.0`); entries must be strictly increasing; at most
one `block` is allowed and it must be last. The metering processor sends one
SNS warning per `warn` level per calendar window (idempotency markers are
per level, so crossing 50 % then 80 % sends two distinct messages and a
duplicate delivery sends none) and blocks only when the `block` level is
reached — which may be above 100 % (`{"at": 1.2, "action": "block"}`).

A list with **no `block` entry is an alert-only budget**: it warns at every
configured level but never blocks the subject, however far over 100 % it
runs. The admin UI flags such periods and requires a reason when a period
becomes alert-only.

Rows created before thresholds existed, and periods submitted without a
list, resolve to the deployment default
`[{"at": <warn_threshold>, "action": "warn"}, {"at": 1.0, "action": "block"}]`,
so existing deployments change behaviour only when an operator configures a
list. `default_limits.<period>.thresholds` sets the list applied to
auto-provisioned and admin-created subjects.

### Rate limits (rpm / tpm)

A subject may also carry `rate: {"rpm": N, "tpm": N}` — requests and
uncached input + output tokens per UTC minute, counted from metered
invocations (not credential vends; the separate
`vend_rate_limit_per_minute` still bounds broker calls). `0` disables a
dimension. The processor increments a short-lived per-minute counter
(`RATE#<subject>` / `<UTC minute>` in the usage table) only for subjects
with a rate limit; reaching either limit blocks the subject through the
same automatic path as a calendar breach with a distinct reason
(`auto: rpm rate limit reached in minute ...`) and SNS subject
(`BLOCKED <subject> reason=rpm`). Rate blocks are `status_origin:
automatic`, so they lift on their own once the current minute is under the
limit — at the next vend, the workload enforcer's 5-minute pass, or the
nightly auto-block sweep (00:05 UTC) for users who never come back; no
manual unblock is needed.
Cached tokens never count toward `tpm`. `default_limits.rate` sets the
deployment default.

### Per-model budgets

A subject may carry, next to its subject-level limits, zero or more budgets
keyed by model or inference-profile ID (as it appears in the invocation log:
`us.anthropic.claude-opus-4-7`, never an ARN). Each model budget has the
same daily/weekly/monthly + thresholds shape as the subject limits — "user X
gets $10/day in total but only $2/day of that may go to Opus". Rate limits
are subject-level only.

The usage processor writes a **second daily ledger row per (subject, day,
model)** — `<subject>#model#<model_id>` in the usage table — in the same
`TransactWriteItems` as the subject row, so the `REQUEST#<id>` idempotency
marker covers both and a duplicate delivery skips both. Model rows are
written for every subject and model (not only where a budget exists), so a
budget added mid-period includes usage already recorded, exactly like
enabling a weekly limit mid-week. Weekly/monthly model totals are derived
from the model's daily rows with the same strongly consistent range query.
This doubles ledger writes per invocation; see the cost note in
DEPLOYMENT.md.

**Known limitation — a model budget breach blocks the whole subject.** The
enforcement primitives in this design act on the identity, not the model:
the revocation shards deny `aws:SourceIdentity` values and the workload
enforcer attaches a role-wide inline deny. When any model budget reaches its
`block` threshold the subject is blocked exactly as for a subject-level
breach (`status_reason: auto: daily USD quota exhausted for model <id> in
...`, SNS `BLOCKED <subject> reason=model:<id>:<period>-<dimension>`), and
calls to *other* models are refused too until the window resets or the
budget is raised/removed. Making the deny model-selective would require
per-identity resource lists inside the 19 revocation shards and would exceed
the 6,144-character managed-policy cap almost immediately, so it is
deliberately not attempted. Use per-model budgets as a cost guardrail, not as
a model allowlist — `allowed_model_arns` remains the allowlist.

Model budgets are **not** part of the credential-vend pre-flight: at vend
time the model is unknown, so the broker checks subject-level limits and
rate limits only. A subject at 99 % of a model budget vends normally; the
first invocation that crosses the block threshold is metered, the subject is
blocked, and the layered enforcement (revocation shards, lease expiry) cuts
access as for any automatic block.

Admin API: `PUT /admin/user/model-budget?user_id=…&model_id=…` with
`{"limits": {daily|weekly|monthly: {...} | null}, "reason": ...}` sets or
replaces one model budget; `DELETE` on the same route removes it;
`GET /admin/user/model-usage?user_id=…&model_id=…` returns the model's
current calendar usage. Both mutations use the same `If-Match` /
`Idempotency-Key` / audit conventions as the limits route, reconcile the
automatic status immediately (a budget below current model usage blocks at
once; removing the binding budget lifts an automatic block), and reject a
`model_id` that is not covered by `allowed_model_arns`. The admin UI exposes
the same operations in the user detail drawer under "Per-model budgets".

## Architecture

[Editable architecture diagram](assets/architecture.drawio) — page 1 is the
runtime architecture; page 2 is the data-flow diagram with the seven trust
boundaries used by the threat model (also rendered as
[`assets/data-flow.png`](assets/data-flow.png)).

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
Converse, InvokeModel, the OpenAI-compatible Chat Completions and Responses
APIs on the `bedrock-runtime` endpoint, token counting, and streaming where
supported by the selected model. Runtime API/model compatibility remains
model- and Region-specific.

**Metering coverage caveat.** Model invocation logging — the metering
source — captures `InvokeModel`, `InvokeModelWithResponseStream`,
`Converse`, and `ConverseStream`, plus the OpenAI-compatible APIs on the
same endpoint (the Responses API logs the resolved inference-profile ARN
and an extra metadata-less record; the usage processor normalizes the
former and skips the latter). Two Runtime APIs authorize under the same
IAM actions but are **not** logged: `StartAsyncInvoke` (async video/image
generation) and `InvokeModelWithBidirectionalStream` (speech-to-speech).
Vended credentials can call them, and that spend never reaches the ledger.
If strict accounting matters, do not include models that are used through
those APIs (for example, video-generation or speech-to-speech models) in
`allowed_model_arns`.

A third surface is structurally outside this design: the **`bedrock-mantle`
endpoint** (`bedrock-mantle:CreateInference`, `bedrock-mantle:
CallWithBearerToken`) is a separate IAM service prefix whose calls are not
captured by model-invocation logging. Vended sessions cannot reach it — the
role, permissions boundary, and session policy grant `bedrock:*` actions
only, so it is denied by omission — but any *other* principal with
`bedrock-mantle:*` spends unmetered. The `DenyDirectBedrockPolicy` helper
and the SCP example in DEPLOYMENT.md deny both prefixes; extend your own
guardrails the same way (threat model T-08, T-26).

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
| Usage table | Canonical daily ledger (tokens, cache tokens, images, USD, unpriced flags), per-minute rate counters, invocation idempotency markers, and stored reconciliation runs |
| Admin audit table | Routine mutation audit events and idempotency records (365-day retention) |
| Invocation logging | Trusted principal ARN, model, request ID, and tokens |
| Usage processor | Event-driven pricing, deduplication, counters, blocking, detection-lag metric |
| Revocation processor | Always-on sharded `SourceIdentity` deny reconciliation |
| Auto-block sweeper | Nightly (00:05 UTC) lift of automatic blocks for users who never vend again, keeping the deny shards from filling with stale identities |
| Emergency processor | Operator-controlled role-wide deny state machine |
| Spend reconciliation processor (opt-in) | Daily ledger-vs-Cost-Explorer comparison, aggregate and per workload |
| CloudWatch/SNS | Operational metrics, alarms, warnings, and block notifications |
| Admin UI | Overview with per-model usage charts, user management, runtime enforcement controls, live leases, emergency stop, alarms, and audit history |

The admin UI signs in with authorization-code + PKCE against the
deployment's OIDC issuer — the demo Cognito pool, or a corporate IdP (Okta,
Entra ID, Auth0, Keycloak, ...) when `jwt_issuer` and `admin_ui_client_id`
are configured — resolving all endpoints from OIDC discovery. It then
exchanges the current ID token through the Identity Pool for temporary AWS
credentials. It remains an `AWS_IAM`/SigV4 client; the browser never receives
the shared routine admin secret. The break-glass key is entered by an operator
only when confirming an emergency action, is sent with that one request, and
is never persisted by the console. The same secretless User Pool client and
JWT audience retain `USER_SRP_AUTH` and `USER_PASSWORD_AUTH` for
programmatic CLI clients.

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

Because the ledger is priced from a catalog, its USD figure is an estimate.
With `reconciliation_enabled: true` a daily Lambda compares the ledger's total
for a settled day (D-2 by default) with Cost Explorer's Bedrock spend for the
same day and Region — in aggregate, and per workload through the
`bedrock-spend-controls-workload` cost-allocation tag — stores the result,
emits `ReconciliationDeltaPercent`, and alarms when two consecutive days drift
past `reconciliation_alarm_percent`. The Operations tab shows the latest
comparison; `GET /admin/reconciliation` lists recent runs. There is no
per-user reconciliation: JWT users share one IAM role and therefore one line
in the bill. Setup, the tag activation step, and how to read a positive vs a
negative delta are in
[DEPLOYMENT.md § Reconciliation (optional)](DEPLOYMENT.md#reconciliation-optional).

Every alarm the stack creates, and every Lambda component, has a runbook
under [`docs/runbooks/`](docs/runbooks/README.md): what the alarm means in
terms of customer impact, when it is page-worthy, likely causes with exact
CLI/Logs Insights checks, remediation, and how to re-run or reconcile a
component by hand. [`docs/cost-estimate.md`](docs/cost-estimate.md) prices
the deployed resources for a demo and a 1 000-user production scenario, and
[`docs/threat-model.md`](docs/threat-model.md) is the STRIDE review of the
seven trust boundaries — assets, assumptions, a data-flow diagram, and 31
threats written in the AWS threat grammar with priority, mitigation, code
references, and status — and
[`docs/threat-model.tc.json`](docs/threat-model.tc.json) is the same model
exported for [Threat Composer](https://github.com/awslabs/threat-composer),
regenerated from `docs/threat-model.json` by
`tools/threat_composer_export.py` (a test fails when the three drift).

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
  limits schema and windows as JWT identities. The console keeps the two
  kinds apart: the **Users** tab lists JWT identities only, the
  **Workloads** tab lists the deployed roster (model, inference profile, IAM
  role, enforceability) joined with each metered row, and the Overview
  reports the two groups side by side.
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
`enforcement_ready: false` in the admin API and as "Metering only" in the UI,
and a manual block on one is presented as *Record block* — stored and
alerted, not enforced. The deployed roster (name, model, profile ARN, role
ARN, enforceability) is written to the `WorkloadRosterParameter` Parameter
Store parameter; the broker reads it with a five-minute cache to label rows
and to answer `GET /admin/workloads`. The `workload:` namespace is reserved:
`POST /admin/users` rejects it, because a hand-made row would be an
unregistered orphan nothing enforces.

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
3. Prices every dimension the record carries (input/output tokens,
   prompt-cache read/write tokens, generated images) and flags dimensions
   the catalog cannot price.
4. Creates a `requestId` idempotency marker.
5. Updates the canonical daily ledger and the per-model daily ledger in the
   same DynamoDB transaction.
6. Derives all current enabled calendar totals from retained daily rows, for
   the subject and for each model that has a budget.
7. Emits CloudWatch EMF metrics.
8. Sends period-qualified warnings at each configured `warn` threshold and
   blocks when a `block` threshold or a per-minute rate limit is reached.

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
guarantee. Token quotas remain independent of the USD estimate.

### Priced dimensions

The usage processor prices every dimension an invocation-log record carries
against the resolved catalog entry for its model:

| Dimension | Log field | Catalog key | Unit | Status |
|---|---|---|---|---|
| Input tokens | `input.inputTokenCount` | `input_per_mtok` | USD / MTok | Priced (required) |
| Output tokens | `output.outputTokenCount` | `output_per_mtok` | USD / MTok | Priced (required) |
| Prompt-cache read | `input.cacheReadInputTokenCount` | `cache_read_per_mtok` | USD / MTok | Priced when the catalog publishes it |
| Prompt-cache write | `input.cacheWriteInputTokenCount` | `cache_write_per_mtok` | USD / MTok | Priced when the catalog publishes it |
| Generated images | `output.outputBodyJson.images[]` length (or `output.outputImageCount`) | `per_image` | USD / image | Priced when the count is present; see caveat |
| Video / audio seconds | not present in the invocation log | — | — | **Not priced** |

The cache field placement was verified against real `Converse` records with
a `cachePoint` (the counters sit beside `inputTokenCount` on the input side).
The Price List resolver reads `Prompt cache read input tokens` /
`Prompt cache write input tokens` rows for catalog models that publish them
(Nova Micro/Lite/Pro/Premier in `us-east-1` at the time of writing) and the
smallest standard text-to-image `image` row for image models (Nova Canvas).
Models billed through Marketplace (Anthropic) have no Price List rows, so
their cache rates must be pinned in `price_overrides` alongside the token
pair; the shipped file pins Opus 4.7 cache read/write at the published
10%/125%-of-input ratios as an example.

Token quotas count only `inputTokenCount` (uncached tokens) plus
`outputTokenCount`, exactly as before; cached tokens are accumulated in
separate `cache_read_tokens` / `cache_write_tokens` ledger counters and in
the USD estimate but never against the input-token limit.

**Caveats — read before trusting the USD figure for these modalities.**

- *Image generation with image delivery disabled.* This stack's managed
  logging configuration sets `imageDataDeliveryEnabled: false`, and a real
  Nova Canvas record then carries **no token counts and no body**, so the
  processor can meter the request (`requests`, `images: 0`) but cannot
  count the images and prices the call at `$0`. Enable image data delivery
  on the invocation-logging configuration (the record then includes
  `outputBodyJson.images`) or keep image models out of `allowed_model_arns`
  if their spend must be enforced.
- *Image size/quality tiers.* The log reports a count only. The resolver
  uses the smallest **standard** text-to-image rate as the per-image
  estimate; premium or 2048px generations are under-counted unless you pin
  the higher rate in `price_overrides`.
- *Cache write at `$0`.* Some catalog models (Nova) publish a `$0` cache
  write rate; the processor records the tokens and prices them at zero as
  published. This is the catalog value, not a gap.
- *Video, audio, and embeddings.* No invocation-log field carries duration
  or embedding counts, and `StartAsyncInvoke` is not logged at all, so these
  are unmetered. Do not include such models in `allowed_model_arns` if
  strict accounting matters.

**Flag now, repair later.** When a record carries a dimension the resolved
entry has no rate for (for example a cache-enabled call to a model whose
pin lacks `cache_*_per_mtok`, or an image count against an entry with no
`per_image`), the processor prices that dimension at the conservative
fallback's rate when the fallback has one (it does for every dimension any
known model prices, at the maximum known rate), increments
`unpriced_requests` on the subject's daily row, unions the dimension name
into the row's `missing_dimensions` string set, and raises the
`FallbackPricedRequests` / `UnpricedDimensionRequests` metrics (the
`pricing_fallback` alarm fires). Nothing is ever silently priced at zero.
`tools/unpriced_usage.py` lists the affected rows; the admin API surfaces
`unpriced_requests` on every usage response. Historical aggregates are
never repriced automatically: add the rate to the catalog for future events
and decide on a manual repair for past ones.

Service tiers (flex/priority), provisioned throughput, batch inference, and
separately billed tools remain outside the estimate.

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
  --daily-thresholds "50:warn,80:warn,100:block" \
  --weekly-usd 25 \
  --weekly-input-tokens 5000000 \
  --weekly-output-tokens 1000000 \
  --weekly-thresholds "100:warn" \
  --rpm 60 --tpm 100000
```

`--<period>-thresholds` takes comma-separated `<percent>:<warn|block>`
entries (omit the `block` entry for an alert-only period); `--rpm`/`--tpm`
set per-minute rate limits and `--disable-rate` removes both on update.

Scripted clients should follow the same safe routine-write flow: perform an
exact GET before deciding whether to create, so reruns do not rely on
duplicate POST upsert behavior, and carry explicit reasons on status and
limit writes. Limit reasons are
optional for compatible API and CLI clients; omitted or blank values use the
standardized legacy audit fallback.

The canonical exact-user route family is:

- `GET /admin/user?user_id=<encoded>` for detail.
- `PUT /admin/user/limits?user_id=<encoded>` for limits (periods, thresholds, rate).
- `PUT /admin/user/status?user_id=<encoded>` for status.
- `PUT` / `DELETE /admin/user/model-budget?user_id=<encoded>&model_id=<id>`
  for one model-scoped budget; `GET /admin/user/model-usage` for its usage.
- `GET /admin/user/usage?user_id=<encoded>&period=weekly&window=...` for a
  calendar usage window (`period` defaults to `daily`).
- `/admin/user/usage-history` and `/admin/user/audit` with the same `user_id`
  query parameter for retained history and per-user audit.

Deployment-wide read routes: `GET /admin/summary` (all-subject figures plus
an `enforcement.subjects` split into `users` and `workloads`, the latter with
`configured`, `metering_only`, `unregistered`, and `awaiting_traffic`
counts), `GET /admin/workloads` (the deployed roster joined with metered
rows; `subject` is `null` for a workload that has not invoked since deploy),
`GET /admin/operations`, `GET /admin/usage/metrics` (each `top_users` entry
carries its `granularity`), `GET /admin/audit`, and
`GET /admin/reconciliation?limit=N` (recent ledger-vs-bill runs, or an
explicit `enabled: false` payload when reconciliation is not deployed).

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
| `examples/` | SigV4 admin client and lazy credential provider for application code |
| `qualification/` | Guarded probes to measure enforcement latency in your own account before trusting an overspend bound |
| `tools/` | Price-catalog export, unpriced-usage report, cost estimate, threat-model export (Threat Composer), and data-flow diagram rendering |
| `docs/` | Runbooks per component and alarm, threat model (Markdown, JSON, Threat Composer), and the cost estimate |
| `tests/` | Unit, API, infrastructure, qualification-probe, pricing, and documentation-consistency tests |

## Verification and security scanning

The change gate is the unit and infrastructure suite plus a synth of both
reference configurations:

```bash
python -m pytest tests/ -q
(cd admin-ui && npm ci && npm test && npm run build)
(cd cdk && for c in demo production; do
  npx cdk synth --app "python app.py" -c deployment_config=config/$c.json --quiet
done)
```

The same static analysis that runs on the hosted repository can be
reproduced locally; the sample is kept clean against it. The Semgrep run
below is deliberately stricter than the defaults: `--disable-nosem` ignores
inline suppressions and the empty `.semgrepignore` keeps `tests/` in scope,
which is how AWS's security review tooling scans submitted code. Every
security rule passes under those conditions; the only error-severity results
are the `return-in-init` parser false positives described below.

```bash
touch .semgrepignore   # opt out of Semgrep's default tests/ exclusion
semgrep scan --config r/python --config r/typescript --config r/javascript \
  --config r/generic --config p/security-audit --config p/secrets \
  --config p/jwt --disable-nosem \
  --exclude cdk.out --exclude node_modules --exclude dist --exclude .venv .
bandit -r gateway usage_processor cdk/stacks tools examples tests \
  enforcement_dispatcher emergency_processor revocation_processor \
  workload_enforcer reconciliation_processor quota_periods_layer \
  auto_block_sweeper --skip B101
gitleaks git .   # tracked history; build artifacts are git-ignored
```

Remaining findings are warning-level and intentional, and the code says why
at each site: the Lambda entrypoint's `0o755` mode, the two `https`-only
`urlopen` calls, and the `ce:GetCostAndUsage` wildcard resource that Cost
Explorer requires. The host-side `pip` bundler runs a constant program
name (`pip3`) with an argument list and no shell, and the test suite signs
its HS256 tokens with a key generated per session rather than one committed
to the repository, so neither needs a suppression. The React
`jsx-not-internationalized` advisories are accepted — the administration
console is a single-locale sample and is not internationalized. Semgrep's
`return-not-in-function` / `return-in-init` findings on
`gateway/app/config.py`, `gateway/app/broker.py`, and
`examples/refreshable_bedrock.py` are parser false positives on
`lambda:` expressions (reproducible with a one-line
`field(default_factory=lambda: …)`); the code contains no such `return`.
