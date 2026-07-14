# Per-user quota monitoring and enforcement for Amazon Bedrock

Amazon Bedrock enforces [service quotas](https://docs.aws.amazon.com/bedrock/latest/userguide/quotas.html)
at the **account level**, and [Projects](https://docs.aws.amazon.com/bedrock/latest/userguide/projects.html)
give you **cost tracking** per workload — but neither can answer *"cap each of
my end users at $1/day and cut them off when they hit it."*

This sample deploys a lightweight **per-user quota layer** for Amazon Bedrock
that does exactly that. It offers two enforcement modes (detailed below): a
**credential broker** that governs native `bedrock-runtime` calls across any
API and provider (recommended), and an OpenAI/Anthropic-compatible **inline
proxy** for the `bedrock-mantle` endpoint. Both:

- **Identity from your existing auth** — users send the JWT your application
  already issues (Amazon Cognito, Okta, Auth0, Entra ID, any OIDC IdP); the
  gateway verifies it against the issuer's JWKS and keys quotas on the `sub`
  claim (configurable). No gateway-issued API keys to manage.

> **Best fit: multi-tenant applications on Bedrock.** If you run a SaaS or
> internal platform where many tenants (customers, teams, projects) share one
> Bedrock account behind **one identity provider**, and you need to cap and
> attribute spend **per tenant** — not just watch the account total — this is
> built for you. Point `jwt_user_claim` at your tenant claim (e.g.
> `custom:tenant_id`) and every budget, block, metric, and log line is keyed
> per tenant with no code change. See
> [Multi-tenant: budget and attribute per tenant](#multi-tenant-budget-and-attribute-per-tenant).
>
> It assumes tenants are **authenticated and cooperating** (a typical B2B /
> internal-platform trust model), not anonymous or adversarial. See
> [Trust model](#trust-model) for exactly what that means and where the edges
> are.
- **Per-user budgets** in USD *and* input/output tokens, per UTC day, with
  optional **auto-provisioning** of first-seen users at default limits
- **Hard real-time admission control in Mode B** — over-budget reservations
  get an HTTP 429 before any tokens are spent upstream; Mode A instead has a
  bounded overspend window while its short-lived credentials remain valid
- **Accurate metering of streaming** responses (SSE), not just JSON ones
- **Per-user CloudWatch metrics + dashboard** (spend, tokens, throttles)
- **Async safety net** — a reconciler blocks users whose settled usage
  drifted over budget and sends SNS alerts, then auto-unblocks after the
  daily reset

The inference schemas remain OpenAI/Anthropic-compatible. Because the Lambda
Function URL is protected by IAM, callers also use the included `httpx`
SigV4 adapter: SigV4 occupies `Authorization`, while the end-user JWT travels
in `X-Quota-User-Token`.

## Two enforcement modes

This sample supports two ways to enforce per-user budgets. They share the
same identity model (JWT `sub`), DynamoDB tables, reconciler, and dashboard —
pick per workload.

### Mode A — Credential broker (any Bedrock API, any provider) — recommended

For apps that call Bedrock **natively** on the `bedrock-runtime` endpoint
(`InvokeModel`/`Converse`/streaming, **any** model provider) the gateway does
**not** proxy inference. Instead:

1. The app presents the user's JWT to the broker (`POST /v1/credentials`).
2. The broker checks the user's budget and, if within limits, returns
   **short-lived AWS credentials** via `sts:AssumeRole`, stamping the
   **`RoleSessionName`, `SourceIdentity`, and a `quota-user` session tag** with
   a sanitized, collision-resistant identity derived from the quota-identity
   claim (the full claim value is preserved in a reverse-map row).
3. The app calls `bedrock-runtime` **directly** with those creds — the gateway
   is out of the data path (no added latency, no protocol coupling).
4. Bedrock **model-invocation logging** (which captures the `bedrock-runtime`
   endpoint) records per-call token counts, and each record's identity carries
   the session name, which the reconciler maps back to the user/tenant to
   meter spend from Bedrock's own telemetry — uniformly across every provider.
5. Over budget → the user is blocked; their next credential refresh is
   refused, so they lose access at the current session's TTL.

> **Native path is `bedrock-runtime` only, by design.** The vended role does
> not grant `bedrock-mantle:*`, because the mantle endpoint is **not** captured
> by model-invocation logging — vended creds calling mantle would be unmetered.
> Use Mode B to govern mantle traffic.

```
                     ┌───────────── Broker (Lambda + Function URL, IAM auth) ─────────────┐
 app backend ───JWT──┤ 1 verify JWT (issuer JWKS), identity = configured claim            │
 (has IAM role)      │ 2 budget check → over budget? 403/429, no creds                     │
      │              │ 3 sts:AssumeRole  session name/SourceIdentity = sanitized id, TTL  │──▶ creds
      ▼              └────────────────────────────────────────────────────────────────────┘
 boto3 bedrock-runtime with vended creds ───────────────────────────▶ Bedrock (any provider)
                                                                          │ model-invocation logs
       EventBridge (5 min) ─▶ Reconciler ─▶ map session→identity, meter ─▶ block/unblock + SNS
```

**Enforcement is *bounded overspend*, not a hard pre-token cap:** a user can
keep spending until their vended session expires (tune with
`VENDED_CREDENTIAL_TTL_SECONDS`) plus telemetry/reconciler lag. In exchange
you get true API/provider-agnostic coverage with zero client protocol change
(only client *construction* uses the broker's creds). See
`examples/demo_native_calls.py`.

### Mode B — Inline proxy (mantle only, hard pre-spend cap)

```
                        ┌─────────────────────────────────────────────┐
 OpenAI / Anthropic SDK │  Gateway (Lambda + Function URL, streaming) │   bedrock-mantle
 SigV4 + user JWT header├─▶ 1 verify IAM caller + JWT (issuer JWKS),  ├──▶ Responses /
 base_url = gateway ────┤     quota identity = "sub" claim            │    Chat Completions /
        ▲               │   2 RESERVE worst case vs daily budget      │    Anthropic Messages
        │               │      └─ over budget → 429 (no spend)        │
   your IdP             │   3 forward (short-term Bedrock token       │
 (Cognito, Okta,        │      minted from the gateway's IAM role)    │
  Auth0, ...)           │   4 SETTLE counters w/ real usage (incl.    │
                        │      SSE) + per-user EMF metrics            │
                        └───────────────┬─────────────────────────────┘
                                        │ DynamoDB (users, usage)
              EventBridge (5 min) ──▶ Reconciler ──▶ block/unblock + SNS alerts
              CloudWatch dashboard: spend/user, tokens, throttles
```

## Multi-tenant: budget and attribute per tenant

The gateway keys every quota on **one configurable JWT claim** — the quota
*identity*. Set it once at deploy and the whole pipeline (budget check,
reserve/settle, the vended session's `SourceIdentity`, model-invocation-log
attribution, the reconciler, admin API, and CloudWatch dimensions) keys on
that value. Nothing else changes.

```bash
cdk deploy \
  -c jwt_issuer=https://your-idp.example.com/... \
  -c jwt_user_claim=custom:tenant_id \
  -c manage_invocation_logging=true
```

Now `custom:tenant_id` is the unit of budgeting: a limit set on
`tenant-acme` is shared by every user whose token carries that claim, and one
tenant can never spend against another's budget (the claim is signed by your
IdP and verified on every call). Common choices:

| `jwt_user_claim` | Budgeting unit | Use when |
|---|---|---|
| `sub` (default) | Individual end user | One budget per person |
| `custom:tenant_id` / `org_id` | Tenant / customer | B2B SaaS: cap each customer org |
| `custom:team` / `custom:project` | Team / cost center | Internal platform chargeback |

Set per-tenant limits through the admin API using the **claim value** as the
`user_id` (the field name is historical — it holds whatever
`jwt_user_claim` resolves to):

```python
from examples.sigv4_gateway import signed_request

signed_request(
    "POST", f"{GATEWAY_URL}/admin/users", admin_key=ADMIN_KEY,
    json={"user_id": "tenant-acme", "name": "ACME Corp", "daily_usd": 200},
)
```

**Assumptions and boundaries of this model:**

- **One IdP / one issuer per deployment.** Identity is a claim inside tokens
  from a single trusted issuer (`jwt_issuer`). This matches how tenant-per-
  claim SaaS is normally built, and mirrors the Claude Apps Gateway's own
  "one issuer per gateway — run separate instances" stance. Federating a
  *different* IdP per tenant is out of scope: it needs multi-issuer
  verification and issuer-namespaced identities (`{iss}#{claim}`) so subjects
  can't collide across issuers. `auth.py` verifies against one issuer today.
- **The claim must be present and signed.** A token missing the configured
  claim is rejected (401), so a tenant can't fall back to an unbudgeted
  identity. Make the claim a **required, IdP-populated** attribute — not one
  the client can set — so tenant A cannot mint a token claiming tenant B.
- **Cross-tenant isolation is by budget, not by model.** Per-tenant *spend*
  is isolated, but by default every tenant may call every enabled model. To
  give tenants (or tiers) different model access, scope the vended role
  (Mode A) or add a model allowlist — see [Trust model](#trust-model).

## Trust model

This sample is built for **authenticated, cooperating** callers — your own
application's tenants/users, behind your IdP and your app backend. It is a
governance and cost-control tool for that setting, **not** an anti-abuse
control for anonymous or adversarial users. Concretely:

- **Identity is only as trustworthy as the claim.** Budgets bind to a JWT
  claim your IdP signs. The gateway verifies the signature, `exp`, and
  (when configured) `iss`/`aud` — but it trusts the *content* of a valid
  token. If end users can influence the claim you budget on (e.g. a
  self-service-editable attribute), they can shift which budget they spend
  against. Budget on an IdP-controlled claim.
- **Enforcement is bounded overspend, not a hard cap** (Mode A). A within-
  budget tenant that pulls credentials right before its budget is exhausted
  can keep calling Bedrock until those creds expire
  (`VENDED_CREDENTIAL_TTL_SECONDS`, default 900s) plus reconciler lag
  (~5 min). Fine for cost control among cooperating tenants; **not** a
  defense against a tenant deliberately racing the window. Shorten the TTL
  to tighten the bound (at the cost of more `AssumeRole` calls), or use
  Mode B (inline proxy) for a hard pre-spend 429.
- **The account boundary still matters.** Per-tenant budgets are only
  authoritative if tenants can't call Bedrock directly, bypassing the
  gateway. See [Making the gateway the only path](#making-the-gateway-the-only-path-account-governance).
- **Token budgets are the hard lever; dollars are an estimate.** Token
  reservations are enforced exactly at admission. Dollar budgets are priced
  from an editable table (placeholders by default) and, for Mode A native
  traffic, cannot see prompt-cache discounts (model-invocation logs carry no
  cache-token fields). Treat dollar caps as close estimates and lead with
  token budgets where a precise ceiling matters.

If you need controls for **anonymous or adversarial** end users (per-IP rate
limits, sign-up abuse prevention, hard pre-token caps), this sample is a
starting point but not a complete solution — layer it behind WAF / API
Gateway throttling and prefer Mode B's pre-spend cap.

## What the gateway itself costs

Separate the solution's cost from the Bedrock spend it governs
(us-east-1 list prices; the driver is Lambda holding the stream open):

- **Per request** ≈ **$0.00014** (~$0.14 per 1,000): 1 GB Lambda × ~8 s
  average stream ($0.000133) + two DynamoDB on-demand writes
  (~$0.0000013) + EMF log ingest (~$0.0000005). Relative to the model call
  it wraps: ~0.3% on premium models, but **up to ~30% on the cheapest
  models** — if you front mostly ultra-cheap models at volume, drop Lambda
  memory to 512 MB (the proxy is I/O-bound) or use the Fargate path,
  where one 1 vCPU task (~$36/month) holds hundreds of concurrent streams.
- **Idle, deployed**: **< $1/month** (Secrets Manager $0.40, reconciler
  pennies, dashboard within the free tier). There is no always-on
  database, load balancer, or container.
- **SnapStart** (opt-in): ~$4/month per GB of cached snapshot.
- **Watch CloudWatch metric cardinality**: per-user metrics cost
  **~$1.50–3 per active user per month** ($0.30 per metric × per-user
  metric/dimension combos, prorated to active hours). Fine at tens of
  users; at hundreds+, keep per-user data in the EMF logs (queryable with
  Logs Insights for pennies) and publish only aggregates/top-N as metrics
  — this is the same cardinality ceiling noted under Scaling
  characteristics.

Rule of thumb: demo scale runs on single-digit dollars per month;
~100k requests/day runs around $400–450/month — a fraction of a percent
of the Bedrock spend it is budgeting, and typically less than the first
runaway workload it blocks.

## Scaling characteristics

Designed for **thousands of users and hundreds of requests/second** on the
default deployment. What scales, and where the ceilings are:

**Scales without tuning:**

- **Quota counters shard per user.** Reserve/settle writes hit each user's
  own `(user_id, window)` item — no shared counter, no lock. On-demand
  DynamoDB carries this to millions of users. Per single user, DynamoDB's
  ~1,000 writes/s/item ÷ 2 writes per request ≈ **~500 req/s per user** —
  far above any human or coding agent.
- **Auth is local**: JWKS keys cached per container, JWT verification is
  CPU-only, upstream Bedrock tokens are derived without network calls.

**Known ceilings (and the fix for each):**

1. **Lambda concurrency vs. long streams.** A streaming proxy holds one
   execution per open stream, so concurrency = simultaneous streams, not
   RPS. The default 1,000 concurrent executions ≈ ~50–100 sustained req/s
   at typical LLM latencies (raisable to tens of thousands). At very high
   sustained concurrency, paying Lambda GB-seconds to idle-wait on
   upstream costs more than containers — and the gateway is a plain ASGI
   app, so the **same code runs unchanged on Fargate/ECS behind an ALB**
   when you outgrow Lambda.
   - *Cold starts during scale-out*: deploy with `-c snapstart=true` to
     enable [Lambda SnapStart](https://docs.aws.amazon.com/lambda/latest/dg/snapstart.html)
     — new execution environments resume from a Firecracker microVM
     snapshot instead of cold-starting. The gateway's lazy-init design
     (HTTP pools, JWKS keys, and tokens are created on first request, not
     at init) means nothing stale is captured in the snapshot. Snapshot
     caching carries a small extra cost, which is why it's opt-in.
2. **Don't share identities.** One JWT subject fanned out across thousands
   of clients concentrates writes on one item (~500 req/s cap) and defeats
   per-user budgeting. One subject = one principal.
3. **O(N) scans** in the reconciler and `GET /admin/users` are fine to
   ~10k users; beyond that, use segment-parallel scans or a DynamoDB
   Streams-driven reconciler, and paginate the admin listing.
4. **Metric cardinality cost.** Per-user CloudWatch metrics cost
   ~$0.30/metric/month per user. At 10k+ users, keep per-user data in the
   EMF *logs* (queryable with Logs Insights) and publish only aggregates /
   top-N as metrics.

**Ceilings that are not the gateway's:** account-level
[bedrock-mantle TPM quotas](https://docs.aws.amazon.com/bedrock/latest/userguide/quotas-mantle.html)
remain the global throughput cap — the sum of user budgets can exceed what
the account can push, and upstream 429s are passed through. And the design
is **single-region**: reserve/settle atomicity depends on conditional
writes, which DynamoDB global tables do not coordinate across regions —
multi-region means one deployment (and budget window) per region.

## How this compares to existing solutions

Two related solutions exist for governing **Claude Code seats** specifically:

- [Guidance for Claude Code with Amazon Bedrock](https://github.com/aws-solutions-library-samples/guidance-for-claude-code-with-amazon-bedrock)
  (AWS Solutions Library) — OIDC federation to temporary AWS credentials plus
  OTEL-based monitoring and quota thresholds. Now in **maintenance mode**;
  its README directs new deployments to:
- [Claude Apps Gateway](https://code.claude.com/docs/en/claude-apps-gateway)
  (Anthropic, [deployable on AWS](https://github.com/aws-samples/anthropic-on-aws/tree/main/claude-apps-gateway))
  — a self-hosted gateway with corporate SSO, model allowlists, and spend
  caps for **Claude Code and Claude Desktop**. Requires an ECS/EKS
  container, RDS PostgreSQL, an internal ALB with private DNS, and VPN
  reachability.

Both are excellent for their scope — and both validate the pattern this
sample implements (OIDC identity → gateway → per-user attribution → spend
caps). This sample differs in three deliberate ways:

1. **Scope: any client, not just Claude apps.** It fronts the
   `bedrock-mantle` endpoint's full protocol surface (OpenAI Responses,
   Chat Completions, Anthropic Messages), so one deployment governs your
   application's end users, OpenAI/Anthropic SDK workloads, and LangChain
   apps with the same budgets, admin API, and dashboard. CLI tools require a
   SigV4-capable sidecar because they cannot sign Lambda Function URLs.
   Neither solution above can serve an OpenAI-protocol client.
2. **Enforcement model: admission control, not post-hoc caps.** Budgets
   are enforced by an atomic reserve→settle protocol *before* the request
   reaches the model (mirroring how bedrock-mantle admits requests against
   its own TPM quotas), backed by an async reconciler. Concurrent requests
   cannot overshoot the budget.
3. **Footprint: serverless.** One Lambda, two DynamoDB tables, no VPC, no
   database, no load balancer — deployable in minutes and billed per request,
   with a small client-side SigV4 adapter for IAM-protected invocation.

A third, **infrastructure-free** approach is worth knowing: per-developer
cost *attribution* using Cognito + IAM **session tags** + **cost-allocation
tags**, with **AWS Budgets** for alerts (e.g. [this walkthrough](https://builder.aws.com/content/3EMlLuVf7JSb1fwM8xeQfSi5cz5/deep-dive-on-managing-bedrock-in-claude-code-over-bedrock-with-aws-budgets-tags)).
The developer's own machine assumes a role tagged with their alias and calls
Bedrock; Cost Explorer then breaks spend down by tag. It shares this sample's
identity spine but stops at attribution — and its two structural limits are
the reason this sample exists:

- **It monitors, it doesn't enforce.** Budgets/Cost Explorer data lags
  **~24 hours**, so a runaway tenant is an email *tomorrow*, not a cutoff
  now. This sample blocks in minutes (bounded overspend) or pre-spend
  (Mode B).
- **Tags are self-attested.** The client sets its own session tag, fine for
  cooperating developers doing chargeback but not an authoritative per-tenant
  cap. This sample sets the identity **server-side** (`SourceIdentity`, set
  by the broker) so a tenant can't relabel its spend.

**Which to pick:**

| Your need | Use |
|---|---|
| Managed Claude Code / Desktop seats for employees, corporate SSO | Claude Apps Gateway |
| Per-developer Bedrock **cost visibility**, zero infrastructure, alerts are enough | Cognito + Budgets + cost-allocation tags |
| **Cap and cut off** spend **per tenant/user** across any Bedrock client, in minutes not a day | **This sample** |

If you need per-tenant or per-user budgets that are *enforced* — across a
mixed fleet of applications and coding agents on Bedrock — this sample is the
reference for that pattern.

## Why reserve → settle?

A naive "check the counter, then increment it" lets a user fire N concurrent
requests that each pass the check before any of them is counted. This gateway
**reserves the request's worst case** (estimated input tokens + `max_tokens`,
priced) in a single conditional DynamoDB update at admission, then **settles**
the counters down to real usage when the response (or stream) completes. This
is the same admission model the `bedrock-mantle` endpoint itself uses for its
TPM quotas, applied per user.

## Contents

| Path | What it is |
|---|---|
| `gateway/` | FastAPI app: JWT auth (OIDC/JWKS), credential broker (Mode A), quota reserve/settle + SSE metering proxy (Mode B), pricing, admin API |
| `reconciler/` | Scheduled Lambda: per-user metering from Bedrock model-invocation logs, drift blocking, auto-unblock, SNS alerts |
| `cdk/` | CDK app (Python): broker/gateway Lambda + IAM-auth Function URL, vended Bedrock role, model-invocation logging, DynamoDB, optional demo Cognito pool, EventBridge, SNS, dashboard |
| `examples/demo_native_calls.py` | Mode A demo: fetch per-user creds from the broker, call Bedrock natively (Converse + InvokeModel) |
| `examples/sigv4_gateway.py` | SigV4 adapters for `httpx`, OpenAI/Anthropic Python SDKs, and signed admin calls |
| `tests/` | 96 unit + end-to-end tests, no AWS account needed |
| `notebook/per_user_quota_demo.ipynb` | Walkthrough: deploy, sign in users, watch a 429 happen |
| `assets/architecture.drawio` | Editable architecture diagram (draw.io) |
| `DEMO.md` | 12-minute demo script + pre-demo runbook + failure playbook |

## Prerequisites

- An AWS account with access to the [`bedrock-mantle` endpoint](https://docs.aws.amazon.com/bedrock/latest/userguide/bedrock-mantle.html)
  and model access enabled for the models you route (defaults assume
  `openai.gpt-oss-120b`; check `GET /v1/models`)
- Python 3.12+, Node.js (for the CDK CLI), Docker (for CDK asset bundling)
- Bootstrapped CDK environment (`cdk bootstrap`)
- Permission to call `pricing:GetProducts` while the stack captures its
  deployment-time Bedrock price snapshot
- `pip install -r gateway/requirements.txt` for the signed client examples

## Deploy

**With your own IdP** (recommended — quotas follow the identities your app
already has):

```bash
cd cdk
pip install -r requirements.txt
cdk deploy \
  -c jwt_issuer=https://your-idp.example.com/... \
  -c jwt_audience=YOUR_APP_CLIENT_ID \
  -c jwt_user_claim=sub \
  -c manage_invocation_logging=true \
  -c alert_email=you@example.com
```

`jwt_audience`, `jwt_user_claim`, and `alert_email` are optional. OIDC
discovery supplies `jwks_uri`; use `-c jwt_jwks_url=https://...` only when
the provider does not expose a standard discovery document.

The stack queries AWS Price List API once when the price-snapshot custom
resource is created. It selects standard on-demand input/output token prices
for the configured catalog models and injects the resulting JSON into both
Lambdas. It does not refresh prices periodically or query Pricing during
inference. `ModelPriceSnapshot` in the stack outputs records the exact rates
captured by that deployment.

**Without an IdP**, omit `jwt_issuer` and the stack creates a **demo Cognito
User Pool** and wires the gateway to it (outputs `DemoUserPoolId` /
`DemoUserPoolClientId`; the demo notebook uses these).

`manage_invocation_logging=true` explicitly acknowledges that Bedrock model
invocation logging is one account-and-region-wide setting. In shared
accounts, preserve the existing configuration instead:

```bash
-c manage_invocation_logging=false \
-c invocation_log_group_name=/your/existing/bedrock/log-group
```

Outputs include the **GatewayUrl** and the **AdminKeySecretArn**. Fetch the
admin key:

```bash
aws secretsmanager get-secret-value --secret-id <AdminKeySecretArn> \
  --query SecretString --output text
```

## Use it

Users (or tenants — whatever `jwt_user_claim` resolves to) are
**auto-provisioned at default limits on their first request**. Convenient for
onboarding, but it means a newly-seen identity silently gets the default
budget: in a multi-tenant deployment, decide whether you want tenants to
appear automatically or be provisioned deliberately at onboarding. Disable
lazy creation with `AUTO_PROVISION_USERS=false` on the Lambda, then
pre-provision each identity by its claim value:

```python
from examples.sigv4_gateway import signed_request

signed_request(
    "POST", f"{GATEWAY_URL}/admin/users", admin_key=ADMIN_KEY,
    json={"user_id": "<sub-claim-value>", "daily_usd": 0.5},
).raise_for_status()
```

Alice uses the OpenAI SDK with the JWT she already has and the included
SigV4 `httpx` adapter:

```python
import httpx
from openai import OpenAI
from examples.sigv4_gateway import FunctionUrlSigV4Auth

client = OpenAI(
    base_url=f"{GATEWAY_URL}/v1",
    api_key="sigv4-managed",
    http_client=httpx.Client(
        auth=FunctionUrlSigV4Auth(alice_jwt, region="us-east-1")
    ),
)
response = client.responses.create(
    model="openai.gpt-oss-120b",
    input="Three bullet points on the CAP theorem.",
)
```

The same `httpx` auth adapter works with the Anthropic Python SDK
(`base_url=f"{GATEWAY_URL}/anthropic"`) and Chat Completions. All three
protocols served by `bedrock-mantle` are proxied and metered, including
`stream=True`. Every response carries `X-Quota-Limit-USD` /
`X-Quota-Remaining-USD` headers. When the budget is gone:

```
HTTP 429 {"error": {"type": "quota_exceeded", "message": "Daily quota exceeded
for user 'alice': ... The quota resets at 00:00 UTC."}}
```

Admin endpoints: `POST /admin/users`, `GET /admin/users`,
`GET /admin/users/{id}/usage`, `PUT /admin/users/{id}/limits`,
`PUT /admin/users/{id}/status`.

## Making the gateway the only path (account governance)

The gateway can only govern traffic that goes **through** it. Any IAM
principal in the account that holds `bedrock:InvokeModel*` or
`bedrock-mantle:*` permissions can still call the endpoints directly and
bypass the quotas. To make per-user limits authoritative for the account,
close the direct path so the **gateway's Lambda role is the only principal
allowed to invoke models**:

**Option A — attach the shipped deny policy (single account).** The stack
creates a customer-managed IAM policy (output
`DenyDirectBedrockPolicyArn`). Attach it to every human/dev/CI role that
should be quota-governed:

```bash
aws iam attach-role-policy --role-name <dev-role> \
  --policy-arn <DenyDirectBedrockPolicyArn>
```

**Option B — Service Control Policy (organization-wide, recommended).**
Denies direct invocation for every principal in the account **except the two
roles the gateway itself uses**: the gateway Lambda's role (output
`GatewayRoleArn`, used for Mode B's mantle proxy) **and** the per-user vended
role (output `BedrockUserRoleArn`, which Mode A's native calls run under).
Both must be excepted — omitting `BedrockUserRoleArn` denies every Mode A
native call, breaking the primary path:

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
        "aws:PrincipalArn": ["<GatewayRoleArn>", "<BedrockUserRoleArn>"]
      }
    }
  }]
}
```

(`aws:PrincipalArn` for an assumed-role session is the underlying **role**
ARN, so listing the two role ARNs covers every vended session.)

Also avoid issuing **long-term Bedrock API keys** to users (they are IAM
users under the hood and can call the endpoints directly); with the gateway
in place, end users never need Bedrock credentials of any kind — their IdP
JWT is enough.

## Using with coding agents

Coding agents are the heaviest per-user consumers of Bedrock in most
accounts. There are two valid integration patterns:

- **Native Bedrock clients, including Claude Code in Bedrock mode:** obtain
  short-lived credentials from `/v1/credentials`, then export the returned
  `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, and `AWS_SESSION_TOKEN`.
  `examples/demo_native_calls.py` implements the signed exchange. These calls
  use Mode A and are metered from Bedrock invocation logs.
- **OpenAI/Anthropic protocol agents:** run them in an application or
  organization-owned sidecar that applies `FunctionUrlSigV4Auth`. CLI tools
  that only support `base_url` plus a Bearer key cannot call the IAM-protected
  Function URL directly because they do not produce SigV4 signatures.

This distinction is intentional: making the Function URL public would restore
direct CLI compatibility, but would remove the IAM-authenticated backend
boundary. The Python SDK path remains fully streaming and meters Anthropic
prompt-cache usage plus token-counting calls.

Practical notes for agent workloads:

- Give developers **bigger budgets** than chat users — an afternoon of
  agentic coding can burn millions of (mostly cached) input tokens.
- Set token validity in your IdP to cover a work session (the demo Cognito
  client issues 12-hour ID tokens), or wire a refresh helper.
- When a developer hits their budget the agent receives a clean HTTP 429
  with the reset time in the message; the reconciler's SNS warning at 80%
  gives them advance notice.

## Monitoring

The gateway emits per-user metrics via CloudWatch Embedded Metric Format
(namespace `BedrockQuotaGateway`): `Requests`, `InputTokens`, `OutputTokens`,
`EstimatedCostUSD`, `LatencyMs`, `Throttles`, `Errors` with `UserId` /
`Model` dimensions. The stack creates a **bedrock-per-user-quota-gateway**
dashboard with spend, requests, and throttles per user.

You can cross-check gateway numbers against the service-side
[CloudWatch metrics for the bedrock-mantle endpoint](https://aws.amazon.com/about-aws/whats-new/2026/06/amazon-bedrock-supports-cloudwatch-metrics-bedrock-mantle-endpoint/).

## Important notes

- **JWT verification**: signatures are verified against the issuer's JWKS
  (`JWT_JWKS_URL`, or the `jwks_uri` from the issuer's OIDC discovery document);
  `exp` is always enforced, `iss`/`aud` when configured. Set
  `JWT_USER_CLAIM` if your quota identity isn't `sub` — e.g. `email`,
  `cognito:username`, or a tenant claim like `custom:tenant_id` to budget
  **per tenant** (see [Multi-tenant](#multi-tenant-budget-and-attribute-per-tenant)).
  A `JWT_SHARED_SECRET` HS256 mode exists for local
  dev/tests only. Note the gateway checks token *validity*, not revocation —
  keep token lifetimes short, and use the admin block endpoint for immediate
  cut-off.
- **Prices are a deployment-time snapshot.** AWS Price List API supplies the
  standard on-demand rates for configured catalog models. Recent models not
  yet published by that API require an explicit pinned override in the CDK
  stack. Unknown models are billed at the most expensive known rate on
  purpose. Price List rates are estimates and do not include private
  discounts, commitments, or credits.
- **Upstream auth**: the gateway mints short-term Bedrock API keys from its
  own IAM role (`aws-bedrock-token-generator`); no long-term secrets are
  stored. The role uses the `AmazonBedrockMantleInferenceAccess` managed
  policy — scope it down to specific Projects for production.
- **Pre-flight estimation** uses a chars/4 heuristic for input tokens plus
  the request's `max_tokens` for output. Reservations are worst-case by
  design; counters settle to actuals within milliseconds of completion.
- The Function URL uses **`AuthType: AWS_IAM`** — callers must SigV4-sign
  (your app backend's IAM role does this), so the URL is **not anonymously
  reachable**, satisfying the Palisade "world-accessible Lambda" policy.
  SigV4 owns `Authorization`; the end-user JWT travels in
  `X-Quota-User-Token`, and the admin key in `X-Quota-Admin-Key`. Grant
  specific caller roles with
  `-c invoker_principal_arns=arn1,arn2` (defaults to the account root).
- Daily windows reset at **00:00 UTC**; usage records expire from DynamoDB
  after 35 days, and broker session-map rows after two days (DynamoDB TTL).
- Warning notifications are emitted once per user per UTC window, not every
  reconciler run.

## Run the tests

```bash
pip install fastapi httpx pytest boto3 'PyJWT[crypto]'
pytest tests/ -q     # 96 tests, no AWS account or network needed
```

## Cleanup

```bash
cd cdk && cdk destroy
```

If the stack managed Bedrock invocation logging, that regional configuration,
its log group, and its writer role are retained intentionally. Review or
remove them manually only after confirming no other workload depends on them.

## Related samples

- [`cost-reporting/converse-metadata-cost-reporting`](../../cost-reporting/converse-metadata-cost-reporting/)
  — cost *reporting* per user/tag for the `bedrock-runtime` Converse API
- [`poc-to-prod/inference-profiles`](../inference-profiles/) — cost tracking
  with Application Inference Profiles on `bedrock-runtime`
- [`genai-use-cases/prompt-routing`](../../genai-use-cases/prompt-routing/)
  — cut spend further by routing prompts to the cheapest capable model
