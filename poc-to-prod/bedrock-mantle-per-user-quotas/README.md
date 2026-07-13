# Per-user quota monitoring and enforcement for Amazon Bedrock (bedrock-mantle)

Amazon Bedrock enforces [tokens-per-minute quotas for the `bedrock-mantle`
endpoint](https://docs.aws.amazon.com/bedrock/latest/userguide/quotas-mantle.html)
at the **account level**, and [Projects](https://docs.aws.amazon.com/bedrock/latest/userguide/projects.html)
give you **cost tracking** per workload — but neither can answer *"cap each of
my end users at $1/day and cut them off when they hit it."*

This sample deploys a lightweight, OpenAI/Anthropic-compatible **quota gateway**
in front of the `bedrock-mantle` endpoint that does exactly that:

- **Identity from your existing auth** — users send the JWT your application
  already issues (Amazon Cognito, Okta, Auth0, Entra ID, any OIDC IdP); the
  gateway verifies it against the issuer's JWKS and keys quotas on the `sub`
  claim (configurable). No gateway-issued API keys to manage.
- **Per-user budgets** in USD *and* input/output tokens, per UTC day, with
  optional **auto-provisioning** of first-seen users at default limits
- **Hard real-time enforcement** — over-budget requests get an HTTP 429
  *before* any tokens are spent upstream
- **Accurate metering of streaming** responses (SSE), not just JSON ones
- **Per-user CloudWatch metrics + dashboard** (spend, tokens, throttles)
- **Async safety net** — a reconciler blocks users whose settled usage
  drifted over budget and sends SNS alerts, then auto-unblocks after the
  daily reset

Clients don't change: they keep using the vanilla OpenAI or Anthropic SDK and
only point `base_url` at the gateway, passing the user's JWT as the key.

## Two enforcement modes

This sample supports two ways to enforce per-user budgets. They share the
same identity model (JWT `sub`), DynamoDB tables, reconciler, and dashboard —
pick per workload.

### Mode A — Credential broker (any Bedrock API, any provider) — recommended

For apps that call Bedrock **natively** (`bedrock-runtime`
`InvokeModel`/`Converse`/streaming, **any** model provider) — or that use the
mantle endpoint — the gateway does **not** proxy inference. Instead:

1. The app presents the user's JWT to the broker (`POST /v1/credentials`).
2. The broker checks the user's budget and, if within limits, returns
   **short-lived AWS credentials** via `sts:AssumeRole` with
   **`RoleSessionName` + `SourceIdentity` = the JWT `sub`**.
3. The app calls Bedrock **directly** with those creds — the gateway is out
   of the data path (no added latency, no protocol coupling).
4. Bedrock **model-invocation logging** records per-call token counts, and
   each record's identity carries the session (= `sub`), so the reconciler
   meters spend **per user** from Bedrock's own telemetry — uniformly across
   every API and provider.
5. Over budget → the user is blocked; their next credential refresh is
   refused, so they lose access at the current session's TTL.

```
                     ┌───────────── Broker (Lambda + Function URL, IAM auth) ─────────────┐
 app backend ───JWT──┤ 1 verify JWT (issuer JWKS), identity = "sub"                        │
 (has IAM role)      │ 2 budget check → over budget? 403/429, no creds                     │
      │              │ 3 sts:AssumeRole  RoleSessionName + SourceIdentity = sub, short TTL │──▶ creds
      ▼              └────────────────────────────────────────────────────────────────────┘
 boto3 bedrock-runtime (or mantle) with vended creds ───────────────▶ Bedrock (any API/provider)
                                                                          │ model-invocation logs
              EventBridge (5 min) ─▶ Reconciler ─▶ meter per sub ─▶ block/unblock + SNS alerts
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
 base_url = gateway ────┼─▶ 1 verify the user's JWT (issuer JWKS),    ├──▶ Responses /
 api_key  = user JWT ───┤     quota identity = "sub" claim            │    Chat Completions /
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
   application's end users, OpenAI-SDK workloads, **Codex CLI**, LangChain
   apps, *and* Claude Code with the same budgets, admin API, and dashboard.
   Neither solution above can serve an OpenAI-protocol client.
2. **Enforcement model: admission control, not post-hoc caps.** Budgets
   are enforced by an atomic reserve→settle protocol *before* the request
   reaches the model (mirroring how bedrock-mantle admits requests against
   its own TPM quotas), backed by an async reconciler. Concurrent requests
   cannot overshoot the budget.
3. **Footprint: serverless.** One Lambda, two DynamoDB tables, no VPC, no
   database, no load balancer, no client-side binaries — deployable in
   minutes and billed per request, which matters when the goal is "let any
   customer try per-user budgets this afternoon."

If your only need is managed Claude Code seats for developers, evaluate
Claude Apps Gateway first. If you need per-user budgets across a mixed
fleet of applications and coding agents on Bedrock, this sample is the
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
| `tests/` | 69 unit + end-to-end tests, no AWS account needed |
| `notebook/per_user_quota_demo.ipynb` | Walkthrough: deploy, sign in users, watch a 429 happen |
| `assets/architecture.drawio` | Editable architecture diagram (draw.io) |
| `DEMO.md` | 12-minute demo script + pre-demo runbook + failure playbook |

## Prerequisites

- An AWS account with access to the [`bedrock-mantle` endpoint](https://docs.aws.amazon.com/bedrock/latest/userguide/bedrock-mantle.html)
  and model access enabled for the models you route (defaults assume
  `openai.gpt-oss-120b`; check `GET /v1/models`)
- Python 3.12+, Node.js (for the CDK CLI), Docker (for CDK asset bundling)
- Bootstrapped CDK environment (`cdk bootstrap`)

## Deploy

**With your own IdP** (recommended — quotas follow the identities your app
already has):

```bash
cd cdk
pip install -r requirements.txt
cdk deploy \
  -c jwt_issuer=https://your-idp.example.com/... \
  -c jwt_audience=<your-app-client-id> \        # optional but recommended
  -c jwt_user_claim=sub \                       # optional, default "sub"
  -c alert_email=you@example.com                # optional
```

**Without an IdP**, omit `jwt_issuer` and the stack creates a **demo Cognito
User Pool** and wires the gateway to it (outputs `DemoUserPoolId` /
`DemoUserPoolClientId`; the demo notebook uses these).

Outputs include the **GatewayUrl** and the **AdminKeySecretArn**. Fetch the
admin key:

```bash
aws secretsmanager get-secret-value --secret-id <AdminKeySecretArn> \
  --query SecretString --output text
```

## Use it

Users are **auto-provisioned at default limits on their first request**
(disable with `AUTO_PROVISION_USERS=false` on the Lambda). To give a specific
user non-default limits, pre-provision them by their IdP subject:

```bash
curl -X POST "$GATEWAY_URL/admin/users" \
  -H "Authorization: Bearer $ADMIN_KEY" -H "Content-Type: application/json" \
  -d '{"user_id": "<sub-claim-value>", "daily_usd": 0.5}'
```

Alice uses the plain OpenAI SDK with the JWT she already has from signing in
to your app:

```python
from openai import OpenAI

client = OpenAI(base_url=f"{GATEWAY_URL}/v1", api_key=alice_jwt)
response = client.responses.create(
    model="openai.gpt-oss-120b",
    input="Three bullet points on the CAP theorem.",
)
```

…or the Anthropic SDK (`base_url=f"{GATEWAY_URL}/anthropic"`, mirroring
mantle's own `/anthropic/v1/messages` path), or Chat Completions — all three
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
Denies direct invocation for every principal in the account except the
gateway role (output `GatewayRoleArn`):

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
      "ArnNotLike": {"aws:PrincipalArn": "<GatewayRoleArn>"}
    }
  }]
}
```

Also avoid issuing **long-term Bedrock API keys** to users (they are IAM
users under the hood and can call the endpoints directly); with the gateway
in place, end users never need Bedrock credentials of any kind — their IdP
JWT is enough.

## Using with coding agents (Claude Code, Codex CLI)

Coding agents are the heaviest per-user consumers of Bedrock in most
accounts, and both major CLIs support custom endpoints — so their usage can
be budgeted per developer through this gateway. The gateway meters what
they actually do: SSE streaming, **Anthropic prompt caching** (cache
read/write tokens are counted and priced with configurable multipliers —
crucial, since most of a coding agent's input arrives as cache reads), and
the `count_tokens` endpoint is proxied for free.

**Claude Code** — point it at the gateway instead of Bedrock-direct:

```bash
export ANTHROPIC_BASE_URL="<GatewayUrl>"          # no /v1 suffix
export ANTHROPIC_AUTH_TOKEN="<the developer's JWT>"
export ANTHROPIC_MODEL="anthropic.claude-opus-4-7" # a mantle Claude model id
claude
```

For unattended refresh of short-lived JWTs, use Claude Code's
`apiKeyHelper` setting (`~/.claude/settings.json`) pointing at a script
that returns a fresh token from your IdP. **Do not use
`CLAUDE_CODE_USE_BEDROCK=1`** — that mode signs requests straight to
`bedrock-runtime` and bypasses the gateway (the lockdown SCP above blocks
it, which is exactly what you want).

**Codex CLI** — add a provider in `~/.codex/config.toml`:

```toml
model = "openai.gpt-oss-120b"        # a mantle model id
model_provider = "quota-gateway"

[model_providers.quota-gateway]
name = "Amazon Bedrock via quota gateway"
base_url = "<GatewayUrl>/v1"
env_key = "GATEWAY_JWT"              # Codex sends this env var as the Bearer token
wire_api = "responses"
```

```bash
export GATEWAY_JWT="<the developer's JWT>"
codex
```

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
(namespace `BedrockMantleGateway`): `Requests`, `InputTokens`, `OutputTokens`,
`EstimatedCostUSD`, `LatencyMs`, `Throttles`, `Errors` with `UserId` /
`Model` dimensions. The stack creates a **bedrock-mantle-quota-gateway**
dashboard with spend, requests, and throttles per user.

You can cross-check gateway numbers against the service-side
[CloudWatch metrics for the bedrock-mantle endpoint](https://aws.amazon.com/about-aws/whats-new/2026/06/amazon-bedrock-supports-cloudwatch-metrics-bedrock-mantle-endpoint/).

## Important notes

- **JWT verification**: signatures are verified against the issuer's JWKS
  (`JWT_JWKS_URL`, derived from `JWT_ISSUER/.well-known/jwks.json` if unset);
  `exp` is always enforced, `iss`/`aud` when configured. Set
  `JWT_USER_CLAIM` if your quota identity isn't `sub` (e.g. `email`,
  `cognito:username`). A `JWT_SHARED_SECRET` HS256 mode exists for local
  dev/tests only. Note the gateway checks token *validity*, not revocation —
  keep token lifetimes short, and use the admin block endpoint for immediate
  cut-off.
- **Prices are placeholders.** Edit `gateway/app/pricing.py` (or set the
  `MODEL_PRICES_JSON` Lambda environment variable) with current values from
  the Bedrock pricing page. Unknown models are billed at the most expensive
  known rate on purpose.
- **Upstream auth**: the gateway mints short-term Bedrock API keys from its
  own IAM role (`aws-bedrock-token-generator`); no long-term secrets are
  stored. The role uses the `AmazonBedrockMantleInferenceAccess` managed
  policy — scope it down to specific Projects for production.
- **Pre-flight estimation** uses a chars/4 heuristic for input tokens plus
  the request's `max_tokens` for output. Reservations are worst-case by
  design; counters settle to actuals within milliseconds of completion.
- The Function URL uses **`AuthType: AWS_IAM`** — callers must SigV4-sign
  (your app backend's IAM role does this), so the URL is **not anonymously
  reachable**, satisfying the Palisade "world-accessible Lambda" policy. The
  end-user JWT still rides in the header and drives the per-user identity:
  SigV4 at the edge (*who may call the gateway*) + JWT in the app (*which
  user is spending*) — defense in depth. Grant specific caller roles with
  `-c invoker_principal_arns=arn1,arn2` (defaults to the account root).
- Daily windows reset at **00:00 UTC**; usage records expire from DynamoDB
  after 35 days (TTL).

## Run the tests

```bash
pip install fastapi httpx pytest boto3 'PyJWT[crypto]'
pytest tests/ -q     # 65 tests, no AWS account or network needed
```

## Cleanup

```bash
cd cdk && cdk destroy
```

## Related samples

- [`cost-reporting/converse-metadata-cost-reporting`](../../cost-reporting/converse-metadata-cost-reporting/)
  — cost *reporting* per user/tag for the `bedrock-runtime` Converse API
- [`poc-to-prod/inference-profiles`](../inference-profiles/) — cost tracking
  with Application Inference Profiles on `bedrock-runtime`
- [`genai-use-cases/prompt-routing`](../../genai-use-cases/prompt-routing/)
  — cut spend further by routing prompts to the cheapest capable model
