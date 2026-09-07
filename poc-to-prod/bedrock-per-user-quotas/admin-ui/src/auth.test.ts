import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  beginSignIn,
  createSessionFromIdToken,
  handleAuthCallback,
  type AuthDependencyOverrides,
  type Signer,
} from "./auth";
import type { AdminConfig } from "./config";

const cfg: AdminConfig = {
  gatewayUrl: "https://gateway.example.test",
  region: "us-east-1",
  userPoolId: "us-east-1_pool",
  userPoolClientId: "client-id",
  identityPoolId: "us-east-1:identity",
  cognitoDomain: "https://login.example.test",
  cognitoIssuer: "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_pool",
};

class MemoryStorage {
  private readonly values = new Map<string, string>();

  getItem(key: string): string | null {
    return this.values.get(key) ?? null;
  }

  setItem(key: string, value: string): void {
    this.values.set(key, value);
  }

  removeItem(key: string): void {
    this.values.delete(key);
  }

  get size(): number {
    return this.values.size;
  }
}

function testCrypto(): Crypto {
  let call = 0;
  return {
    getRandomValues: <T extends ArrayBufferView | null>(array: T): T => {
      call += 1;
      if (array) {
        const bytes = new Uint8Array(array.buffer, array.byteOffset, array.byteLength);
        bytes.fill(call);
      }
      return array;
    },
    subtle: {
      digest: vi.fn().mockResolvedValue(new Uint8Array(32).fill(9).buffer),
    },
  } as unknown as Crypto;
}

function encoded(value: unknown): string {
  return btoa(JSON.stringify(value))
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
}

function idToken(overrides: Record<string, unknown> = {}, now = Date.now()): string {
  return `${encoded({ alg: "none" })}.${encoded({
    iss: "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_pool",
    aud: "client-id",
    exp: Math.floor(now / 1000) + 3600,
    nonce: "unused",
    email: "admin@example.test",
    token_use: "id",
    ...overrides,
  })}.signature`;
}

function harness(now = Date.now()) {
  const storage = new MemoryStorage();
  const navigate = vi.fn();
  const replaceUrl = vi.fn();
  const fetchMock = vi.fn();
  const createCredentialsProvider = vi.fn().mockReturnValue(
    vi.fn().mockResolvedValue({
      accessKeyId: "access",
      secretAccessKey: "secret",
      sessionToken: "session",
      expiration: new Date(now + 5 * 60 * 1000),
    }),
  );
  const createSigner = vi.fn().mockReturnValue({ fetch: vi.fn() } satisfies Signer);
  const deps: AuthDependencyOverrides = {
    crypto: testCrypto(),
    fetch: fetchMock as typeof fetch,
    storage,
    now: () => now,
    navigate,
    replaceUrl,
    createCredentialsProvider,
    createSigner,
    origin: "https://admin.example.test",
  };
  return {
    storage,
    navigate,
    replaceUrl,
    fetchMock,
    createCredentialsProvider,
    createSigner,
    deps,
  };
}

async function pendingCallback(
  testHarness: ReturnType<typeof harness>,
  code: string,
): Promise<{ href: string; nonce: string; state: string }> {
  await beginSignIn(cfg, testHarness.deps);
  const lastNavigation = testHarness.navigate.mock.calls[
    testHarness.navigate.mock.calls.length - 1
  ];
  const authorizeUrl = new URL(String(lastNavigation?.[0]));
  const nonce = authorizeUrl.searchParams.get("nonce")!;
  const state = authorizeUrl.searchParams.get("state")!;
  return {
    nonce,
    state,
    href: `https://admin.example.test/auth/callback?code=${code}&state=${state}`,
  };
}

beforeEach(() => {
  vi.restoreAllMocks();
});

describe("managed-login PKCE", () => {
  it("builds an authorization-code request with PKCE S256 and transient state", async () => {
    const testHarness = harness();

    await beginSignIn(cfg, testHarness.deps);

    expect(testHarness.navigate).toHaveBeenCalledOnce();
    const url = new URL(String(testHarness.navigate.mock.calls[0][0]));
    expect(`${url.origin}${url.pathname}`).toBe("https://login.example.test/oauth2/authorize");
    expect(url.searchParams.get("response_type")).toBe("code");
    expect(url.searchParams.get("scope")).toBe("openid email profile");
    expect(url.searchParams.get("redirect_uri")).toBe("https://admin.example.test/auth/callback");
    expect(url.searchParams.get("code_challenge_method")).toBe("S256");
    expect(url.searchParams.get("code_challenge")).toMatch(/^[A-Za-z0-9_-]{43}$/);
    expect(url.searchParams.get("state")).toMatch(/^[A-Za-z0-9_-]{43}$/);
    expect(url.searchParams.get("nonce")).toMatch(/^[A-Za-z0-9_-]{43}$/);
    expect(testHarness.storage.size).toBe(1);
  });

  it("exchanges a callback once, validates nonce, and removes code/state from the URL", async () => {
    const testHarness = harness();
    const callback = await pendingCallback(testHarness, "single-use-code");
    testHarness.fetchMock.mockResolvedValue(new Response(JSON.stringify({
      id_token: idToken({ nonce: callback.nonce }),
      refresh_token: "refresh-token",
    }), { status: 200, headers: { "Content-Type": "application/json" } }));

    const [first, second] = await Promise.all([
      handleAuthCallback(cfg, testHarness.deps, callback.href),
      handleAuthCallback(cfg, testHarness.deps, callback.href),
    ]);

    expect(first).toBe(second);
    expect(first?.email).toBe("admin@example.test");
    expect(testHarness.fetchMock).toHaveBeenCalledOnce();
    expect(testHarness.replaceUrl).toHaveBeenCalledWith("https://admin.example.test/");
    expect(testHarness.storage.size).toBe(0);
    const request = testHarness.fetchMock.mock.calls[0][1] as RequestInit;
    expect(String(request.body)).toContain("grant_type=authorization_code");
    expect(String(request.body)).toContain("code_verifier=");
  });

  it("rejects a state mismatch before exchanging the code", async () => {
    const testHarness = harness();
    await pendingCallback(testHarness, "state-code");

    await expect(handleAuthCallback(
      cfg,
      testHarness.deps,
      "https://admin.example.test/auth/callback?code=state-code&state=wrong",
    )).rejects.toThrow("OAuth state");

    expect(testHarness.fetchMock).not.toHaveBeenCalled();
    expect(testHarness.storage.size).toBe(0);
  });

  it("surfaces a sanitized provider error only after matching its saved state", async () => {
    const testHarness = harness();
    const callback = await pendingCallback(testHarness, "provider-error");
    const href = new URL("https://admin.example.test/auth/callback");
    href.search = new URLSearchParams({
      error: "access_denied",
      error_description: "  Cognito\ndenied\u0000access\u007f  ",
      state: callback.state,
    }).toString();

    await expect(handleAuthCallback(cfg, testHarness.deps, href.toString()))
      .rejects.toThrow("Cognito denied access");

    expect(testHarness.replaceUrl).toHaveBeenCalledWith("https://admin.example.test/");
    expect(testHarness.storage.size).toBe(0);
    expect(testHarness.fetchMock).not.toHaveBeenCalled();
  });

  it("scrubs but does not consume pending PKCE state when an error omits state", async () => {
    const testHarness = harness();
    await pendingCallback(testHarness, "missing-error-state");

    await expect(handleAuthCallback(
      cfg,
      testHarness.deps,
      "https://admin.example.test/auth/callback?error=access_denied&error_description=Untrusted+provider+text",
    )).rejects.toThrow("The OAuth callback did not include state.");

    expect(testHarness.replaceUrl).toHaveBeenCalledWith("https://admin.example.test/");
    expect(testHarness.storage.size).toBe(1);
    expect(testHarness.fetchMock).not.toHaveBeenCalled();
  });

  it("scrubs but does not consume pending PKCE state when error state mismatches", async () => {
    const testHarness = harness();
    await pendingCallback(testHarness, "mismatched-error-state");

    await expect(handleAuthCallback(
      cfg,
      testHarness.deps,
      "https://admin.example.test/auth/callback?error=access_denied&error_description=Untrusted+provider+text&state=wrong",
    )).rejects.toThrow("The OAuth state does not match the sign-in request.");

    expect(testHarness.replaceUrl).toHaveBeenCalledWith("https://admin.example.test/");
    expect(testHarness.storage.size).toBe(1);
    expect(testHarness.fetchMock).not.toHaveBeenCalled();
  });

  it("preserves pending PKCE state when its saved callback URI binding is invalid", async () => {
    const testHarness = harness();
    const callback = await pendingCallback(testHarness, "callback-uri-error");
    const key = "bedrockQuotaAdmin.pkce";
    const pending = JSON.parse(testHarness.storage.getItem(key)!);
    testHarness.storage.setItem(key, JSON.stringify({
      ...pending,
      redirectUri: "https://another.example.test/auth/callback",
    }));

    await expect(handleAuthCallback(
      cfg,
      testHarness.deps,
      `https://admin.example.test/auth/callback?error=access_denied&error_description=Untrusted+provider+text&state=${encodeURIComponent(callback.state)}`,
    )).rejects.toThrow("The OAuth callback URI does not match the sign-in request.");

    expect(testHarness.replaceUrl).toHaveBeenCalledWith("https://admin.example.test/");
    expect(testHarness.storage.size).toBe(1);
    expect(testHarness.fetchMock).not.toHaveBeenCalled();
  });

  it.each([
    "https://admin.example.test/not-the-callback",
    "https://other.example.test/auth/callback",
  ])("preserves pending PKCE state when the actual error callback URI is %s", async (callbackBase) => {
    const testHarness = harness();
    const callback = await pendingCallback(testHarness, "actual-callback-uri-error");
    const href = new URL(callbackBase);
    href.search = new URLSearchParams({
      error: "access_denied",
      error_description: "Untrusted provider text",
      state: callback.state,
    }).toString();

    await expect(handleAuthCallback(cfg, testHarness.deps, href.toString()))
      .rejects.toThrow("The OAuth callback URI does not match the sign-in request.");

    expect(testHarness.replaceUrl).toHaveBeenCalledWith("https://admin.example.test/");
    expect(testHarness.storage.size).toBe(1);
    expect(testHarness.fetchMock).not.toHaveBeenCalled();
  });

  it.each([
    ["nonce", { nonce: "wrong" }, "nonce"],
    ["issuer", { iss: "https://issuer.example.test" }, "issuer"],
    ["audience", { aud: "another-client" }, "audience"],
    ["expiry", { exp: 1 }, "expired"],
  ])("rejects an ID token with invalid %s", async (suffix, overrides, message) => {
    const testHarness = harness();
    const callback = await pendingCallback(testHarness, `invalid-${suffix}`);
    testHarness.fetchMock.mockResolvedValue(new Response(JSON.stringify({
      id_token: idToken({ nonce: callback.nonce, ...overrides }),
    }), { status: 200, headers: { "Content-Type": "application/json" } }));

    await expect(handleAuthCallback(cfg, testHarness.deps, callback.href))
      .rejects.toThrow(message);
  });
});

describe("session renewal and logout", () => {
  it("refreshes the ID token and creates Identity Pool signing material from the new token", async () => {
    const now = 2_000_000;
    const testHarness = harness(now);
    const initial = idToken({ exp: Math.floor(now / 1000) + 30 }, now);
    const renewed = idToken({ exp: Math.floor(now / 1000) + 3600 }, now);
    testHarness.fetchMock.mockResolvedValue(new Response(JSON.stringify({
      id_token: renewed,
    }), { status: 200, headers: { "Content-Type": "application/json" } }));
    const session = createSessionFromIdToken(cfg, initial, "refresh-token", testHarness.deps);

    const authorization = await session.authorization();

    expect(authorization.idToken).toBe(renewed);
    expect(testHarness.fetchMock).toHaveBeenCalledOnce();
    expect(String((testHarness.fetchMock.mock.calls[0][1] as RequestInit).body))
      .toContain("grant_type=refresh_token");
    expect(testHarness.createCredentialsProvider).toHaveBeenCalledWith(cfg, renewed);
    expect(testHarness.createSigner).toHaveBeenCalledOnce();
  });

  it("renews the signer when Identity Pool credentials approach expiration", async () => {
    const now = 3_000_000;
    const testHarness = harness(now);
    const provider = vi.fn()
      .mockResolvedValueOnce({
        accessKeyId: "first",
        secretAccessKey: "secret",
        expiration: new Date(now + 30_000),
      })
      .mockResolvedValueOnce({
        accessKeyId: "second",
        secretAccessKey: "secret",
        expiration: new Date(now + 300_000),
      });
    testHarness.createCredentialsProvider.mockReturnValue(provider);
    testHarness.createSigner
      .mockReturnValueOnce({ fetch: vi.fn() })
      .mockReturnValueOnce({ fetch: vi.fn() });
    const session = createSessionFromIdToken(cfg, idToken({}, now), "refresh", testHarness.deps);

    const first = await session.authorization();
    const second = await session.authorization();

    expect(first.signer).not.toBe(second.signer);
    expect(testHarness.createCredentialsProvider).toHaveBeenCalledOnce();
    expect(provider).toHaveBeenCalledTimes(2);
    expect(testHarness.createSigner).toHaveBeenCalledTimes(2);
  });

  it("reauthenticates for Identity Pool authorization rejection but not transient failure", async () => {
    const now = 3_500_000;
    const deniedHarness = harness(now);
    const onReauthenticate = vi.fn();
    deniedHarness.deps.onReauthenticate = onReauthenticate;
    deniedHarness.createCredentialsProvider.mockReturnValue(
      vi.fn().mockRejectedValue(Object.assign(new Error("Token rejected"), {
        name: "NotAuthorizedException",
      })),
    );
    const deniedSession = createSessionFromIdToken(
      cfg,
      idToken({}, now),
      "refresh",
      deniedHarness.deps,
    );

    await expect(deniedSession.authorization()).rejects.toThrow("Token rejected");
    expect(onReauthenticate).toHaveBeenCalledOnce();
    expect(deniedHarness.navigate).toHaveBeenCalledOnce();
    expect(deniedHarness.createSigner).not.toHaveBeenCalled();

    const transientHarness = harness(now);
    const transientReauthentication = vi.fn();
    transientHarness.deps.onReauthenticate = transientReauthentication;
    transientHarness.createCredentialsProvider.mockReturnValue(
      vi.fn().mockRejectedValue(Object.assign(new Error("Timed out"), {
        name: "TimeoutError",
      })),
    );
    const transientSession = createSessionFromIdToken(
      cfg,
      idToken({}, now),
      "refresh",
      transientHarness.deps,
    );

    await expect(transientSession.authorization()).rejects.toThrow("Timed out");
    expect(transientReauthentication).not.toHaveBeenCalled();
    expect(transientHarness.navigate).not.toHaveBeenCalled();
  });

  it("clears local state and redirects through Cognito logout", () => {
    const now = 4_000_000;
    const testHarness = harness(now);
    const session = createSessionFromIdToken(cfg, idToken({}, now), "refresh", testHarness.deps);

    session.logout();

    const url = new URL(String(testHarness.navigate.mock.calls[0][0]));
    expect(`${url.origin}${url.pathname}`).toBe("https://login.example.test/logout");
    expect(url.searchParams.get("client_id")).toBe("client-id");
    expect(url.searchParams.get("logout_uri")).toBe("https://admin.example.test/");
    expect(testHarness.storage.size).toBe(0);
  });
});
