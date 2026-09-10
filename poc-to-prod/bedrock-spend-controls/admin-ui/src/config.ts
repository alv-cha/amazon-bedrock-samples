export interface AdminConfig {
  gatewayUrl: string;
  region: string;
  /** OIDC issuer URL; endpoints are resolved from its discovery document. */
  issuer: string;
  /** Public (no-secret) OAuth client id registered for this SPA. */
  clientId: string;
  identityPoolId: string;
  /** Space-separated OAuth scopes requested at sign-in. */
  scopes: string;
}

/**
 * Keys accepted from deployments generated before the UI became
 * IdP-agnostic. `userPoolId` and `cognitoDomain` are obsolete: endpoints
 * now come from OIDC discovery on the issuer.
 */
interface LegacyConfigKeys {
  userPoolId: string;
  userPoolClientId: string;
  cognitoDomain: string;
  cognitoIssuer: string;
}

declare global {
  interface Window {
    QUOTA_ADMIN_CONFIG?: Partial<AdminConfig & LegacyConfigKeys>;
  }
}

function issuerUrl(value: string): string {
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw new Error("config.js issuer must be a valid HTTPS URL.");
  }
  if (
    url.protocol !== "https:" ||
    url.username ||
    url.password ||
    url.search ||
    url.hash
  ) {
    throw new Error(
      "config.js issuer must be an HTTPS URL without credentials, query, or fragment.",
    );
  }
  return url.toString().replace(/\/+$/, "");
}

export function loadConfig(): AdminConfig {
  const c = window.QUOTA_ADMIN_CONFIG ?? {};
  const issuer = c.issuer ?? c.cognitoIssuer;
  const clientId = c.clientId ?? c.userPoolClientId;
  const missing: string[] = [];
  if (!c.gatewayUrl) missing.push("gatewayUrl");
  if (!issuer) missing.push("issuer");
  if (!clientId) missing.push("clientId");
  if (!c.identityPoolId) missing.push("identityPoolId");
  if (missing.length) {
    throw new Error(
      `config.js is not filled in (missing: ${missing.join(", ")}). ` +
        "Use the deployment-generated public configuration.",
    );
  }
  return {
    gatewayUrl: c.gatewayUrl!.replace(/\/$/, ""),
    region: c.region ?? "us-east-1",
    issuer: issuerUrl(issuer!),
    clientId: clientId!,
    identityPoolId: c.identityPoolId!,
    scopes: c.scopes || "openid email profile",
  };
}
