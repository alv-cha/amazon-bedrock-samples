# Adaptive credential enforcement qualification

Updated: 2026-08-29

This record separates documentation-backed constraints, local verification,
and live AWS evidence. Do not promote an unmeasured mechanism by changing this
file's status without attaching the probe output and target account/Region.

## Live sandbox deployment

- Account: `199990519514` (`alvcha+custprojects1-admin`)
- Region: `us-east-1`
- Stack: `BedrockPerUserQuotaGateway`
- CloudFormation status: `CREATE_COMPLETE` on 2026-08-30
- Deployed mode: `lease`
- Actual STS duration: 900 seconds
- Effective permission lease: 300 seconds
- Bedrock invocation logging: stack-managed in
  `/bedrock/quota-gateway/model-invocations`; no previous configuration existed
- Admin UI and read-only Operations endpoint: deployed and smoke-tested
- Five-minute before/after authorization probes: **2/2 passed** with Anthropic
  Claude Haiku 4.5 (`anthropic.claude-haiku-4-5-20251001-v1:0`)
- Observed effective permission duration: 299 seconds in both samples
- Observed underlying STS duration: 899 seconds in both samples
- Pre-deadline `CountTokens`: authorized, 26 input tokens in both samples
- Post-deadline result with the same keys: `AccessDeniedException`, observed
  0.157 and 0.223 seconds after the fixed deadline
- Credential material printed: no
- One-minute lease, invocation-log latency/load, revocation, in-flight stream,
  and emergency mutation tests: not run

## Pricing correction impact (2026-09-01)

The first successful Opus stress run was priced through the conservative
`$15/$75` fallback because `us.anthropic.claude-opus-4-7` was absent from the
snapshot. Its 4,896 input + 139,264 output tokens were recorded as `$10.51824`.
Using the corrected US geographic-profile rates `$5.50/$27.50`, the same token
base is `$3.856688` (a `-$6.661552` correction) and would not cross the `$10`
quota. That historical DynamoDB aggregate and blocked status are not rewritten
by a pricing deployment; a new stress identity is required for clean evidence
unless an explicit data migration is approved.

## Current decision matrix

| Mechanism | Local status | Live status | Deployment decision |
|---|---|---|---|
| 15-minute STS, renewal-only block | Existing tests pass | Existing deployed behavior; no new mutation performed | `legacy` remains default |
| 1-minute permission lease | Policy/API/unit/CDK tests pass | Pending dedicated sandbox probe | Opt-in only |
| 5-minute permission lease | Policy/API/unit/CDK tests pass | Deadline probe passed in account `199990519514`, `us-east-1` on 2026-08-30 | Candidate default after load and detection-lag gates |
| 15-minute permission lease | Policy/API/unit/CDK tests pass | Pending dedicated sandbox probe | Opt-in only |
| 60-minute STS + targeted `SourceIdentity` deny | Sharding/failure/CDK tests pass | Pending propagation/isolation probe | Experimental only |
| 8-hour STS from Lambda broker | Rejected by configuration tests | Not run; AWS documents one-hour role-chaining maximum | No-go in current architecture |
| Role-wide emergency stop | API/state-machine/IAM/CDK tests pass | Pending sandbox activation/recovery exercise | Do not invoke without operator confirmation |

## Local evidence

- The compact permission-lease session policy is 270 characters in the guarded
  dry run, below STS's 2,048-character plaintext limit.
- Effective deadlines are capped to the authenticating JWT `exp`.
- Logical lease tests cover conditional reservation, non-extending same-ID
  retries after STS failure, refresh windows, jitter, rate limiting, and a
  second strongly consistent block/emergency check before STS. A fully atomic
  multi-item linearization remains a future hardening option; the fixed lease
  deadline bounds the residual final-call race.
- The lazy botocore provider performs no broker call at construction, coalesces
  concurrent first use, reuses credentials between refreshes, rotates lease IDs
  only after success, and reuses the same ID for transport retries.
- Revocation tests cover exact persisted `SourceIdentity`, deterministic policy
  sharding, 6,144-character capacity checks, last-known-good preservation on
  overflow, managed-policy version rotation, IAM failure retry, DLQ/alarm
  infrastructure, and periodic reconciliation.
- Emergency tests cover separate break-glass authorization, explicit
  confirmation, vending-gate ordering, generation-aware opposite transitions,
  unconditional deny/no-op policy versions, retryable pending state, audit
  state, failure retention, scheduled reconciliation, and recovery ordering.
- Lease and revocation CDK modes synthesize locally. No deployment was run.

## Live probe prerequisites

Record all of these before execution:

- Dedicated non-production AWS profile:
- Account ID:
- Region:
- Dedicated sandbox role ARN:
- CountTokens-compatible model ID:
- Role tags proving non-production:
- Approver and approval timestamp:

The role must trust the selected caller for `sts:AssumeRole` and
`sts:SetSourceIdentity`, allow the tested Bedrock Runtime actions, and have a
dedicated pre-attached managed deny policy safe for temporary
`CreatePolicyVersion`/default-version changes. Never use an untagged shared or
production role.

## Required live results

Attach the JSON output from `spikes/lease_revocation_probe.py` and record:

| Check | Samples | p50 | p95 | Maximum | Pass criterion | Result |
|---|---:|---:|---:|---:|---|---|
| 1-minute policy denial after deadline |  |  |  |  | New calls denied | Pending |
| 5-minute policy denial after deadline | 2 | 0.190s | 0.223s | 0.223s | New calls denied | Passed 2/2; both 299s effective vs 899s STS |
| Targeted deny propagation |  |  |  |  | Under 300s; second user unaffected | Pending |
| Deny removal propagation |  |  |  |  | Under 300s | Pending |
| One-hour role-chained session |  |  |  |  | Succeeds | Pending |
| 3,601-second role-chained session |  |  |  |  | Rejected | Pending |
| In-flight stream |  |  |  |  | Behavior documented; completion allowed | Pending |

Also measure invocation-log delivery plus processing separately using
`DetectionLagMilliseconds`; do not combine it with IAM or lease cutoff and
present the total as one service guarantee.

## Production promotion gates

### Five-minute lease candidate default

All must be true:

1. Both deadline probes deny new Bedrock calls as designed.
2. Lazy provider load test passes at the expected concurrently active user
   count with refresh jitter and no synchronized spike.
3. Broker Lambda, DynamoDB, and shared regional STS quotas retain approved
   headroom.
4. Operational runbook distinguishes `expiration` from `sts_expiration`.

### One-minute lease

Requires the five-minute gates plus an explicit need for the tighter
post-detection cutoff and a higher-frequency refresh load test. It remains
opt-in even if it passes.

### Sixty-minute revocation

All must be true:

1. Targeted deny never affects the control identity.
2. Every observed propagation sample is below five minutes.
3. Worst-case simultaneous blocked identities fit the immutable 19-shard
   managed-policy set by actual serialized size, and the account supports the
   resulting 20 role policy attachments including emergency stop.
4. IAM throttling, overflow, DLQ, stale-state, and reconciliation alarms are
   owned and exercised.
5. Security review accepts the narrowly scoped managed-policy versioning
   permissions and permissions boundary.

Until these gates are recorded, keep production configuration on `legacy` or a
separately approved lease mode; do not describe revocation as immediate.
