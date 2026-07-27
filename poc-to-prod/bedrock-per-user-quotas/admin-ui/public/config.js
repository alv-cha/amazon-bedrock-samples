// Runtime configuration for the admin console.
//
// Fill these in AFTER deploying the stack (from the CloudFormation outputs and
// the demo Cognito pool), then re-upload this file to the S3 bucket — the SPA
// reads window.QUOTA_ADMIN_CONFIG at load time so one build works for any
// deployment. Do NOT put any secret here; these are all public identifiers.
window.QUOTA_ADMIN_CONFIG = {
  // CfnOutput GatewayUrl (Function URL, e.g. https://xxxx.lambda-url.us-east-1.on.aws/)
  gatewayUrl: "",
  region: "us-east-1",
  // CfnOutput DemoUserPoolId / DemoUserPoolClientId
  userPoolId: "",
  userPoolClientId: "",
  // CfnOutput AdminIdentityPoolId
  identityPoolId: "",
};
