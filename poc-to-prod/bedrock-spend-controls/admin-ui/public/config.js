// Runtime configuration for the admin console.
//
// Fill these in AFTER deploying the stack (from the CloudFormation outputs and
// the demo Cognito pool), then re-upload this file to the S3 bucket — the SPA
// reads window.QUOTA_ADMIN_CONFIG at load time so one build works for any
// deployment. Do NOT put any secret here; these are all public identifiers.
window.QUOTA_ADMIN_CONFIG = {
  // CfnOutput BrokerApiUrl (AWS_IAM Function URL)
  gatewayUrl: "",
  region: "us-east-1",
  // CfnOutput DemoUserPoolId / DemoUserPoolClientId
  userPoolId: "",
  userPoolClientId: "",
  // Cognito managed-login origin, for example
  // https://bedrock-spend-admin-....auth.us-east-1.amazoncognito.com
  cognitoDomain: "",
  // Public User Pool issuer used for callback checks and Identity Pool login.
  cognitoIssuer: "",
  // CfnOutput AdminIdentityPoolId
  identityPoolId: "",
};
