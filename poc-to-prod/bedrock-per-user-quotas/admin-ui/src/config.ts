export interface AdminConfig {
  gatewayUrl: string;
  region: string;
  userPoolId: string;
  userPoolClientId: string;
  identityPoolId: string;
}

declare global {
  interface Window {
    QUOTA_ADMIN_CONFIG?: Partial<AdminConfig>;
  }
}

export function loadConfig(): AdminConfig {
  const c = window.QUOTA_ADMIN_CONFIG ?? {};
  const missing = (["gatewayUrl", "userPoolId", "userPoolClientId", "identityPoolId"] as const).filter(
    (k) => !c[k],
  );
  if (missing.length) {
    throw new Error(
      `config.js is not filled in (missing: ${missing.join(", ")}). ` +
        "Set the CloudFormation outputs in config.js and re-upload it.",
    );
  }
  return {
    gatewayUrl: c.gatewayUrl!.replace(/\/$/, ""),
    region: c.region ?? "us-east-1",
    userPoolId: c.userPoolId!,
    userPoolClientId: c.userPoolClientId!,
    identityPoolId: c.identityPoolId!,
  };
}
