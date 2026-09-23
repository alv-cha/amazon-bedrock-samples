---
tags:
    - Use cases
    - Cost-Optimization
    - API-Usage-Example
---
<!-- <h1> Cost-optimized custom prompt routing with Amazon Bedrock </h1> -->
!!! tip inline end "[Open in github](https://github.com/aws-samples/amazon-bedrock-samples/blob/main/genai-use-cases/prompt-routing/custom_prompt_router_w_bedrock.ipynb){:target="_blank"}"

> *This notebook should work well with the **`Python 3`** kernel in SageMaker Studio, or any environment with valid AWS credentials.*

<h2>Overview</h2>

Not every prompt needs your most powerful (and most expensive) model. A short
classification or a simple FAQ answer can be handled well by a small, cheap
model, while a multi-step reasoning task may genuinely need a premium one.
A **prompt router** inspects each incoming request and sends it to the
*cheapest model that can still answer it well* — cutting cost and latency
without a meaningful drop in quality.

In this notebook we build a **custom** router from scratch on top of the
[`bedrock-mantle` endpoint](https://docs.aws.amazon.com/bedrock/latest/userguide/bedrock-mantle.html)
— Amazon Bedrock's OpenAI- and Anthropic-compatible API surface — and
demonstrate **two complementary strategies**:

1. **Heuristic router (+ optional embeddings)** — a dependency-free difficulty
   heuristic (length + hard-reasoning keywords), optionally refined with
   Amazon Titan Text Embeddings similarity against per-tier reference prompts.
   The routing decision itself costs a fraction of a cent, or nothing at all.
2. **LLM-as-router** — ask a small, inexpensive model to read the prompt and
   return a structured JSON routing decision. More flexible and easier to
   extend with natural-language policies, at the cost of one extra small model
   call per request.

We then wrap both behind a single `route_and_invoke()` function and **compare
each strategy against an "all-large-model" baseline** on a mixed workload to
quantify the savings. Because the ladder spans **two providers** (OpenAI GPT-OSS
and Anthropic Claude), the router also demonstrates the per-family invocation
pattern the mantle endpoint requires.

<h2>Context</h2>

#### Why build your own router?

Amazon Bedrock offers a managed *Intelligent Prompt Routing* feature that
routes between two models of the same family. A **custom** router is worth
building when you want to:

- route across **different model families / providers** (this notebook routes
  between OpenAI GPT-OSS and Anthropic Claude models) rather than within one
  family;
- encode **your own** routing policy — task type, tenant tier, prompt length,
  cost ceilings, business rules — instead of a managed quality predictor;
- keep the routing logic fully **inspectable and testable** in your own code.

#### Why the `bedrock-mantle` endpoint?

The `bedrock-mantle` endpoint serves the **OpenAI Responses**, **OpenAI Chat
Completions**, and **Anthropic Messages** APIs with the vanilla OpenAI and
Anthropic SDKs — you only change the base URL and API key. It is the natural
home for a cross-provider router, with one important design consequence:
**there is no single shared API across families** (no Converse equivalent).
OpenAI models answer on the Responses API under `/v1`, Anthropic models on the
Messages API under `/anthropic/v1`. The router therefore resolves a *tier* to a
*family-specific client* — the routing logic itself stays model-agnostic.

#### The cost ladder

The whole idea rests on the large price spread between model tiers:

| Tier | Model | API | Relative cost | Good for |
|------|-------|-----|---------------|----------|
| `small`  | `openai.gpt-oss-20b`  | OpenAI Responses | \$ | short answers, classification, extraction, simple chat |
| `medium` | `openai.gpt-oss-120b` | OpenAI Responses | \$\$ | summarization, drafting, moderate reasoning |
| `large`  | `anthropic.claude-opus-4-7` | Anthropic Messages | \$\$\$ | multi-step reasoning, complex analysis, hard code |

If most of your traffic is simple and lands on `small`, the blended cost per
request drops dramatically — we measure exactly that at the end.

<h2>Prerequisites</h2>

- An AWS account with **Amazon Bedrock** access in a
  [region that supports the `bedrock-mantle` endpoint](https://docs.aws.amazon.com/bedrock/latest/userguide/bedrock-mantle.html#bedrock-mantle-supported)
  (this notebook defaults to `us-east-1`).
- **Model access** enabled for the models in the ladder (`openai.gpt-oss-20b`,
  `openai.gpt-oss-120b`, `anthropic.claude-opus-4-7`) — check what your account
  can see with the Models API cell below and edit the `TIERS` dict to match.
- **Authentication**: either set an
  [Amazon Bedrock API key](https://docs.aws.amazon.com/bedrock/latest/userguide/api-keys.html)
  in the `AWS_BEARER_TOKEN_BEDROCK` environment variable, or just have AWS
  credentials available — the notebook then mints a short-term key from your
  identity with `aws-bedrock-token-generator`.
- *(Optional)* Model access for **Amazon Titan Text Embeddings V2** if you want
  the embeddings-refined router. Embeddings are not offered on the mantle
  endpoint, so that one call uses `bedrock-runtime` via `boto3`.

> Inference on `bedrock-mantle` is governed by
> [separate per-model input/output tokens-per-minute quotas](https://docs.aws.amazon.com/bedrock/latest/userguide/quotas-mantle.html)
> from `bedrock-runtime`. The SDKs' built-in retries handle transient
> throttling.

<h2>Setup</h2>

Install the SDKs and configure the endpoint.

```python
%pip install --upgrade --quiet openai anthropic aws-bedrock-token-generator boto3
```

```python
import json
import os
import time

REGION = "us-east-1"  # any region where bedrock-mantle is offered
MANTLE = f"https://bedrock-mantle.{REGION}.api.aws"


def bedrock_api_key() -> str:
    """Bedrock API key: env var if set, else a short-term key from your role.

    Short-term keys last up to 12 hours; provide_token() caches and refreshes,
    so call this when (re)building clients rather than storing the string.
    """
    key = os.environ.get("AWS_BEARER_TOKEN_BEDROCK")
    if key:
        return key
    from aws_bedrock_token_generator import provide_token
    return provide_token(region=REGION)


print("Endpoint:", MANTLE)
```

<h3>Configure the model tiers</h3>

The ladder is *the* config surface: to change models, prices, or add a tier,
edit only this cell. Prices are **illustrative placeholders** for demonstrating
cost accounting — check the current Amazon Bedrock pricing page for real
numbers.

Each tier declares its **family**, which selects the API used to invoke it
(`openai` → Responses API, `anthropic` → Messages API).

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class ModelTier:
    name: str              # logical tier: "small" | "medium" | "large"
    model_id: str          # mantle model ID
    family: str            # "openai" | "anthropic" -> selects the client
    input_per_mtok: float  # USD per 1M input tokens  (PLACEHOLDER)
    output_per_mtok: float # USD per 1M output tokens (PLACEHOLDER)


# Ordered cheapest -> most capable. Cross-provider on purpose: that is what a
# custom router can do that managed Intelligent Prompt Routing cannot.
TIERS = {
    "small":  ModelTier("small",  "openai.gpt-oss-20b",         "openai",    0.07,  0.30),
    "medium": ModelTier("medium", "openai.gpt-oss-120b",        "openai",    0.15,  0.60),
    "large":  ModelTier("large",  "anthropic.claude-opus-4-7",  "anthropic", 15.00, 75.00),
}

# The small, cheap tier doubles as the LLM-as-router decision maker.
ROUTER_TIER = "small"


def estimate_cost(tier: ModelTier, input_tokens: int, output_tokens: int) -> float:
    return (input_tokens * tier.input_per_mtok + output_tokens * tier.output_per_mtok) / 1_000_000
```

Verify which models your account can actually see on the endpoint (edit
`TIERS` above if yours differ):

```python
from openai import OpenAI

available = [m.id for m in OpenAI(base_url=f"{MANTLE}/v1", api_key=bedrock_api_key()).models.list()]
for tier in TIERS.values():
    marker = "OK " if tier.model_id in available else "?? "
    print(f"{marker} {tier.name:<7} {tier.model_id}")
```

<h2>Building the router</h2>

<h3>Per-family invocation</h3>

Because the two families answer on different APIs, invocation goes through a
tiny factory: `openai` tiers use the **Responses API** via the OpenAI SDK,
`anthropic` tiers use the **Messages API** via the Anthropic SDK (note its
base URL is `<endpoint>/anthropic`, mirroring mantle's path layout). Both
return the same normalized dict — text, token usage, latency — so everything
downstream is family-agnostic.

```python
from anthropic import Anthropic

_openai_client = OpenAI(base_url=f"{MANTLE}/v1", api_key=bedrock_api_key())
_anthropic_client = Anthropic(base_url=f"{MANTLE}/anthropic", api_key=bedrock_api_key())


def invoke_model(tier: ModelTier, prompt: str, max_tokens: int = 512,
                 temperature: float | None = None) -> dict:
    """Invoke one tier's model through its family API and normalize the result."""
    started = time.time()

    if tier.family == "openai":
        kwargs = {"temperature": temperature} if temperature is not None else {}
        r = _openai_client.responses.create(
            model=tier.model_id, input=prompt, max_output_tokens=max_tokens, **kwargs,
        )
        text = r.output_text
        input_tokens, output_tokens = r.usage.input_tokens, r.usage.output_tokens

    elif tier.family == "anthropic":
        kwargs = {"temperature": temperature} if temperature is not None else {}
        r = _anthropic_client.messages.create(
            model=tier.model_id, max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}], **kwargs,
        )
        text = "".join(block.text for block in r.content if block.type == "text")
        input_tokens, output_tokens = r.usage.input_tokens, r.usage.output_tokens

    else:
        raise ValueError(f"unknown family {tier.family!r}")

    return {
        "text": text,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "latency_s": time.time() - started,
    }


# Smoke test with the cheapest tier:
print(invoke_model(TIERS["small"], "In one short sentence, what is a prompt router?", max_tokens=60)["text"])
```

<h3>Strategy 1 — heuristic router (+ optional embeddings)</h3>

The base heuristic needs **zero extra calls**: prompts containing
hard-reasoning keywords go `large`, very long prompts go at least `medium`,
everything else goes `small`.

Optionally, pass an `embed_fn` to refine the base decision with cosine
similarity against a handful of per-tier **reference prompts**. Embeddings are
not offered on the mantle endpoint, so the embedding function is *injectable*
— the next cell wires in Amazon Titan Text Embeddings via `bedrock-runtime`,
and you can skip it entirely.

```python
import math

HARD_KEYWORDS = ("prove", "derive", "design", "architecture", "step by step",
                 "diagnose", "optimize", "trade-off", "analyze", "justify")

REFERENCE_PROMPTS = {
    "small": [
        "What is the capital of France?",
        "Translate 'good morning' into Spanish.",
        "Classify this review as positive or negative: 'I loved it!'",
    ],
    "medium": [
        "Summarize the following paragraph in two sentences.",
        "Draft a polite email declining a meeting invitation.",
        "Explain what a REST API is to a non-technical manager.",
    ],
    "large": [
        "Prove that the square root of 2 is irrational, step by step.",
        "Design a fault-tolerant architecture for a global payment system and justify each choice.",
        "Given this failing function and stack trace, diagnose the root cause and propose a fix.",
    ],
}


def _cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _mean(vectors):
    return [sum(v[i] for v in vectors) / len(vectors) for i in range(len(vectors[0]))]


_centroid_cache = {}


def route_by_heuristic(prompt: str, embed_fn=None) -> str:
    """Pick a tier from keywords/length, optionally refined by embeddings."""
    lowered = prompt.lower()

    tier = "small"
    if embed_fn is not None:
        key = id(embed_fn)
        if key not in _centroid_cache:
            _centroid_cache[key] = {
                t: _mean([embed_fn(p) for p in prompts])
                for t, prompts in REFERENCE_PROMPTS.items()
            }
        vec = embed_fn(prompt)
        sims = {t: _cosine(vec, c) for t, c in _centroid_cache[key].items()}
        tier = max(sims, key=sims.get)

    # Heuristic nudges apply regardless of whether embeddings were used.
    if any(kw in lowered for kw in HARD_KEYWORDS):
        tier = "large"
    elif len(prompt) > 600 and tier == "small":
        tier = "medium"
    return tier


print(route_by_heuristic("What's the capital of Australia?"))
print(route_by_heuristic("Prove that there are infinitely many primes, step by step."))
```

*(Optional)* Wire in Titan Text Embeddings from `bedrock-runtime` as the
`embed_fn`. Skip this cell if you don't have Titan access — the heuristic
router works without it.

```python
import boto3

_bedrock_runtime = boto3.client("bedrock-runtime", region_name=REGION)


def titan_embed(text: str) -> list[float]:
    response = _bedrock_runtime.invoke_model(
        modelId="amazon.titan-embed-text-v2:0",
        body=json.dumps({"inputText": text, "dimensions": 256}),
    )
    return json.loads(response["body"].read())["embedding"]


print(route_by_heuristic("Draft a polite email declining a meeting invitation.", embed_fn=titan_embed))
```

<h3>Strategy 2 — LLM-as-router</h3>

Ask the `small` tier to classify the prompt into a tier and reply with JSON.
Two production notes baked in:

- the decision is parsed **best-effort** and **fails safe to `medium`** — a
  wrong cheap answer costs more than a right mid-tier one;
- the router call itself uses `temperature=0` and a tight `max_tokens`, so the
  overhead per request stays tiny.

```python
ROUTER_SYSTEM_PROMPT = """You are a routing classifier. Given a user prompt, \
decide which model tier should handle it and respond with ONLY a JSON object.

Tiers:
- "small":  simple factual questions, classification, extraction, short chat, translation.
- "medium": summarization, drafting, explanations, moderate reasoning.
- "large":  multi-step reasoning, complex analysis, math proofs, hard code, architecture/design.

Choose the CHEAPEST tier that can answer the prompt well. Respond in exactly this format:
{"tier": "small|medium|large", "reason": "<short justification>"}"""


def parse_routing_decision(raw: str) -> str:
    """Best-effort parse of the router model's JSON. Fail safe to 'medium'."""
    try:
        start, end = raw.find("{"), raw.rfind("}") + 1
        decision = json.loads(raw[start:end])
        tier = decision.get("tier", "medium")
        return tier if tier in TIERS else "medium"
    except (ValueError, KeyError):
        return "medium"


def route_by_llm(prompt: str) -> str:
    routing_prompt = (
        f"{ROUTER_SYSTEM_PROMPT}\n\nUser prompt:\n\"\"\"\n{prompt}\n\"\"\"\n\nJSON decision:"
    )
    result = invoke_model(TIERS[ROUTER_TIER], routing_prompt, max_tokens=100, temperature=0.0)
    return parse_routing_decision(result["text"])


print(route_by_llm("Translate 'thank you very much' into French."))
print(route_by_llm("Design a multi-region active-active architecture for a payment system."))
```

<h3>Putting it together: `route_and_invoke()`</h3>

One entry point: route (by either strategy, or `force_tier` for baselines),
invoke through the tier's family client, and record tokens, latency, and
estimated cost for every request — the record you'd persist in production.

```python
ROUTERS = {
    "heuristic": route_by_heuristic,
    "llm": route_by_llm,
}


def route_and_invoke(prompt: str, strategy: str = "heuristic",
                     force_tier: str | None = None, max_tokens: int = 512,
                     **router_kwargs) -> dict:
    if force_tier is not None:
        tier_name = force_tier
    else:
        tier_name = ROUTERS[strategy](prompt, **router_kwargs)

    tier = TIERS[tier_name]
    result = invoke_model(tier, prompt, max_tokens=max_tokens)
    cost = estimate_cost(tier, result["input_tokens"], result["output_tokens"])

    return {
        "prompt": prompt,
        "strategy": "forced" if force_tier is not None else strategy,
        "tier": tier_name,
        "model_id": tier.model_id,
        "family": tier.family,
        "answer": result["text"],
        "input_tokens": result["input_tokens"],
        "output_tokens": result["output_tokens"],
        "latency_s": round(result["latency_s"], 3),
        "est_cost_usd": round(cost, 6),
    }


route_and_invoke("What is the capital of Australia?", strategy="heuristic")
```

<h3>Comparing strategies on a workload</h3>

A mixed workload — six simple/medium prompts, three genuinely hard ones — run
through each router and through an **all-large baseline**. This mirrors a
realistic traffic mix where most requests are easy.

```python
WORKLOAD = [
    "What is the capital of Australia?",
    "Translate 'thank you very much' into French.",
    "Classify this ticket as bug or feature: 'The app crashes when I tap save.'",
    "Summarize the benefits of retrieval-augmented generation in two sentences.",
    "Draft a friendly reminder email about an overdue invoice.",
    "Explain the CAP theorem to a junior engineer.",
    "Prove that there are infinitely many prime numbers, step by step.",
    "Design a multi-region highly available architecture for a payment system and justify each component.",
    "Given a race condition in a queue, diagnose the cause and propose a locking strategy with trade-offs.",
]


def run_workload(label: str, **kwargs) -> dict:
    rows = [route_and_invoke(p, max_tokens=256, **kwargs) for p in WORKLOAD]
    total = sum(r["est_cost_usd"] for r in rows)
    dist = {}
    for r in rows:
        dist[r["tier"]] = dist.get(r["tier"], 0) + 1
    return {"label": label, "rows": rows, "total_cost": total, "tier_distribution": dist}


results = {}
for label, kwargs in [
    ("baseline (all large)", {"force_tier": "large"}),
    ("heuristic router", {"strategy": "heuristic"}),
    ("llm router", {"strategy": "llm"}),
]:
    results[label] = run_workload(label, **kwargs)

print(f"{'strategy':<22} {'total cost (USD)':>18}  tier distribution")
print("-" * 72)
baseline = results["baseline (all large)"]["total_cost"]
for label, r in results.items():
    print(f"{label:<22} {r['total_cost']:>18.6f}  {r['tier_distribution']}")

for label in ("heuristic router", "llm router"):
    saved = baseline - results[label]["total_cost"]
    print(f"\n{label} saved ${saved:.6f} ({100 * saved / baseline:.1f}%) vs all-large baseline")
```

Inspect where each prompt landed under one of the routers:

```python
for row in results["heuristic router"]["rows"]:
    print(f"[{row['tier']:<6}] {row['model_id']:<28} ${row['est_cost_usd']:.6f}  {row['prompt'][:50]}")
```

<h2>Best practices & other considerations</h2>

- **Fail safe, not cheap.** If the router errors or is uncertain, default to a
  *capable* tier (as `route_by_llm` does) rather than the cheapest — a wrong
  cheap answer costs more than a right expensive one.
- **Prefer plain-text JSON over schema enforcement for the router call.**
  Structured-output support varies by model and API on the mantle endpoint;
  the best-effort parse + fail-safe default here works with all of them.
- **Handle throttling with fallback.** `bedrock-mantle` enforces
  [per-model input/output TPM quotas](https://docs.aws.amazon.com/bedrock/latest/userguide/quotas-mantle.html)
  and may apply additional internal rate limiting. The OpenAI/Anthropic SDKs
  retry transient errors with backoff by default; on persistent 429s for a
  tier, consider falling back to an adjacent tier so the request still succeeds.
- **Attribute costs with Projects.** Associate router traffic with an
  [Amazon Bedrock Project](https://docs.aws.amazon.com/bedrock/latest/userguide/projects.html)
  (pass `project=...` to the OpenAI client) to see routed vs. baseline spend in
  Cost Explorer by tag.
- **Measure quality, not just cost.** Periodically sample routed traffic and
  score the cheap-tier answers (LLM-as-judge or human review); if quality on a
  category dips, raise its tier or add reference prompts.
- **Tune the boundaries.** The reference prompts and tier descriptions are the
  knobs. Curate them from your real traffic; a few well-chosen examples per
  tier beat many generic ones.
- **Log every decision.** Persist `{tier, model_id, tokens, latency, cost}` per
  request so you can audit routing quality and cost over time.
- **Combine the two strategies.** Use the near-free heuristic router for the
  clear cases and escalate to the LLM router only when the decision is
  ambiguous (e.g. similarity scores within a small margin).

<h2>Next steps</h2>

- Edit the `TIERS` dict to match **your** account's model access and real
  pricing — the ladder is fully declarative.
- Replace the reference-centroid heuristic with a **trained classifier** on
  embeddings once you have labeled traffic.
- Add **task-type routing** (code / summarization / reasoning) on top of the
  cost tiers for models that specialize.
- Put `route_and_invoke()` behind a gateway for a deployable service — the
  [per-user quota gateway sample](../../poc-to-prod/bedrock-mantle-per-user-quotas/)
  in this repository is a natural front door: it adds per-user budgets and
  enforcement on the same `bedrock-mantle` endpoint.
- Add an **evaluation harness** using the
  [evaluation-observe](../../evaluation-observe/) samples to track quality vs.
  cost as you tune the router.

<h2>Cleanup</h2>

This notebook creates **no persistent AWS resources** — it only makes on-demand
model invocations, which incur per-token charges while running and nothing
afterward. There is nothing to delete.

If you adapted the notebook to create Projects, gateways, or Lambda functions,
remember to remove those separately to avoid ongoing charges.
