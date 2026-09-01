# Runtime quota admin UI

Static React console for the broker's administrative API.

It shows:

- Current users and status.
- Daily USD, input-token, and output-token limits.
- Current UTC-day usage.
- Runtime-only bounded-overspend guarantee and active enforcement mode.
- Actual STS lifetime, effective permission cutoff, refresh overlap/jitter, and
  invocation-to-detection lag metric.
- Read-only emergency convergence, revocation capacity/freshness, qualification
  gates, and CloudWatch alarm/DLQ alarm states.

The Operations panel has no mutation controls. CloudWatch is queried by the
broker Lambda; the browser receives no CloudWatch IAM permissions, emergency
key, secret ARN/value, IAM policy controls, incident detail, or emergency and
revocation action buttons. Incomplete metric queries and unresolved alarms
cannot produce a green revocation status. Failed refreshes visibly mark cached
data with its last successful timestamp, emergency qualification is separate
from convergence, and permission-lease duration shows `Not active` outside
lease mode. Missing telemetry is displayed as unknown or unavailable rather
than healthy.

The browser never receives the shared admin secret. With the demo Cognito
deployment it:

1. Authenticates to the User Pool.
2. Exchanges the ID token through the Cognito Identity Pool.
3. SigV4-signs the broker's `AWS_IAM` Function URL.
4. Sends the ID token in `X-Quota-User-Token`.
5. Uses the configured admin group claim for `/admin` authorization.

## Build

```bash
npm ci
npm run build
```

`npx cdk deploy -c deployment_config=config/demo.json` uploads `dist/` and writes
`config.js` with the generated public resource identifiers. No secret is
written to the site.

For a customer IdP, replace the demo Cognito login integration and provide an
Identity Pool or equivalent temporary AWS credential flow. See
[`../DEPLOYMENT.md`](../DEPLOYMENT.md).
