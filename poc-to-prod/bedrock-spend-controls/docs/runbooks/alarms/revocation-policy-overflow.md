# RevocationPolicyOverflowAlarm

**Operations key:** `revocation_overflow` · **Metric:** `RevocationPolicyOverflow` (Sum ≥ 1 over 5 min, 1 period) · **Emitted by:** `revocation_processor/handler.py` — `overflow_count` in the success `_emit`, one per shard whose desired document exceeds `REVOCATION_POLICY_MAX_CHARACTERS` (6 144).

## What it means

At least one of the 19 deny shards has more blocked identities hashed into it
than fit in a 6 144-character managed policy. For that shard the processor
**keeps the last known-good deny document** rather than writing a truncated
or empty one (see the comment in the shard loop: replacing it with a no-op
would instantly restore every blocked identity in the shard). Consequence:

- Identities already in the shard stay denied.
- **Newly blocked identities that hash into the overflowing shard are not
  cut by IAM.** Their sessions run to the permission lease deadline; new
  vends are still refused by the broker.

This is capacity exhaustion, not an error, and it will not self-heal until
blocked identities are unblocked or the window resets and the automatic
blocks lift.

## Severity guidance

**Next business day** at the default 300 s lease with a handful of overflow
shards. **Page** when `overflow_shards` covers many shards (a mass-block
event) *and* the dial is 900 s — that is up to 15 minutes of overspend per
newly blocked identity with no IAM cut.

## Likely causes

1. **Many identities blocked at once.** Each shard holds roughly 40–60
   identities depending on `source_identity` length (a sanitized JWT `sub`;
   the sanitizer in `gateway/app/broker.py` caps it). With 19 shards that is
   ~800–1 100 simultaneously blocked identities before the first overflow.
   Read the count: `metrics.revoked_identities_desired` in
   `/admin/operations`, or the `blocked_identities` field of the last
   success log line.
2. **Very long `source_identity` values** (long tenant claims) — fewer fit
   per shard.
3. **A day-boundary reset that did not lift automatic blocks.** Blocks lift
   lazily (at the next vend or enforcer pass), so a burst of blocked
   identities that never vend again stays in the shards until the row's
   status changes. The row's `status_origin: automatic` block is harmless,
   but it occupies shard capacity.

## Remediation

1. See which shards overflowed and by how much:
   ```bash
   aws logs filter-log-events --log-group-name /aws/lambda/<RevocationProcessorFn> \
     --filter-pattern '{ $.RevocationPolicyOverflow > 0 }' \
     --start-time $(( $(date +%s) - 3600 ))000 --query 'events[-1].message' --output text \
     | python3 -c 'import json,sys; r=json.load(sys.stdin); print(r["overflow_shards"]); [print(s) for s in r["shards"] if s["overflow"]]'
   ```
   `desired_characters` vs `applied_characters` shows the gap.
2. **Reduce the blocked set.** Unblock identities that no longer need to be
   blocked (`PUT /admin/user/status`), or raise limits for identities whose
   automatic block is stale. Each unblock removes the identity from its
   shard on the next reconcile.
3. **Shorten exposure meanwhile**: `PUT /admin/enforcement
   {"permission_lease_seconds": 60, ...}`.
4. **Do not change `revocation_policy_shards`.** Synthesis rejects any value
   other than 19 because rehashing live identities across a different shard
   count creates a transient window where a blocked identity is in no shard.
   Adding capacity requires a separately reviewed two-policy-set migration
   (DEPLOYMENT.md § Lease and revocation qualification).
5. Force a reconcile after unblocking:
   `aws lambda invoke --function-name <RevocationProcessorFn> --payload '{"source":"aws.events"}' --cli-binary-format raw-in-base64-out /dev/stdout`.

## How to verify recovery

- `RevocationPolicyOverflow` Sum = 0 for one 5-minute period → `OK`.
- `metrics.recent_overflow_count: 0` and `reconciliation_status: current`
  in `/admin/operations`.
- The last success log's `shards[]` shows `overflow: false` everywhere and
  `desired_characters == applied_characters`.

## Related

- [revocation-sync-failure.md](revocation-sync-failure.md)
- Component: [components/revocation-processor.md](../components/revocation-processor.md)
- Threat model: T-13 (shard overflow as an enforcement-degradation vector) in [../threat-model.md](../../threat-model.md)
