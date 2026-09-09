export interface AdminConfig {
  gatewayUrl: string;
  region: string;
  userPoolId: string;
  userPoolClientId: string;
  identityPoolId: string;
  cognitoDomain: string;
  cognitoIssuer: string;
}

declare global {
  interface Window {
    QUOTA_ADMIN_CONFIG?: Partial<AdminConfig>;
  }
}

function httpsOrigin(value: string, field: "cognitoDomain" | "cognitoIssuer"): string {
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw new Error(`config.js ${field} must be a valid HTTPS origin.`);
  }
  if (
    url.protocol !== "https:" ||
    url.username ||
    url.password ||
    url.pathname !== "/" ||
    url.search ||
    url.hash
  ) {
    throw new Error(`config.js ${field} must be an HTTPS origin without a path, query, or fragment.`);
  }
  return url.origin;
}

function cognitoIssuer(value: string): string {
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw new Error("config.js cognitoIssuer must be a valid HTTPS URL.");
  }
  if (
    url.protocol !== "https:" ||
    url.username ||
    url.password ||
    url.pathname === "/" ||
    url.search ||
    url.hash
  ) {
    throw new Error("config.js cognitoIssuer must be an HTTPS issuer URL without credentials, query, or fragment.");
  }
  return url.toString().replace(/\/$/, "");
}

export function loadConfig(): AdminConfig {
  const c = window.QUOTA_ADMIN_CONFIG ?? {};
  const missing = ([
    "gatewayUrl",
    "userPoolId",
    "userPoolClientId",
    "identityPoolId",
    "cognitoDomain",
    "cognitoIssuer",
  ] as const).filter((key) => !c[key]);
  if (missing.length) {
    throw new Error(
      `config.js is not filled in (missing: ${missing.join(", ")}). ` +
        "Use the deployment-generated public configuration.",
    );
  }
  return {
    gatewayUrl: c.gatewayUrl!.replace(/\/$/, ""),
    region: c.region ?? "us-east-1",
    userPoolId: c.userPoolId!,
    userPoolClientId: c.userPoolClientId!,
    identityPoolId: c.identityPoolId!,
    cognitoDomain: httpsOrigin(c.cognitoDomain!, "cognitoDomain"),
    cognitoIssuer: cognitoIssuer(c.cognitoIssuer!),
  };
}
