import {
  CognitoUserPool,
  CognitoUser,
  AuthenticationDetails,
} from "amazon-cognito-identity-js";
import { fromCognitoIdentityPool } from "@aws-sdk/credential-provider-cognito-identity";
import { AwsClient } from "aws4fetch";
import type { AdminConfig } from "./config";

// Signed-in session: the raw ID token (used both as the Identity Pool login
// AND as the gateway's admin-authorizing JWT) plus an aws4fetch client that
// SigV4-signs requests with the Identity Pool's temporary AWS credentials.
export interface Session {
  email: string;
  idToken: string;
  signer: AwsClient;
}

function userPool(cfg: AdminConfig): CognitoUserPool {
  return new CognitoUserPool({
    UserPoolId: cfg.userPoolId,
    ClientId: cfg.userPoolClientId,
  });
}

/** Authenticate against the Cognito user pool (USER_SRP). Returns the ID token. */
function cognitoLogin(cfg: AdminConfig, email: string, password: string): Promise<string> {
  return new Promise((resolve, reject) => {
    const user = new CognitoUser({ Username: email, Pool: userPool(cfg) });
    const details = new AuthenticationDetails({ Username: email, Password: password });
    user.authenticateUser(details, {
      onSuccess: (result) => resolve(result.getIdToken().getJwtToken()),
      onFailure: (err) => reject(err),
      // New Cognito users must set a permanent password on first login.
      newPasswordRequired: () =>
        reject(new Error("This account must set a new password first (use the Cognito hosted UI or AWS CLI).")),
    });
  });
}

export async function signIn(cfg: AdminConfig, email: string, password: string): Promise<Session> {
  const idToken = await cognitoLogin(cfg, email, password);
  const loginKey = `cognito-idp.${cfg.region}.amazonaws.com/${cfg.userPoolId}`;
  // Exchange the ID token for temporary AWS credentials via the Identity Pool.
  const credentials = fromCognitoIdentityPool({
    clientConfig: { region: cfg.region },
    identityPoolId: cfg.identityPoolId,
    logins: { [loginKey]: idToken },
  });
  // aws4fetch signs Function URL requests for service "lambda".
  const resolved = await credentials();
  const signer = new AwsClient({
    accessKeyId: resolved.accessKeyId,
    secretAccessKey: resolved.secretAccessKey,
    sessionToken: resolved.sessionToken,
    service: "lambda",
    region: cfg.region,
  });
  const email_ = decodeEmail(idToken) ?? email;
  return { email: email_, idToken, signer };
}

function decodeEmail(idToken: string): string | undefined {
  try {
    const payload = JSON.parse(atob(idToken.split(".")[1].replace(/-/g, "+").replace(/_/g, "/")));
    return payload.email;
  } catch {
    return undefined;
  }
}
