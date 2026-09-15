# Threat model

STRIDE review of Bedrock Spend Controls against the trust boundaries the
code actually establishes. Every mitigation cites the file that implements
it; status is **Mitigated** (control exists and is tested), **Accepted**
(known residual risk, documented, not planned), or **Open** (no control;
listed so it is not forgotten). A machine-readable copy is in
[`threat-model.json`](threat-model.json) for diffing between reviews.

Reviewed: 2026-09-15, against the `per-user-quotas` branch after the
multi-dimension pricing, thresholds/rate-limit, model-budget, and
reconciliation changes.

## What this system protects

The asset is **the Bedrock bill**, bounded per identity, tenant, or
workload. Confidentiality of prompts is out of scope by construction: the
broker never sees inference traffic, and the managed invocation-logging
configuration disables payload delivery. The integrity properties that
matter are (a) usage is attributed to the right subject, (b) a blocked
subject loses access within the documented bound, and (c) administrative
changes are authorized and audited.

## Trust boundaries

| # | Boundary | Crosses | Implemented by |
|---|---|---|---|
| B1 | Application / end user → broker | OIDC JWT in `X-Quota-User-Token`; SigV4 on the `AWS_IAM` Function URL | `gateway/app/auth.py`, `gateway/app/main.py:_authenticate`, `cdk/stacks/spend_controls_stack.py` Function URL + `invoker_principal_arns` |
| B2 | Broker → STS / IAM | `AssumeRole` with `RoleSessionName = SourceIdentity = sanitized claim`, inline session policy | `gateway/app/broker.py`, `gateway/app/session_policy.py`, `BedrockUserRole` + permissions boundary in the stack |
| B3 | Vended session → Bedrock | IAM authorization of `bedrock:InvokeModel*` / `CountTokens` on `allowed_model_arns`; invocation log is the metering truth | Role policy, boundary, session policy; Bedrock model-invocation logging |
| B4 | Invocation logs → usage processor → DynamoDB | CloudWatch Logs subscription; `identity.arn` and `modelId` from the record; transactional ledger writes | `usage_processor/handler.py` |
| B5 | DynamoDB streams → enforcement processors → IAM policy versions | `REVOCATION#` sentinels and `CONFIG#EMERGENCY_STOP`; `CreatePolicyVersion` on pre-attached policies; `PutRolePolicy` on workload roles | `enforcement_dispatcher/`, `revocation_processor/`, `emergency_processor/`, `workload_enforcer/` |
| B6 | Admin UI / API → admin mutations | Shared admin key or admin JWT claim; separate break-glass key; `If-Match` + `Idempotency-Key` | `gateway/app/main.py:_require_admin`, `_require_emergency_admin`, `gateway/app/quota.py` admin mutations, admin-audit table |
| B7 | Operator / account administrator → out-of-band IAM, SCP, logging config | Console/CLI with account privileges | Not controllable by the sample; `DenyDirectBedrockPolicy`, SCP guidance |

## Threats

### B1 — Application / end user → broker

**T-01 · Spoofing · JWT forgery or replay.**
An attacker presents a fabricated or captured token to obtain credentials
under another identity.
*Mitigation:* signature verified against the issuer's JWKS (RS/ES) or the
dev-only HS256 secret; `iss`, `aud` (any of the configured list), `exp`
enforced — `gateway/app/auth.py:JwtVerifier.verify`. Discovery and JWKS
URLs must be HTTPS (`test_auth.py::test_oidc_discovery_rejects_non_https_*`).
The lease deadline is capped at the token's `exp`
(`main.py:_reserve_permission_lease`, `expires_no_later_than=jwt_expiration`),
so a replayed token cannot mint credentials that outlive it.
*Residual:* a valid token stolen before `exp` is usable until then; there is
no `jti` replay cache. **Status: Accepted** — the caller must also hold an
IAM principal allowed on the Function URL (SigV4), so token theft alone is
insufficient.

**T-02 · Spoofing / Elevation · `jwt_user_claim` chosen or IdP misconfigured so
users control their own quota identity.**
If the quota claim is user-editable at the IdP (e.g. a self-service
`preferred_username`), one user can spend another's budget.
*Mitigation:* none in code beyond rejecting reserved prefixes
(`quota.py:validate_user_id`, `RESERVED_USER_ID_PREFIXES`) and the
`workload:` namespace (`main.py:_authenticate`). Documentation directs
operators to a stable, IdP-controlled claim (DEPLOYMENT.md "Decisions
before deployment"). **Status: Accepted** — IdP configuration is outside the
sample; the default `sub` is safe.

**T-03 · Denial of service · Vend flooding.**
A client refreshes credentials in a loop, exhausting STS (600 rps regional)
or DynamoDB.
*Mitigation:* per-identity vend rate limit
(`quota.py:_consume_vend_rate`, `VEND#<user>#<minute>` conditional
increment, `vend_rate_limit_per_minute`, default 6) → `429
lease_rate_limited`; logical lease refresh window
(`reserve_lease`, `LeaseNotRefreshable`) rejects a new lease ID before
`refresh_after`; Function URL requires a SigV4 principal from
`invoker_principal_arns`. **Status: Mitigated.** *Residual:* an attacker
holding many valid identities can multiply the per-identity budget; the
SigV4 requirement scopes who can try.

**T-04 · Information disclosure · Quota headers leak another user's state.**
*Mitigation:* headers describe only the authenticated identity's own limits
(`main.py:_quota_headers`). **Status: Mitigated.**

### B2 — Broker → STS / IAM

**T-05 · Spoofing · Session-name collision or injection.**
Two identities sanitize to the same `RoleSessionName`, or a crafted claim
breaks out of the session-name character set, so metering credits the wrong
subject or the revocation deny matches the wrong session.
*Mitigation:* `broker.py:session_name_for` derives a collision-resistant
name (sanitized prefix plus a hash of the full claim) and the same value is
set as `SourceIdentity`; the exact stamped value is persisted on the user
row (`quota.py:record_session`, `source_identity`) so the revocation
processor never re-derives it. Reverse map `SESSION#<name> → maps_to`.
`SetSourceIdentity` and `TagSession` are granted only to the broker role on
the trust policy (stack). **Status: Mitigated** (`tests/test_broker.py`).

**T-06 · Elevation · Session policy widens access.**
*Mitigation:* the session policy is `Allow bedrock:* Resource:"*"` with a
`DateLessThan aws:CurrentTime` condition; STS intersects it with the role
policy and the permissions boundary, so `"*"` cannot add a model absent from
`allowed_model_arns` (`session_policy.py` docstring; stack boundary). The
role grants exactly `bedrock:CountTokens`, `InvokeModel`,
`InvokeModelWithResponseStream`. **Status: Mitigated.**

**T-07 · Elevation · Bearer-token bypass of the model allowlist.**
`bedrock:CallWithBearerToken` requires `Resource:"*"` and would defeat the
per-model ARN allowlist.
*Mitigation:* not granted anywhere on the vended role, boundary, or session
policy; the `DenyDirectBedrockPolicy` and the SCP example explicitly deny it
for other principals (stack; DEPLOYMENT.md § Prevent bypass).
**Status: Mitigated.**

**T-08 · Elevation · `bedrock-mantle` endpoint bypass.**
Bedrock Mantle (`bedrock-mantle:CreateInference`, `bedrock-mantle:
CallWithBearerToken`) is a separate IAM service prefix and its calls are
**not captured by model-invocation logging** (Bedrock docs, "Monitor model
invocation using CloudWatch Logs and Amazon S3"). If reachable, spend is
invisible to metering.
*Finding (verified against the stack and `session_policy.py`):* the vended
role's identity policy, the permissions boundary, and the session policy
grant only `bedrock:*` actions. A vended session therefore has **no**
`bedrock-mantle:*` permission and is denied by omission — the boundary
guarantees that even a future policy edit on the role cannot add it without
also editing the boundary. **Status: Mitigated for vended sessions.**
*Residual:* any *other* principal in the account with `bedrock-mantle:*`
spends unmetered; the `DenyDirectBedrockPolicy` and SCP example cover only
`bedrock:*` and should be extended with `bedrock-mantle:CreateInference` and
`bedrock-mantle:CallWithBearerToken` where Mantle is not wanted.
**Status (other principals): Accepted, documented** — see README
"Metering coverage caveat" and B7.

### B3 — Vended session → Bedrock

**T-09 · Repudiation / Tampering · `requestMetadata` spoofing.**
The caller controls `requestMetadata` in the invocation log.
*Mitigation:* never read for attribution; the processor uses only
`identity.arn` (Bedrock-populated) and `modelId`
(`usage_processor/handler.py:_parse_invocation`). README "Identity" states
it is not trusted. **Status: Mitigated.**

**T-10 · Tampering · Unmetered Runtime APIs.**
`StartAsyncInvoke` and `InvokeModelWithBidirectionalStream` authorize under
`bedrock:InvokeModel*` but produce no invocation-log record.
*Mitigation:* none possible in the data plane. Documented with the
recommendation to exclude such models from `allowed_model_arns`
(README "Metering coverage caveat"; the reconciliation delta runbook lists
it as a cause). **Status: Accepted.**

**T-11 · Tampering · Image generation priced at zero when image delivery is
off.** A real Nova Canvas record with `imageDataDeliveryEnabled: false`
carries neither token counts nor a body, so `images: 0` and cost `$0` with
no `missing_dimensions` flag (nothing was present to flag).
*Mitigation:* the record is still counted as a request (`_is_image_model`
prevents it from being skipped as metadata-less) so the volume is visible;
README "Priced dimensions" caveat tells operators to enable image delivery
or exclude image models. **Status: Accepted** — flagged as the one pricing
gap the fallback alarm cannot detect.

### B4 — Invocation logs → usage processor → DynamoDB

**T-12 · Tampering · Log tampering or deletion by an account administrator.**
Someone with `logs:DeleteLogGroup` / `PutModelInvocationLoggingConfiguration`
removes or redirects the metering source.
*Mitigation:* the managed log group and logging configuration have
`RemovalPolicy.RETAIN`; the writer role's trust policy is condition-scoped
to `aws:SourceAccount` and `aws:SourceArn` (stack). Nothing prevents an
administrator from turning logging off. **Status: Accepted** — see B7. The
opt-in reconciliation (`reconciliation_enabled`, `reconciliation_processor/`)
is the detection control: a growing positive bill-minus-ledger gap is the
observable symptom.

**T-13 · Denial of service · Metering lag or stream lag leading to overspend.**
Slow log delivery or a backed-up subscription delays detection.
*Mitigation:* overspend is bounded by construction:
`metering lag + min(lease remainder, deny propagation)` (README
"Enforcement guarantee"); the lease deadline is embedded in the credential
and cannot be extended (`session_policy.py`; `quota.py:reserve_lease`
non-extending retries); `DetectionLagMilliseconds` is measured per record
and shown on the Operations tab. **Status: Mitigated (bounded).**
*Residual:* the bound is not measured under load
(`qualification/QUALIFICATION.md` is pending), and workload mode has no lease
— see T-19.

**T-14 · Tampering · Double-charging or lost increments on retry.**
CloudWatch Logs delivery is at-least-once.
*Mitigation:* one `TransactWriteItems` with a conditional `REQUEST#<id>`
marker Put plus the subject and per-model ledger Updates; a cancelled
transaction re-reads the marker before classifying as duplicate
(`handler.py:_apply_usage`). **Status: Mitigated**
(`test_duplicate_delivery_is_idempotent`, `test_duplicate_request_skips_both_subject_and_model_rows`).

**T-15 · Elevation · Unresolved session → usage silently lost.**
A `SESSION#` map row expired (TTL = `usage_retention_days`) so the record
cannot be attributed.
*Mitigation:* logged as `unresolved_sessions` warning; retention floor is 31
days. **Status: Accepted** — a session cannot outlive the vended STS
credential (≤ 3 600 s), so this only happens for records delivered days late.

### B5 — DynamoDB streams → enforcement processors → IAM

**T-16 · Tampering · IAM eventual consistency.**
A `CreatePolicyVersion` succeeds but takes seconds to propagate; sessions
keep working meanwhile.
*Mitigation:* propagation is inside the documented bound; the lease is the
fallback. **Status: Accepted (bounded).**

**T-17 · Denial of service · Revocation shard overflow.**
Blocking many identities (legitimately, or by an attacker who can trigger
blocks for many identities they control) fills a 6 144-character shard;
new identities in that shard are not IAM-cut.
*Mitigation:* the processor keeps the last-good deny set rather than
clearing it (`revocation_processor/handler.py` shard loop, `overflow`
branch), alarms (`RevocationPolicyOverflowAlarm`), and the lease still
bounds the new identities; the shard count is immutable to prevent rehash
gaps (`configuration.py`); the nightly auto-block sweep (T-31) evicts
identities whose automatic block is already stale, so capacity is consumed
by *currently* over-quota subjects rather than by everyone ever blocked.
**Status: Mitigated (fail-safe), capacity Accepted** — see the overflow
runbook.

**T-18 · Elevation · Enforcement processors escalate via IAM.**
A compromised processor uses its IAM permissions to grant itself or the
vended role wider access.
*Mitigation:* revocation and emergency processors may only version their
designated pre-attached policy ARNs (no `AttachRolePolicy`, `CreatePolicy`,
or role edits); the vended role's permissions boundary caps what any
policy version can grant (stack). The workload enforcer's
`PutRolePolicy` is scoped to exactly the configured workload role ARNs. The
emergency processor's DynamoDB access is restricted with
`dynamodb:LeadingKeys = CONFIG#EMERGENCY_STOP`. **Status: Mitigated.**

**T-19 · Denial of service · Workload enforcement failure has no lease
fallback.** A blocked workload keeps spending until `PutRolePolicy` succeeds.
*Mitigation:* alarm (`WorkloadEnforcementFailureAlarm`), 5-minute repair
schedule, manual attach procedure in the runbook. **Status: Accepted** —
inherent to workload mode (no vend path); documented as the one place the
bound does not apply.

**T-20 · Tampering · Stream consumer starvation.**
A third stream consumer would throttle the two enforcement readers.
*Mitigation:* dispatcher fan-out design; synth-level comment and test
(`test_cdk_stack.py` asserts exactly two `EventSourceMapping`s).
**Status: Mitigated.**

**T-31 · Tampering / Denial of service · Nightly sweep lifts a block that
should have held.** The auto-block sweeper (`AutoBlockSweeperFn`) writes
`active` to blocked user rows on a schedule, without a human in the loop. A
bug in its criterion, a stale read, or a compromised function would unblock
subjects that are still over quota — or unblock an admin freeze.
*Mitigation:* the criterion is the broker's own (`row_enforcement.over_budget`
in the shared layer, exercised by the workload-enforcer suite as well); only
automatic-origin rows are candidates and `status_origin: admin` is never
written by it; every lift is a `TransactWriteItems` conditional on the
observed `version`, `status`, and `status_reason`, so a concurrent admin block
or usage-processor re-block wins and the sweep counts a race instead of
overwriting; the `REVOCATION#` sentinel rides in the same transaction so the
row and the deny shards cannot disagree; the function has no `iam:*` and
cannot widen anything beyond flipping status; a lifted subject that is in
fact over quota is re-blocked at its next vend or metered request through the
existing paths (`refresh_auto_status` / `_evaluate_quota`). Tests:
`tests/test_auto_block_sweeper.py` (admin never touched, monthly budget
holds through a daily reset, race counted not retried, sentinel written).
*Residual:* a failed pass leaves stale automatic blocks — an availability
issue for those users and slow shard growth, never under-enforcement —
visible for a day through `AutoBlockSweepFailureAlarm` and the console card.
**Status: Mitigated.**

### B6 — Admin UI / API → admin mutations

**T-21 · Spoofing · Admin key or emergency key exposure.**
*Mitigation:* both keys live in Secrets Manager, are read by the Lambda at
cold start, never reach the browser (`config.js` carries public identifiers
only; the UI authorizes by admin JWT claim); the break-glass key is entered
per action and not persisted (`admin-ui`, DEPLOYMENT.md § Operations tab);
`ADMIN_JWT_CLAIM` empty disables JWT admin so a browser can never be an
admin without the group. Both compares are constant-time
(`main.py:_require_admin` and `_require_emergency_admin`,
`secrets.compare_digest`) — the emergency compare was `==` until this
review and is now fixed. **Status: Mitigated.** *Residual:* key rotation is
manual and requires container recycling (gateway runbook).

**T-22 · Elevation · Admin JWT claim escalation.**
A user adds themselves to the admin group at the IdP.
*Mitigation:* the claim/value is operator-configured
(`admin_jwt_claim`/`admin_jwt_value`); IdP group membership is outside the
sample. **Status: Accepted.**

**T-23 · Repudiation · Unaudited or replayed administrative change.**
*Mitigation:* every create/limit/status/model-budget mutation writes an
immutable audit event with actor, auth method, reason, before/after
snapshot, and a request-hash-bound idempotency marker in the same
transaction (`quota.py:_admin_metadata_items`,
`update_admin_limits`, `update_admin_model_budget`); `If-Match` versioning
rejects lost updates (`409 version_conflict`); reasons are required for
sensitive changes in the UI and stored for all. Enforcement-dial and
emergency actions write their own immutable audit rows
(`CONFIG#ENFORCEMENT_AUDIT#`, `EMERGENCY_AUDIT#`). **Status: Mitigated.**

**T-24 · Denial of service · Malicious admin mass-blocks or sets alert-only
everywhere.** An authorized admin can disable enforcement by making every
budget alert-only or Unlimited.
*Mitigation:* UI requires explicit confirmation and a reason for Unlimited,
period changes, and alert-only thresholds; all changes audited. Cannot be
prevented for a legitimate admin. **Status: Accepted (audited).**

**T-25 · Information disclosure · Admin API returns CloudWatch/IAM
internals.** *Mitigation:* Operations responses expose alarm *keys* and
states, metric values, and configuration numbers — never secret ARNs,
policy ARNs, or the emergency key (`main.py:admin_operations`;
`test_operations_is_read_only_safe_and_reports_revocation_health`).
**Status: Mitigated.**

### B7 — Operator / account administrator

**T-26 · Elevation · Direct Bedrock access outside the vended role.**
Any principal with `bedrock:InvokeModel*` (or `bedrock-mantle:*`, T-08)
spends unmetered.
*Mitigation:* `DenyDirectBedrockPolicyArn` helper policy and SCP example
(DEPLOYMENT.md § Prevent bypass); opt-in reconciliation
(`reconciliation_enabled`) detects the resulting bill/ledger gap. **Status: Accepted** — the sample cannot
constrain the management plane; production must apply the SCP/boundary.

**T-27 · Tampering · Administrator edits IAM policies, the boundary, the
logging configuration, or DynamoDB directly.**
*Mitigation:* none; `RETAIN` policies preserve evidence, CloudTrail (not
deployed by this sample) is the audit source. **Status: Accepted.**

**T-28 · Tampering · Price catalog manipulation.** An operator pins a `$0`
price to make a model free.
*Mitigation:* synth requires a positive token pair except for image models
with a positive `per_image`, requires a `reason` on every override, and
the reference catalog is reviewed in Git (`configuration.py:_price`).
**Status: Mitigated (reviewed config).**

**T-29 · Information disclosure / Tampering · Reconciliation reads the
account bill.** With `reconciliation_enabled`, a Lambda holds
`ce:GetCostAndUsage` on `*` (Cost Explorer has no resource scoping) and can
read the whole account's Bedrock spend, not just this stack's; the stored
`RECONCILE#` rows put account-level USD in the usage table, readable by
anyone with the admin API key.
*Mitigation:* opt-in and off by default; the grant is the single `ce:` verb
and the function has no users/audit-table access; the query is filtered to
Bedrock services and the stack Region, and only totals (no usage-type or
account breakdown) are stored; the broker never calls CE and serves the
rows behind the same admin authorization as the ledger itself
(`reconciliation_processor/handler.py`, stack `SpendReconciliationFn`).
**Status: Mitigated (least privilege + admin gate).** Residual: an admin
sees aggregate account Bedrock spend for the Region, which they can already
read from the ledger's own totals in the metered case.

**T-30 · Tampering / Information disclosure · Workload roster parameter.**
The broker labels `workload:` rows and answers `GET /admin/workloads` from
the `WorkloadRosterParameter` SSM parameter (name, model, inference-profile
ARN, role ARN, `enforcement_ready`). A principal with `ssm:PutParameter` on
it could relabel a role-less workload as *Enforced* (an admin then believes
a block stops traffic when it does not) or hide a workload from the console;
reading it discloses the account's workload role and profile ARNs.
*Mitigation:* the parameter is written only by CloudFormation and the
broker's grant is `ssm:GetParameter` on that single ARN; the roster is
**presentation only** — the enforcer Lambda carries its own copy in its
environment and evaluates budgets from the ledger, so a tampered parameter
cannot change what is enforced, only what the console *says* is enforced;
the values are also present in the stack template and outputs an operator
with `ssm:PutParameter` in the account can already read. The `workload:`
namespace is rejected by `POST /admin/users`, so the admin API cannot mint
an unregistered row (`gateway/app/main.py:_workload_registry`,
`create_user`; stack `WorkloadRosterParameter`).
**Status: Mitigated (single writer + read-only grant + no enforcement
dependency).** Residual: a console label can lie to an admin who has
already been compromised at the account level.

## Summary

| Status | Count | IDs |
|---|---|---|
| Mitigated | 19 | T-03, T-04, T-05, T-06, T-07, T-08 (vended sessions), T-09, T-13*, T-14, T-17*, T-18, T-20, T-21, T-23, T-25, T-28, T-29, T-30, T-31 |
| Accepted | 12 | T-01, T-02, T-10, T-11, T-12, T-15, T-16*, T-19, T-22, T-24, T-26, T-27 |
| Open | 0 | — |

\* bounded rather than eliminated.

The two structural accepts to keep in front of any adopter: **the sample
cannot constrain the account's management plane** (T-12, T-26, T-27 — apply
the SCP), and **workload mode has no permission lease** (T-19 — an IAM
failure there is the only unbounded-overspend path, and it is alarmed).
