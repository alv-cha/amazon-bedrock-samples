# Deployment guide

This is the canonical deployment guide for the runtime-only per-user quota
sample.

## Decisions before deployment

| Decision | Demo/personal account | Production/shared account |
|---|---|---|
| Quota identity | JWT `sub` | Stable user, tenant, team, or project claim |
| IdP | Stack-created Cognito | Existing OIDC IdP |
| Enforcement | Layered (lease + revocation + emergency, always on); 300 s default lease | Same layers; pick the default lease window and tune the runtime dial per incident |
| Credential lifetime | 900-second STS; optional 60/300/900-second permission lease | 900–3600-second STS; no role-chained session above one hour |
| Runtime models | `*` for exploration | Explicit model and inference-profile ARNs; exclude models used via unmetered APIs (async invoke, bidirectional streaming) |
| Invocation logging | Stack managed | Reuse centrally managed logging |
| Log subscription | Dedicated demo group | Confirm subscription-filter capacity and ownership |
| Auto-provisioning | Enabled | Usually disabled |
| Function URL callers | Account default | Explicit backend/admin role ARNs |
| Usage retention | 35 days | Policy-defined value |
| Routine admin audit | Fixed 365 days from deployment; no backfill | Fixed 365 days from deployment; no backfill |
| DynamoDB deletion | `DESTROY` | `RETAIN` |
| Alerts | Personal email | Operations topic/email |
| Admin UI | Stack-created Cognito path | `admin_ui=true` + `admin_ui_client_id` against the corporate IdP |
| Direct Bedrock access | Optional demo deny policy | Required SCP, boundary, or equivalent deny |
| Price fallback | Conservative default | Review against most expensive allowed model |

The hard architectural decision is the enforcement guarantee. This sample
does not inspect each inference request. Every credential embeds an
immutable permission-lease deadline, and the always-on revocation layer
cuts blocked identities' in-flight sessions after eventually consistent
IAM propagation. Already-authorized streams may finish.

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
   minutes; the permission lease ends Bedrock permission after 1, 5, or 15
   minutes (runtime dial, `PUT /admin/enforcement`).
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
| `default_limits` | Daily finite; weekly/monthly `null` | Object with `daily`, `weekly`, `monthly`, optional `rate`; enabled periods contain non-negative `usd`, `input_tokens`, `output_tokens` and an optional `thresholds` list; `null` disables a period. See [Thresholds and rate limits](#thresholds-and-rate-limits) |
| `warn_threshold` | `0.8` | Greater than 0 and less than 1; the `warn` level of the default thresholds list applied to rows and periods without their own list |
| `usage_retention_days` | `35` | At least 31; canonical daily ledger retention used to derive current monthly totals |
| `retain_tables_on_delete` | `false` | `true` maps tables to `RETAIN` |
| `vended_ttl_seconds` | `900` | 900–3600; Lambda broker role chaining rejects longer sessions |
| `permission_lease_seconds` | `300` | `60`, `300`, or `900`; deployment DEFAULT for the runtime dial (`PUT /admin/enforcement`) |
| `refresh_overlap_seconds` | `10` | Positive and less than permission lease |
| `refresh_jitter_seconds` | `5` | Non-negative and less than refresh overlap |
| `vend_rate_limit_per_minute` | `6` | Positive per-user attempts, including retries |
| `revocation_policy_shards` | `19` | Immutable layout; plus emergency policy = 20 role attachments |
| `revocation_reconcile_minutes` | `5` | Positive periodic repair interval for the revocation layer |
| `allowed_model_arns` | `["*"]` | Non-empty Bedrock resource ARN list or `*`; see the metering coverage caveat in the README |
| `invoker_principal_arns` | `[]` | IAM principals allowed to invoke the Function URL |
| `manage_invocation_logging` | none | Explicit `true` or `false` required |
| `invocation_log_group_name` | empty | Required when logging is externally managed |
| `model_config` | `config/model-pricing.json` | Validated catalog, overrides, and fallback |
| `jwt_issuer` | empty | Empty creates demo Cognito |
| `jwt_audience` | empty | Comma-separated audiences accepted by the broker |
| `jwt_jwks_url` | discovery | Explicit JWKS URL when discovery is unavailable |
| `jwt_user_claim` | `sub` | Claim used as the quota key |
| `admin_jwt_claim` | empty | Claim used for browser admin authorization |
| `admin_jwt_value` | empty | Required claim value or group |
| `admin_ui` | `false` | Hosts the UI; works with demo Cognito or a BYO issuer; requires both admin JWT fields |
| `admin_ui_client_id` | empty | BYO issuer only: the SPA's public OAuth client id (defaults to `jwt_audience`) |
| `admin_ui_connect_origins` | `[]` | BYO issuer only: extra HTTPS origins for the UI CSP (token endpoint on a different origin) |
| `alert_email` | empty | Creates an SNS email subscription |
| `snapstart` | `false` | Enable Python Lambda SnapStart for the broker |
| `adapter_layer_arn` | regional default | Override Lambda Web Adapter layer |
| `workloads` | empty | Workload-mode roster (inline JSON or file path); see [Workload mode](#workload-mode-per-workload-quotas) |
| `reconciliation_enabled` | `false` | Boolean; deploys the daily ledger-vs-Cost-Explorer comparison; see [Reconciliation (optional)](#reconciliation-optional) |
| `reconcile_lag_days` | `2` | 1–14; which settled day (`today − lag`, UTC) each run compares |
| `reconciliation_alarm_percent` | `10` | Greater than 0 and at most 100; absolute `ReconciliationDeltaPercent` that, over two consecutive daily runs, raises `reconciliation_delta` |

Calendar quota windows are fixed in UTC: days reset at 00:00, weeks reset
Monday at 00:00, and months reset on the first at 00:00. All enabled periods
are enforced simultaneously. Weekly/monthly values are not rolling windows.

### Thresholds and rate limits

Every enabled period carries an ordered `thresholds` list; a subject may
also carry per-minute `rate` limits. Both can be set per subject through the
admin API/UI and defaulted for new subjects through `default_limits`:

```json
{
  "default_limits": {
    "daily": {
      "usd": 25,
      "input_tokens": 10000000,
      "output_tokens": 2000000,
      "thresholds": [
        {"at": 0.5, "action": "warn"},
        {"at": 0.8, "action": "warn"},
        {"at": 1.0, "action": "block"}
      ]
    },
    "weekly": {
      "usd": 100,
      "input_tokens": 0,
      "output_tokens": 0,
      "thresholds": [{"at": 1.0, "action": "warn"}]
    },
    "monthly": null,
    "rate": {"rpm": 60, "tpm": 100000}
  }
}
```

Synthesis validates the list with the same rules the runtime applies:
non-empty, `0 < at <= 10`, strictly increasing `at`, `action` in
`warn`/`block`, at most one `block` and only as the last entry. A period
whose list has no `block` is alert-only: it warns at each level and never
blocks. Omitting `thresholds` keeps the pre-existing behaviour
(`[{warn_threshold: warn}, {1.0: block}]`); omitting `rate` (or setting a
dimension to `0`) leaves rate limiting off.

The metering processor sends one SNS warning per level per calendar window
(`warning_sent_<period>_<at_bps>_window` markers on the user row) and blocks
only when a `block` level is reached, which may be above 100 %. Rate limits
are counted from metered invocations per UTC minute — requests, and uncached
input + output tokens (cache read/write tokens are excluded) — in a
short-lived `RATE#<subject>` / `<UTC minute>` counter row in the usage table
that is written only for subjects with a positive `rpm`/`tpm`. A breach uses
the same automatic block path as a calendar breach (`status_origin:
automatic`, revocation sentinel, SNS `BLOCKED <subject> reason=rpm|tpm`) and
lifts automatically once the current minute is under the limit, at the next
credential vend or workload-enforcer pass. Rate blocks therefore never
require a manual unblock; a manual admin block still never auto-lifts.

Existing rows need no migration: rows without stored thresholds resolve to
the deployment default, `rate` is absent (off), and the first admin edit
materializes the list on the row.

The usage table retains one exactly-once daily ledger row per subject **and
one per (subject, model)** — `<subject>#model#<model_id>` — written in the
same transaction so the `REQUEST#` idempotency marker covers both. The
broker, usage processor, and workload enforcer derive current weekly/monthly
totals with strongly consistent range queries over those daily rows. This
avoids a rollup migration gap when a longer period is enabled mid-window, and
lets a per-model budget added mid-period include usage already recorded. It
adds a small read cost (up to 37 small rows per ledger) to vends and metered
invocations for period-aware evaluation, and — since the model row exists —
**two ledger `UpdateItem`s per metered invocation instead of one** (three
items in the transaction with the marker). Subjects with model budgets pay one
extra range query per budgeted model on each metered invocation and vend.
Increasing retention does not restore rows that
already expired; a deployment previously below 31 days must wait for a clean
month boundary or backfill from retained invocation logs before enabling a
monthly cap.

### Safe routine administration

The routine API remains behind the `AWS_IAM` Function URL. Browser requests are
SigV4-signed with temporary Identity Pool credentials and carry the current ID
token in `X-Quota-User-Token`; trusted programmatic clients may use the shared
routine key. The demo browser authenticates with Cognito managed login using an
authorization-code + PKCE flow. Its secretless app client still enables
`USER_SRP_AUTH` and `USER_PASSWORD_AUTH` for programmatic CLI clients and
keeps the same client ID as the gateway JWT audience.

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
- `PUT /admin/user/limits?user_id=<encoded>` for limits (periods, thresholds, `rate`).
- `PUT /admin/user/status?user_id=<encoded>` for status.
- `PUT` / `DELETE /admin/user/model-budget?user_id=<encoded>&model_id=<id>`
  for one per-model budget (same `If-Match`/`Idempotency-Key`/reason rules;
  `model_id` must be covered by `allowed_model_arns`).
- `GET /admin/user/model-usage?user_id=<encoded>&model_id=<id>` for a model
  ledger's current calendar usage.
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

A limit of `0` means **Unlimited** for that one dimension of an enabled
period. The UI requires explicit confirmation plus a non-empty reason when a
positive limit becomes Unlimited, when a quota period is enabled or disabled,
and when a submitted finite limit is below current usage for that period;
other limit reasons are optional. Deployment defaults enable a finite daily
period and leave weekly and monthly disabled (`null`). Temporary per-user
overrides, bulk operations, user delete, and usage reset are not part of this
MVP.

### Operations tab

`GET /admin/operations` uses the routine admin authorization path and powers a
GUI status panel. It combines deployed credential configuration, normalized
emergency convergence state, conservative qualification metadata, p95
`DetectionLagMilliseconds`, revocation freshness/failure/overflow metrics, and
CloudWatch alarm states—including the emergency/revocation DLQ alarms. The
same panel shows the live-lease timeline: the first page of vended logical
leases, polled every five seconds from
`GET /admin/users?include_usage=false`.

The same tab exposes two explicit controls:

- The runtime permission-lease dial calls `PUT /admin/enforcement`. Its valid
  values come from `GET /admin/enforcement`; the UI does not hardcode them and
  requires an audit reason before applying a change.
- Emergency activate/recover calls `POST /admin/emergency-stop`. It requires
  the separate break-glass key, the action-specific confirmation phrase, and a
  non-empty reason. The key is held only in the dialog state for that request
  and is never persisted by the console.

CloudWatch reads are performed by the broker role using only
`cloudwatch:GetMetricData`, `cloudwatch:DescribeAlarms`, and
`cloudwatch:ListMetrics`. The browser still
has only Function URL invocation permission. API responses and generated UI
configuration never include the emergency key, secret ARN/value, or IAM policy
ARNs. If CloudWatch is denied or has no data, the endpoint returns local state
with `unavailable`, `unknown`, `not_applicable`, or
`INSUFFICIENT_DATA`; it does not label missing telemetry healthy and does not
break user quota administration.

### Overview usage charts

`GET /admin/usage/metrics?days=N` (default 14, maximum 30) powers the
Overview tab's per-model usage charts and the top-users-by-spend list. It
reads the EMF metrics the usage processor emits (`EstimatedCostUSD`,
`Requests`, `InputTokens`, `OutputTokens` in the deployed metrics namespace)
in daily UTC buckets that match the quota calendar windows. Quota accounting
is unaffected: the DynamoDB daily ledger remains the canonical enforcement
source; these charts are observability only. Model and user dimension values
are discovered with `cloudwatch:ListMetrics`, which only lists metrics that
received data points in roughly the last two weeks, so a 30-day range can
omit identities idle since then. Discovery is capped at 20 models and 100
users per response. Top-user entries resolve the display name from the users
table server-side; identities that metered usage but were since removed fall
back to the raw quota key. When CloudWatch is denied or degraded the endpoint
returns `status: unavailable` or `partial` with empty or partial series
instead of failing, and the UI explains the gap.

Qualification shown in the panel is reviewed deployment metadata, not
inferred from alarm health. Lease, revocation, and emergency live
qualification remain pending until recorded in `qualification/QUALIFICATION.md`.

### Runtime enforcement dial

Enforcement is layered and always on; the only operational knob is the
permission-lease window, and it changes at runtime without a redeploy:

```bash
# Read the effective window, its source, and the valid values
GET /admin/enforcement

# Change it (audited; applies to NEW vends immediately)
# Send the GET ETag as If-Match and a fresh Idempotency-Key.
PUT /admin/enforcement
{"permission_lease_seconds": 60, "reason": "incident response"}
```

Outstanding credentials keep the deadline they were issued with, and the
revocation layer keeps cutting blocked identities regardless of the dial.
The deployment key `permission_lease_seconds` only sets the default used
when no runtime override exists. Every change writes an immutable
`CONFIG#ENFORCEMENT_AUDIT` row and emits the `EnforcementDialChanged`
metric. The state row and immutable audit record commit in one DynamoDB
transaction. Stale `If-Match` values and reused idempotency keys return distinct
`409` errors instead of silently overwriting another operator.

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
cdk/.venv/bin/python qualification/lease_revocation_probe.py \
  --profile YOUR_SANDBOX_PROFILE \
  --role-arn arn:aws:iam::111122223333:role/YOUR_DEDICATED_SANDBOX_ROLE \
  --managed-policy-arn arn:aws:iam::111122223333:policy/YOUR_PREATTACHED_SANDBOX_DENY \
  --region us-east-1 \
  --model-id YOUR_COUNT_TOKENS_MODEL
```

Live mode temporarily versions IAM policy and invokes Bedrock. Run it only
after reviewing the printed account/role and explicitly approving those exact
non-production resources. Record results in `qualification/QUALIFICATION.md`.

The current broker's caller is a Lambda execution-role session, so
`AssumeRole` is role chaining and cannot exceed 3,600 seconds. Eight-hour
configuration is rejected. Supporting it requires a separate first-hop
federation/token-issuer design with bypass and replay analysis.

The revocation layer uses an immutable 19-shard layout plus one emergency managed
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
break-glass procedure, then either export it as `EMERGENCY_ADMIN_KEY` for the
CLI or enter it into the Operations tab for one action. Routine admin keys and
admin UI JWTs alone cannot invoke these operations.

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
the standard endpoints with `user_id=workload:<name>`;
`GET /admin/users?granularity=workload` filters rows, and
`GET /admin/workloads` returns the deployed roster joined with them.

**Roster parameter.** The stack writes `{workload_id: {name, model,
profile_arn, role_arn, enforcement_ready}}` to the `WorkloadRosterParameter`
SSM parameter (intelligent tiering, so large rosters are promoted past the
4 KB standard cap automatically). Profile ARNs are deploy-time tokens and
about 100 characters each, which is why the roster is not a Lambda
environment variable: the broker's environment is capped at 4 KB. The broker
reads the parameter with a five-minute cache (`WORKLOAD_ROSTER_CACHE_SECONDS`)
and falls back to the inline `WORKLOAD_ROSTER_JSON` (empty in deployments;
used by local runs and tests) if Parameter Store is unreadable, logging a
warning. `GET /admin/workloads` reports which source answered as
`roster_source`. Roster changes therefore take effect at most five minutes
after a deploy without a broker restart.

**Console.** The Users tab lists JWT identities only (`granularity=user`);
the Workloads tab shows the roster with per-workload model, inference profile,
IAM role, and an enforcement pill: *Enforced* (role attached), *Metering
only* (no role), *Unregistered* (a metered row no longer in the config), or
*Awaiting traffic* (configured, no invocation since deploy — no quota row
yet, so limits and status become editable after its first call). The
Overview reports users and workloads as separate groups with an all-subject
total in the enforcement strip. `POST /admin/users` rejects the `workload:`
prefix; workload rows only come from metering.

Scale envelope: 1,000 application inference profiles per account
(adjustable), 1,000 IAM roles per account (adjustable); the deny document is
about 300 bytes against the 10,240-character inline policy limit.

### Price configuration

`config/model-pricing.json` contains:

```json
{
  "catalog_models": {
    "price-list-model-name": ["runtime-model-id"],
    "Nova Canvas": ["amazon.nova-canvas-v1:0"]
  },
  "price_overrides": {
    "anthropic.claude-opus-4-7": {
      "input_per_mtok": 5,
      "output_per_mtok": 25,
      "cache_read_per_mtok": 0.5,
      "cache_write_per_mtok": 6.25,
      "reason": "Marketplace-billed; no Pricing API entry"
    },
    "us.anthropic.claude-opus-4-7": {
      "input_per_mtok": 5.5,
      "output_per_mtok": 27.5,
      "reason": "US geographic uplift"
    }
  },
  "fallback_price": {
    "input_per_mtok": 15,
    "output_per_mtok": 75
  }
}
```

Every price entry requires `input_per_mtok` and `output_per_mtok` (USD per
million tokens) and may add `cache_read_per_mtok`, `cache_write_per_mtok`
(USD per million tokens) and `per_image` (USD per generated image).
Unknown keys fail synthesis. The token pair must be positive except for an
image model (`per_image > 0`), whose token rates are pinned at `0` because
the Price List publishes none; `fallback_price` must always carry a positive
token pair. `cache_write_per_mtok` may be `0` (the Nova catalog value).

AWS Price List is queried during deployment and the resulting snapshot seeds
an SSM parameter plus a usage-processor environment fallback. For each
`catalog_models` entry the resolver reads the standard on-demand
`Input tokens` / `Output tokens` rows and, when published, the
`Prompt cache read input tokens` / `Prompt cache write input tokens` rows
(matched literally on `inferenceType`, unit `1K tokens`) and the smallest
standard text-to-image `image`-unit row. An ambiguous catalog (two
different rates for one dimension) fails the resolve rather than averaging.
The conservative fallback is raised, per dimension, to at least the highest
known rate for that dimension, so a record whose dimension is missing from
its own model's entry is priced conservatively rather than at zero.
A daily
EventBridge schedule re-resolves catalog prices and rewrites the parameter,
so Pricing API changes reach metering without a redeploy; if Parameter Store
or a refresh fails, metering continues on the last value it read (or the
processor's built-in conservative defaults before its first read) and the
refresh failure surfaces through the Lambda error metric. The snapshot is
deliberately **not** copied into the Lambda environment: Lambda caps
environment variables at 4 KB and the resolved catalog with cache and image
dimensions is already ~3 KB. Current official
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

Do not inject the complete catalog into the price configuration: it contains
many Regions and incompatible units and would exceed the SSM standard
parameter limit (4 KB). Use it for discovery/audit, then map only the exact Runtime
model and inference-profile IDs needed by the deployed Region. `--metering-compatible`
selects the standard on-demand input/output USD token rows and computes
`price_per_million_tokens`, but it deliberately does not guess Runtime IDs.

An unknown ID is charged at the fallback, which deployment raises to at least
the highest input and output rates in the known snapshot. The configured
`$15/$75` fallback is deliberately conservative and is not the Opus 4.7 list
price. It can make USD usage higher than the final bill. It is not a universal
upper bound for a more expensive model or a modality that is not billed by
input/output tokens. Review pricing before adding models, inference profiles,
service tiers, provisioned throughput, or separately billed tools. Prompt
caching and image generation are priced when the catalog entry carries the
dimension (see the README "Priced dimensions" table and its caveats); a
record carrying a dimension its entry lacks is priced at the fallback's rate
for that dimension, flagged on the daily row (`unpriced_requests`,
`missing_dimensions`), and alarmed via `pricing_fallback`. List flagged rows
with `tools/unpriced_usage.py --table <UsageTableName>`. Updating pricing
changes future events only; existing DynamoDB daily aggregates and blocked
status are not repriced.

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

### Reconciliation (optional)

The ledger prices every invocation from the catalog; it is an *estimate*. To
check daily that the estimate tracks the bill, enable reconciliation:

```json
{
  "reconciliation_enabled": true,
  "reconcile_lag_days": 2,
  "reconciliation_alarm_percent": 10
}
```

This deploys `SpendReconciliationFn`, a `cron(0 6 * * ? *)` schedule, the
`reconciliation_delta` alarm, a dashboard widget, and grants the function
`ce:GetCostAndUsage` (no resource-level scoping exists for Cost Explorer;
this is the only `ce:` action granted). Each run compares, for day
`today − reconcile_lag_days` in UTC:

- **Aggregate** — the sum of every metered subject's daily ledger row against
  Cost Explorer `UnblendedCost` for `SERVICE ∈ {Amazon Bedrock, Amazon Bedrock
  Service}` in the stack's Region.
- **Per workload** — each `workload:<name>` row against Cost Explorer
  filtered on the cost-allocation tag `bedrock-spend-controls-workload=<name>`
  that the stack already stamps on every application inference profile.

Results are stored as `RECONCILE#<day>` rows in the usage table (same TTL as
the ledger) and served by `GET /admin/reconciliation`; the Operations tab
shows a **Spend reconciliation** card. When the feature is off the endpoint
and card say so explicitly rather than showing an empty comparison.

**Before the first run:**

1. *Cost Explorer must be enabled on the payer account.* Open Billing and
   Cost Management → Cost Explorer once; the API can take up to 24 h to start
   answering, during which the function emits `ReconciliationFailure` and one
   SNS message per day.
2. *Activate the cost-allocation tag* if you use workloads, or every workload
   reports `tag_inactive` (ledger has spend, CE sees none for the tag):
   Billing and Cost Management → **Cost allocation tags** → select
   `bedrock-spend-controls-workload` → **Activate**, or
   ```bash
   aws ce update-cost-allocation-tags-status \
     --cost-allocation-tags-status TagKey=bedrock-spend-controls-workload,Status=Active
   ```
   The tag appears in the list only after a tagged resource has incurred
   cost; CE starts attributing ~24 h after activation and does not backfill.
   Until then the function sends `RECONCILIATION: cost-allocation tag not
   active` daily and emits `ReconciliationTagInactive = 1` per workload.
   **In an AWS Organization this is a management (payer) account action**:
   a linked account gets `AccessDeniedException: Linked account doesn't have
   access to cost allocation tags` from both the console and the CLI, so ask
   the payer administrator to activate the key once for the organization.
   Aggregate reconciliation does not depend on the tag and works from the
   linked account as long as Cost Explorer is enabled there.

**Reading the delta.** `delta = billed − estimated`; `delta_percent` is that
difference relative to the *larger* of the two figures, so it is always
within ±100 % (−100 % = the bill saw nothing the ledger metered, +100 % = the
ledger saw nothing the bill charged; `null` only when both are zero). A
*positive* delta is
expected wherever other principals in the account call Bedrock in that
Region (console Playground, other roles, direct calls): the ledger holds only
metered subjects, CE holds the whole account. Set
`reconciliation_alarm_percent` above that floor, or route those callers
through the gateway or a workload profile. A *negative* delta means the
catalog over-prices (fallback-priced models, a high override, a cross-Region
profile priced at the base model's rate) or the account has credits or
private pricing; see
[docs/runbooks/alarms/reconciliation-delta.md](docs/runbooks/alarms/reconciliation-delta.md).

**Limits.** Cost Explorer data lags 24–48 h, which is why the default compares
D-2, and daily figures for the last day may still change (`Estimated: true`).
JWT-vended users share one IAM role and therefore one line in the bill: there
is **no per-user reconciliation**, aggregate is the finest grain for them.
Cost Explorer bills $0.01 per API request — one run is `1 + workloads`
requests, roughly $0.30 per month plus $0.30 per workload. The historical
ledger is never repriced by a run; reconciliation is a drift signal, not a
correction.

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
- Python 3.12 or later, with `pip` available (interpreter module or PATH).
- No container runtime is required: the broker bundles on the host with
  pinned manylinux wheels. Docker or Finch is used only as an automatic
  fallback if host pip bundling fails (set `CDK_DOCKER=finch` for Finch).
- Bedrock model access in the selected Region.

```bash
cd poc-to-prod/bedrock-spend-controls

export AWS_PROFILE=your-profile
export AWS_REGION=us-east-1
export AWS_DEFAULT_REGION=$AWS_REGION
export ALERT_EMAIL=you@example.com

aws sso login --profile "$AWS_PROFILE"   # omit for non-SSO credentials
aws sts get-caller-identity
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
  --daily-output-tokens 200000 \
  --weekly-usd 10 \
  --weekly-input-tokens 5000000 \
  --weekly-output-tokens 1000000

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
`get-usage --period weekly` uses
`GET /admin/user/usage?user_id=...&period=weekly&window=...`. User IDs are
supplied as raw query values and encoded once by the signed HTTP client. Do not
treat duplicate, version, or idempotency conflicts as success.

In the UI, verify the Overview, Users, Operations, and Audit tabs; the
Overview per-model usage charts with their metric and range selectors; the
live-lease timeline on Operations; 25-row
server pagination/search; explicit Unlimited confirmation; required reasons
for period enable/disable, Unlimited, and finite-below-current-period changes;
optional reasons for ordinary limit increases; reasoned status changes; and
the detail period-selectable Usage/Changes views. In a
dedicated sandbox, also verify a dial change and one complete emergency
activate/recover cycle with the break-glass key.

### 7. Runtime smoke test

Obtain a JWT for a quota user from your IdP (for the demo Cognito pool, an
`InitiateAuth` call with `USER_PASSWORD_AUTH` against the deployed app client
returns an ID token), then run the integration path an application would use:
the lazy credential provider from `examples/refreshable_bedrock.py` vends
short-lived credentials from the broker and hands them to an ordinary boto3
Bedrock Runtime client.

```bash
export GATEWAY_URL="$BROKER_API_URL"
export USER_JWT='your-quota-user-jwt'

cdk/.venv/bin/python - <<'PY'
import os, sys
sys.path.insert(0, "examples")
from refreshable_bedrock import QuotaBrokerCredentialProvider

provider = QuotaBrokerCredentialProvider(
    os.environ["GATEWAY_URL"], os.environ["USER_JWT"],
    region=os.environ.get("AWS_REGION", "us-east-1"),
)
runtime = provider.bedrock_client()
response = runtime.converse(
    modelId="openai.gpt-oss-20b-1:0",
    messages=[{"role": "user", "content": [{"text": "Reply with exactly: runtime quota smoke"}]}],
)
print(response["output"]["message"]["content"][0]["text"])
print("usage:", response["usage"])
PY
```

The vend happens on the first signed request; the inference itself goes
directly to `bedrock-runtime`. A blocked or over-budget user fails here with a
`BrokerCredentialError` instead of reaching Bedrock. Allow invocation-log
delivery time (typically one to two minutes) before checking usage:

```bash
cdk/.venv/bin/python examples/sigv4_gateway.py \
  --gateway-url "$BROKER_API_URL" \
  --profile "$AWS_PROFILE" \
  --region "$AWS_REGION" \
  --admin-key "$ADMIN_KEY" \
  get-usage "$USER_ID" --period weekly
```

`USER_ID` is the value of the configured JWT identity claim (`sub` by
default). The ledger row should show one request with the same input/output
token counts the `Converse` response reported.

To smoke-test enforcement end to end: set the user's daily token limits to
the current usage plus one token (`update-user`), repeat the call above and
confirm the credential request is rejected, then raise the limits and confirm
vending recovers. Each step is visible in the Admin UI audit log and in the
per-user CloudWatch EMF events.

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
  "default_limits": {
    "daily": {
      "usd": 25,
      "input_tokens": 10000000,
      "output_tokens": 2000000
    },
    "weekly": null,
    "monthly": null
  },
  "warn_threshold": 0.75,
  "usage_retention_days": 90,
  "retain_tables_on_delete": true,
  "vended_ttl_seconds": 900,
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
export DEPLOYMENT_CONFIG=config/production.local.json

aws sso login --profile "$AWS_PROFILE"
aws sts get-caller-identity

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

Both the backend and the browser UI federate with any OIDC-compliant issuer.
The UI resolves its endpoints from the issuer's discovery document
(`/.well-known/openid-configuration`), signs in with authorization-code +
PKCE against a public (no-secret) client, and exchanges the ID token for
temporary AWS credentials at a Cognito Identity Pool to SigV4-sign the
`AWS_IAM` Function URL.

Set `admin_ui=true` together with your issuer, and the stack hosts and wires
everything — including an IAM OIDC provider and the Identity Pool that
trusts it:

```json
{
  "jwt_issuer": "https://your-idp.example.com",
  "jwt_audience": "bedrock-runtime-quota-broker",
  "admin_ui": true,
  "admin_ui_client_id": "spa-public-client-id",
  "admin_jwt_claim": "groups",
  "admin_jwt_value": "bedrock-quota-admins"
}
```

- `admin_ui_client_id` is the SPA's public OAuth client registered in the
  corporate IdP (omit it when the UI shares the data-plane `jwt_audience`
  client). The broker then accepts both audiences.
- After the first deploy, register the `AdminUiCallbackUrl` stack output as
  the redirect URI on that client. This is the only chicken-and-egg step:
  the CloudFront URL exists only after deployment.
- The client must allow browser (CORS) calls to the token endpoint. In
  Microsoft Entra ID register the redirect URI under the *Single-page
  application* platform; in Okta/Auth0 use a public SPA client. If the IdP
  requires a scope for refresh tokens (Entra: `offline_access`), the UI
  session re-authenticates silently on expiry without it.
- If the IdP serves its token endpoint from a different origin than the
  issuer (as Cognito does), list that origin in
  `admin_ui_connect_origins` so the UI's Content-Security-Policy allows it.
- Sign-out uses the discovered `end_session_endpoint` with both the OIDC
  standard and Cognito parameter names; providers ignore the name they do
  not use.

Hosting the UI elsewhere (own domain, existing web platform) remains
possible: build `admin-ui/`, serve it with a `config.js` like the one the
stack writes, and allow the exact HTTPS origin on the Function URL CORS
policy — the stack configures CORS automatically only for its own CloudFront
distribution. Do not use a wildcard production origin.

```javascript
window.QUOTA_ADMIN_CONFIG = {
  gatewayUrl: "BROKER_API_URL",
  region: "us-east-1",
  issuer: "https://your-idp.example.com",
  clientId: "spa-public-client-id",
  identityPoolId: "IDENTITY_POOL_ID",
  scopes: "openid email profile"
};
```

`config.js` contains public identifiers only. The shared admin key is for
trusted CLI or backend use and must never be embedded in the browser;
browser admins are authorized by their JWT claim (`admin_jwt_claim` /
`admin_jwt_value`).

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
        "bedrock:CallWithBearerToken",
        "bedrock-mantle:CreateInference",
        "bedrock-mantle:CallWithBearerToken"
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

The two `bedrock-mantle:*` actions are included because the Bedrock Mantle
endpoint is not captured by model-invocation logging: any principal that can
call it spends outside the ledger. The vended role never receives those
actions, so the exception for `BEDROCK_USER_ROLE_ARN` does not reopen the
gap.

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
10. New Bedrock authorization fails after the effective
    permission deadline even though `sts_expiration` is later.
11. Detection lag is reported separately from the post-detection cutoff.
12. Revocation mode, when enabled, denies only the targeted `SourceIdentity`,
    reports p50/p95/max propagation, and alarms/falls back on failure.
13. Emergency activation closes vending before its role-wide deny; recovery
    removes the deny before reopening vending.
14. Unknown models use the configured conservative fallback.
15. Destroy testing confirms production tables are retained.
16. Managed login completes authorization-code + PKCE while the same app
    client still issues `USER_PASSWORD_AUTH` tokens for programmatic clients
    with the gateway JWT audience.
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
query route; `get-usage --period monthly` calls
`GET /admin/user/usage?user_id=...&period=monthly&window=...`. A race still surfaces as an
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
  --daily-output-tokens 2000000 \
  --monthly-usd 500 --monthly-input-tokens 200000000 \
  --monthly-output-tokens 40000000

# List
python examples/sigv4_gateway.py \
  --gateway-url "$BROKER_API_URL" --profile "$AWS_PROFILE" \
  --region "$AWS_REGION" --admin-key "$ADMIN_KEY" list-users

# Update any dimensions while preserving unspecified periods
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

# Query the current monthly calendar window
python examples/sigv4_gateway.py \
  --gateway-url "$BROKER_API_URL" --profile "$AWS_PROFILE" \
  --region "$AWS_REGION" --admin-key "$ADMIN_KEY" \
  get-usage tenant-acme --period monthly

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
