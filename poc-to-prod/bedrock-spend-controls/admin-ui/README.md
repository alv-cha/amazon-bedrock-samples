# Bedrock Spend Controls admin UI

Static React console for the broker's administrative API.

It provides:

- A 25-row server-paginated user list with server-side status/search filters.
- A safe create wizard. `POST /admin/users` is conditional and never overwrites
  an existing user; duplicate identities return `409 user_already_exists`.
- Simultaneous daily, weekly, and monthly UTC-calendar limits for USD, input
  tokens, and output tokens. `null` disables a period and `0` means
  **Unlimited** for one enabled dimension. The create/edit matrix explains
  reset times and that newly enabled periods include usage since their current
  boundary. Period enable/disable, Unlimited changes, and finite limits below
  current-period usage require an audit reason.
- Reasoned block/unblock controls.
- A user table period selector plus highest-utilization signal, and a detail
  drawer with all three current windows, period-selectable Usage history, and
  period-qualified Changes (per-user routine audit).
- A global Audit log for routine create, limit, and status changes, with an
  explicit refresh and its own last-successful freshness/error state.
- Independent stale/error states, so one failed panel retains its last
  successful timestamp without presenting unrelated data as fresh.
- Runtime-only bounded-overspend guarantee and layered enforcement status.
- Actual STS lifetime, effective permission cutoff, refresh overlap/jitter, and
  invocation-to-detection lag metric.
- An audited runtime permission-lease dial whose allowed values are returned by
  the gateway, require a reason in the UI, and apply to new credentials without
  a redeploy.
- Break-glass emergency activation/recovery with a separate key, exact
  confirmation phrase, and reason; the key is held only for that request.
- Emergency convergence, revocation capacity/freshness, qualification gates,
  and CloudWatch alarm/DLQ states.

The Operations tab never mutates IAM directly. The broker owns CloudWatch and
IAM permissions; the browser receives no CloudWatch permissions, secret ARN,
or policy ARN. The runtime dial calls the audited admin endpoint. Emergency
actions require the operator to supply the independent break-glass key, which
is never part of generated `config.js`, API responses, or persistent browser
storage. Incomplete metric queries and unresolved alarms cannot produce a
green revocation status. Failed refreshes visibly mark cached data with its
last successful timestamp, and missing telemetry is displayed as unknown or
unavailable rather than healthy.

The browser never receives the shared admin secret. With the demo Cognito
deployment it:

1. Redirects to Cognito managed login with an authorization-code + PKCE S256
   request. Only the short-lived verifier, state, and nonce are kept in
   `sessionStorage`; tokens remain in memory.
2. Validates callback state plus the ID token nonce, issuer, audience, and
   expiry before using it, then removes the code from the browser URL.
3. Exchanges the current ID token through the Cognito Identity Pool and renews
   both the token and temporary AWS credentials as they approach expiry. The
   deployed CSP must allow the regional Lambda Function URL destination
   (`https://*.lambda-url.<region>.on.aws`) and Cognito Identity endpoint
   (`https://cognito-identity.<region>.<AWS URL suffix>`) in `connect-src`;
   otherwise either credential bootstrap or the signed API request is blocked.
4. SigV4-signs the broker's `AWS_IAM` Function URL and sends the ID token in
   `X-Quota-User-Token` for `/admin` authorization.
5. Redirects through Cognito `/logout` and clears in-memory authentication
   state when signing out.

The User Pool client remains secretless and continues to allow
`USER_SRP_AUTH` and `USER_PASSWORD_AUTH` for the notebook/CLI flows. The same
client ID remains the gateway JWT audience.

First-party exact-user requests use the canonical query-route family:
`/admin/user`, `/admin/user/limits`, `/admin/user/status`,
`/admin/user/usage`, `/admin/user/usage-history`, and `/admin/user/audit`, with
the raw identity supplied as `user_id`. The request builder encodes it once
before SigV4 signing. `/admin/users/{id}` and its suffixes remain
legacy-compatible, but are ambiguous for identities containing path-like
values such as `/audit` or `/usage`; `POST|GET /admin/users` remains the
canonical collection route.

## Routine write safety and audit

Every browser routine mutation sends a UUID `Idempotency-Key`. Limit and status
changes also send `If-Match` with the ETag/version from the user record the
operator reviewed. Limit changes send a trimmed reason when provided; the UI
requires a non-empty reason for positive-to-Unlimited or finite-below-usage
changes and leaves it optional otherwise. Successful writes return the
complete canonical user and a new ETag; the UI validates that response rather
than merging assumed local state. Duplicate create, stale-version, and
idempotency-reuse conflicts remain visible and require refresh/review instead
of being treated as success.

Routine create, limit, and status changes are written transactionally to the
dedicated admin-audit table and can be read globally or per user. A limit
event contains the trimmed operator reason when supplied; compatible clients
that omit it receive the standardized legacy fallback. Its current CDK
retention is 365 days. Audit history begins when this version is deployed;
there is no backfill for earlier changes. Usage history is separately bounded
by `usage_retention_days`.

Temporary per-user overrides, bulk operations, user delete, and usage reset are
outside this MVP.

## Build and test

```bash
npm ci
npm test
npm run build
```

`npx cdk deploy -c deployment_config=config/demo.json` uploads `dist/` and writes
`config.js` with the generated public resource identifiers, including the
Cognito managed-login origin. Callback (`/auth/callback`) and logout (`/`) URLs
are derived from the deployed CloudFront origin. No secret is written to the
site.

The app client does not register a localhost callback by default. Use the
deployed CloudFront URL for authentication; enabling local callbacks requires
an explicit, separately reviewed deployment configuration change.

For a customer IdP, replace the demo Cognito login integration and provide an
Identity Pool or equivalent temporary AWS credential flow. See
[`../DEPLOYMENT.md`](../DEPLOYMENT.md).
