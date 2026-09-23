# Cost-optimized custom prompt routing with Amazon Bedrock

Not every prompt needs your most powerful (and most expensive) model. A
**prompt router** inspects each incoming request and sends it to the *cheapest
model that can still answer it well*, cutting cost and latency without a
meaningful drop in quality.

This sample builds a **custom** router from scratch — as opposed to Amazon
Bedrock's managed *Intelligent Prompt Routing* feature — on top of the
[`bedrock-mantle` endpoint](https://docs.aws.amazon.com/bedrock/latest/userguide/bedrock-mantle.html),
Amazon Bedrock's OpenAI- and Anthropic-compatible API surface. The routing
logic is fully yours, spans **multiple model families/providers**, and is easy
to inspect, test, and extend.

## Contents

- [`custom_prompt_router_w_bedrock.ipynb`](./custom_prompt_router_w_bedrock.ipynb)
  — the walkthrough notebook.

## What it demonstrates

Two complementary routing strategies over a cross-provider, three-tier cost
ladder (`openai.gpt-oss-20b` → `openai.gpt-oss-120b` →
`anthropic.claude-opus-4-7`):

1. **Heuristic router (+ optional embeddings)** — a dependency-free difficulty
   heuristic (length, hard-reasoning keywords), optionally refined with Amazon
   Titan Text Embeddings similarity against per-tier reference prompts. The
   routing decision costs a fraction of a cent, or nothing at all.
2. **LLM-as-router** — a small, cheap model reads the prompt and returns a
   JSON routing decision, parsed best-effort with a fail-safe default. More
   flexible; adds one small model call per request.

Because the mantle endpoint has **no single shared API across families**
(OpenAI models answer on the Responses API, Anthropic models on the Messages
API), the router resolves each tier to a **family-specific client** — using the
vanilla OpenAI and Anthropic SDKs pointed at the Bedrock endpoint. Both
strategies sit behind a single `route_and_invoke()` function that records
tokens, latency, and estimated cost, and the notebook **compares** each
strategy against an "all-large-model" baseline on a mixed workload to quantify
the savings.

## Prerequisites

- Amazon Bedrock access in a [region that offers the `bedrock-mantle`
  endpoint](https://docs.aws.amazon.com/bedrock/latest/userguide/bedrock-mantle.html#bedrock-mantle-supported)
  (defaults to `us-east-1`).
- Model access enabled for the models in the `TIERS` dict (and optionally
  Amazon Titan Text Embeddings V2 for the embeddings-refined router, invoked
  via `bedrock-runtime`).
- Either an [Amazon Bedrock API key](https://docs.aws.amazon.com/bedrock/latest/userguide/api-keys.html)
  in `AWS_BEARER_TOKEN_BEDROCK`, or AWS credentials — the notebook mints a
  short-term key with `aws-bedrock-token-generator`.

## Related samples

- [`poc-to-prod/bedrock-mantle-per-user-quotas`](../../poc-to-prod/bedrock-mantle-per-user-quotas/)
  — put the router behind a per-user quota gateway on the same endpoint for
  budgets and enforcement.
- [`evaluation-observe`](../../evaluation-observe/) — measure whether routing
  preserves answer quality.
