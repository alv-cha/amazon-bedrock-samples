# Admin console (React SPA)

Optional admin UI for the Bedrock per-user quota gateway. Static React on
S3 + CloudFront, authenticated with the demo Cognito user pool via a Cognito
Identity Pool. The browser gets temporary AWS credentials from the Identity
Pool and SigV4-signs its calls to the gateway's `AWS_IAM` Function URL. The
Cognito ID token rides in `X-Quota-User-Token` and the gateway's admin-by-JWT
authorization grants it — **no admin secret is ever held by the browser.**

## Enable it

Deploy the stack with the UI flag **and** an admin group claim so the login
identity is authorized for the `/admin` API:

```bash
cd cdk
PATH=/tmp/quota-venv/bin:$PATH cdk deploy \
  -c admin_ui=true \
  -c admin_jwt_claim=cognito:groups \
  -c admin_jwt_value=quota-admins
```

The UI is only wired when the stack created the demo Cognito pool (i.e. no BYO
`jwt_issuer`). For a bring-your-own issuer, create the Identity Pool + OIDC
provider manually and point `config.js` at it — see `DEPLOYMENT.md`.

## Build and publish

```bash
cd admin-ui
npm install
npm run build          # -> dist/ (Vite)
```

`cdk deploy` uploads `dist/` to the UI bucket automatically. After the first
deploy, fill in `dist/config.js` (or `public/config.js` before building) with
the CloudFormation outputs — `GatewayUrl`, `DemoUserPoolId`,
`DemoUserPoolClientId`, `AdminIdentityPoolId`, region — and re-upload
`config.js`. These are all public identifiers; put no secret there.

## Screens

- **Summary** — enforcement (authoritative DynamoDB state) and observability
  (CloudWatch namespace) shown separately, plus the deploy-time reconciler
  interval (read-only) and a Mode A vs Mode B note.
- **Users** — per-user daily budget, today's spend, status, and managed Mantle
  project, with set-budget / block-unblock / set-project actions.
