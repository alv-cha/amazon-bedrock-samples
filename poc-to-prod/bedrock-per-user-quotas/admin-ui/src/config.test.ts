import { afterEach, describe, expect, it } from "vitest";
import { loadConfig } from "./config";

const valid = {
  gatewayUrl: "https://gateway.example.test/",
  region: "us-east-1",
  userPoolId: "us-east-1_pool",
  userPoolClientId: "client",
  identityPoolId: "us-east-1:identity",
  cognitoDomain: "https://login.example.test",
  cognitoIssuer: "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_pool",
};

afterEach(() => {
  delete window.QUOTA_ADMIN_CONFIG;
});

describe("runtime configuration", () => {
  it("requires and normalizes the public Cognito managed-login origin", () => {
    window.QUOTA_ADMIN_CONFIG = { ...valid, cognitoDomain: "https://login.example.test/" };

    expect(loadConfig()).toEqual({
      ...valid,
      gatewayUrl: "https://gateway.example.test",
      cognitoDomain: "https://login.example.test",
    });
  });

  it("rejects a missing or non-origin managed-login URL", () => {
    window.QUOTA_ADMIN_CONFIG = { ...valid, cognitoDomain: "" };
    expect(() => loadConfig()).toThrow("cognitoDomain");

    window.QUOTA_ADMIN_CONFIG = {
      ...valid,
      cognitoDomain: "https://login.example.test/oauth2/authorize",
    };
    expect(() => loadConfig()).toThrow("without a path");

    window.QUOTA_ADMIN_CONFIG = {
      ...valid,
      cognitoDomain: "http://login.example.test",
    };
    expect(() => loadConfig()).toThrow("HTTPS origin");
  });
});
