import { describe, expect, it, vi } from "vitest";
import type { Session } from "./auth";
import type { AdminConfig } from "./config";
import {
  ApiError,
  api,
  transport,
  type UserRow,
} from "./api";

const cfg: AdminConfig = {
  gatewayUrl: "https://gateway.example.test",
  region: "us-east-1",
  userPoolId: "us-east-1_pool",
  userPoolClientId: "client",
  identityPoolId: "us-east-1:identity",
  cognitoDomain: "https://login.example.test",
  cognitoIssuer: "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_pool",
};

function sessionWith(fetch: ReturnType<typeof vi.fn>): Session {
  return {
    email: "admin@example.test",
    authorization: vi.fn().mockResolvedValue({
      idToken: "jwt-token",
      signer: { fetch },
    }),
    reauthenticate: vi.fn().mockResolvedValue(undefined),
    logout: vi.fn(),
  };
}

function response(body: BodyInit | null, status = 200, headers: Record<string, string> = {}): Response {
  return new Response(body, { status, headers });
}

const user: UserRow = {
  user_id: "tenant/alice",
  name: "Alice",
  status: "active",
  status_reason: "created",
  status_origin: "admin",
  version: 7,
  created_at: "2026-09-01T10:00:00Z",
  updated_at: "2026-09-01T10:00:00Z",
  limits: { daily_usd: 10, daily_input_tokens: 100, daily_output_tokens: 50 },
  today: { cost_usd: 2, input_tokens: 20, output_tokens: 10, requests: 3 },
};

describe("transport", () => {
  it("returns empty responses and response metadata without parsing failures", async () => {
    const fetch = vi.fn().mockResolvedValue(response(null, 204, {
      ETag: '"8"',
      "X-Request-Id": "request-8",
    }));

    const result = await transport<undefined>(cfg, sessionWith(fetch), "DELETE", "/empty");

    expect(result).toEqual({ data: undefined, etag: '"8"', requestId: "request-8", status: 204 });
  });

  it("accepts plain text and rejects declared malformed JSON safely", async () => {
    const plainFetch = vi.fn().mockResolvedValue(response("accepted", 200, { "Content-Type": "text/plain" }));
    await expect(transport<string>(cfg, sessionWith(plainFetch), "GET", "/plain"))
      .resolves.toMatchObject({ data: "accepted" });

    const malformedFetch = vi.fn().mockResolvedValue(response("{broken", 200, { "Content-Type": "application/json" }));
    await expect(transport(cfg, sessionWith(malformedFetch), "GET", "/broken"))
      .rejects.toMatchObject({ status: 200, code: "invalid_response" });
  });

  it("preserves structured error status, code, details, and request ID", async () => {
    const fetch = vi.fn().mockResolvedValue(response(JSON.stringify({
      error: {
        type: "version_conflict",
        message: "Changed",
        details: { current_user: { ...user, version: 8 } },
      },
    }), 409, {
      "Content-Type": "application/json",
      "X-Request-Id": "conflict-request",
    }));

    const caught = await transport(cfg, sessionWith(fetch), "PUT", "/conflict").catch((error) => error);

    expect(caught).toBeInstanceOf(ApiError);
    expect(caught).toMatchObject({
      message: "Changed",
      status: 409,
      code: "version_conflict",
      requestId: "conflict-request",
      details: { current_user: { version: 8 } },
    });
  });

  it("uses useful fallbacks for plain-text HTTP and network failures", async () => {
    const deniedFetch = vi.fn().mockResolvedValue(response("Access denied", 403, { "Content-Type": "text/plain" }));
    await expect(transport(cfg, sessionWith(deniedFetch), "GET", "/denied"))
      .rejects.toMatchObject({ status: 403, code: "forbidden", message: "Access denied" });

    const networkFetch = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));
    await expect(transport(cfg, sessionWith(networkFetch), "GET", "/offline"))
      .rejects.toMatchObject({ status: 0, code: "network_error", message: "Failed to fetch" });
  });
  it("triggers managed reauthentication on a 401 response", async () => {
    const fetch = vi.fn().mockResolvedValue(response(JSON.stringify({
      error: { type: "unauthorized", message: "Expired" },
    }), 401, { "Content-Type": "application/json" }));
    const session = sessionWith(fetch);

    await expect(transport(cfg, session, "GET", "/expired"))
      .rejects.toMatchObject({ status: 401, code: "unauthorized" });

    expect(session.reauthenticate).toHaveBeenCalledOnce();
    expect(session.authorization).toHaveBeenCalledOnce();
  });
});

describe("endpoint response validation", () => {
  it("turns empty or structurally invalid successful JSON into typed API errors", async () => {
    const emptyFetch = vi.fn().mockResolvedValue(response(null, 200));
    await expect(api.summary(cfg, sessionWith(emptyFetch)))
      .rejects.toMatchObject({ status: 200, code: "invalid_response" });

    const invalidMutationFetch = vi.fn().mockResolvedValue(response(JSON.stringify({
      user_id: user.user_id,
      status: "blocked",
      reason: "policy request",
    }), 200, { "Content-Type": "application/json", "X-Request-Id": "invalid-body" }));
    await expect(api.setStatus(cfg, sessionWith(invalidMutationFetch), user, "blocked", "policy request"))
      .rejects.toMatchObject({ status: 200, code: "invalid_response", requestId: "invalid-body" });

    const wrongIdentity = { ...user, user_id: "tenant/bob", status: "blocked" as const, status_reason: "policy request", version: 8 };
    const mismatchedFetch = vi.fn().mockResolvedValue(response(JSON.stringify({
      user_id: "tenant/bob",
      status: "blocked",
      reason: "policy request",
      user: wrongIdentity,
    }), 200, { "Content-Type": "application/json" }));
    await expect(api.setStatus(cfg, sessionWith(mismatchedFetch), user, "blocked", "policy request"))
      .rejects.toMatchObject({ status: 200, code: "invalid_response" });
  });
});

describe("mutation requests", () => {
  it("sends a generated idempotency key, If-Match version, and trimmed status reason", async () => {
    const randomUuid = vi.spyOn(globalThis.crypto, "randomUUID").mockReturnValue(
      "00000000-0000-4000-8000-000000000001",
    );
    const canonical = { ...user, version: 8, status: "blocked" as const, status_reason: "policy request" };
    const fetch = vi.fn().mockResolvedValue(response(JSON.stringify({
      user_id: user.user_id,
      status: "blocked",
      reason: "policy request",
      user: canonical,
    }), 200, { "Content-Type": "application/json" }));

    await api.setStatus(cfg, sessionWith(fetch), user, "blocked", "  policy request  ");

    expect(randomUuid).toHaveBeenCalledOnce();
    const [url, init] = fetch.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("https://gateway.example.test/admin/user/status?user_id=tenant%2Falice");
    expect(init.headers).toMatchObject({
      "Idempotency-Key": "00000000-0000-4000-8000-000000000001",
      "If-Match": '"7"',
      "X-Quota-User-Token": "jwt-token",
    });
    expect(JSON.parse(String(init.body))).toEqual({ status: "blocked", reason: "policy request" });
  });

  it("uses canonical query routes for slash and route-suffix user IDs", async () => {
    vi.spyOn(globalThis.crypto, "randomUUID").mockReturnValue(
      "00000000-0000-4000-8000-000000000099",
    );
    const routeUser = { ...user, user_id: "tenant/alice/usage-history/audit/limits/status" };
    const { today: _today, ...canonical } = routeUser;
    const updatedLimits = {
      daily_usd: 12,
      daily_input_tokens: 120,
      daily_output_tokens: 60,
    };
    const fetch = vi.fn()
      .mockResolvedValueOnce(response(JSON.stringify({ user: canonical }), 200, { "Content-Type": "application/json", ETag: '"7"' }))
      .mockResolvedValueOnce(response(JSON.stringify({
        user_id: routeUser.user_id,
        start: "2026-09-01",
        end: "2026-09-02",
        usage: [],
        next_cursor: null,
      }), 200, { "Content-Type": "application/json" }))
      .mockResolvedValueOnce(response(JSON.stringify({
        user_id: routeUser.user_id,
        events: [],
        next_cursor: null,
      }), 200, { "Content-Type": "application/json" }))
      .mockResolvedValueOnce(response(JSON.stringify({
        user_id: routeUser.user_id,
        updated: true,
        limits: updatedLimits,
        user: { ...canonical, limits: updatedLimits, version: 8 },
      }), 200, { "Content-Type": "application/json", ETag: '"8"' }))
      .mockResolvedValueOnce(response(JSON.stringify({
        user_id: routeUser.user_id,
        status: "blocked",
        reason: "policy request",
        user: {
          ...canonical,
          status: "blocked",
          status_reason: "policy request",
          version: 8,
        },
      }), 200, { "Content-Type": "application/json", ETag: '"8"' }));
    const clientSession = sessionWith(fetch);

    await api.getUser(cfg, clientSession, routeUser.user_id);
    await api.usageHistory(cfg, clientSession, routeUser.user_id, {
      start: "2026-09-01",
      end: "2026-09-02",
    });
    await api.listUserAuditPage(cfg, clientSession, routeUser.user_id);
    await api.setLimits(cfg, clientSession, routeUser, updatedLimits);
    await api.setStatus(
      cfg,
      clientSession,
      routeUser,
      "blocked",
      "policy request",
    );

    const urls = fetch.mock.calls.map(([value]) => new URL(String(value)));
    expect(urls.map((url) => url.pathname)).toEqual([
      "/admin/user",
      "/admin/user/usage-history",
      "/admin/user/audit",
      "/admin/user/limits",
      "/admin/user/status",
    ]);
    for (const url of urls) {
      expect(url.searchParams.get("user_id")).toBe(routeUser.user_id);
      expect(url.pathname).not.toMatch(/^\/admin\/users\//);
    }
    expect(urls[1].searchParams.get("limit")).toBe("25");
    expect(urls[1].searchParams.get("start")).toBe("2026-09-01");
    expect(urls[1].searchParams.get("end")).toBe("2026-09-02");
    expect(urls[2].searchParams.get("limit")).toBe("25");

    for (const callIndex of [3, 4]) {
      const init = fetch.mock.calls[callIndex][1] as RequestInit;
      expect(init.headers).toMatchObject({
        "Idempotency-Key": "00000000-0000-4000-8000-000000000099",
        "If-Match": '"7"',
      });
    }
  });
});


describe("paginated operational endpoints", () => {
  it("loads exactly one 25-user page and passes opaque cursor, status, and query server-side", async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce(response(JSON.stringify({ users: [user], next_cursor: "opaque {cursor}" }), 200, { "Content-Type": "application/json" }))
      .mockResolvedValueOnce(response(JSON.stringify({ users: [], next_cursor: null }), 200, { "Content-Type": "application/json" }));
    const session = sessionWith(fetch);

    const first = await api.listUsersPage(cfg, session);
    expect(first.next_cursor).toBe("opaque {cursor}");
    expect(fetch).toHaveBeenCalledOnce();
    expect(fetch.mock.calls[0][0]).toBe("https://gateway.example.test/admin/users?limit=25");

    await api.listUsersPage(cfg, session, {
      limit: 25,
      cursor: first.next_cursor,
      status: "blocked",
      query: " Alice / team ",
    });
    const secondUrl = new URL(String(fetch.mock.calls[1][0]));
    expect(secondUrl.searchParams.get("cursor")).toBe("opaque {cursor}");
    expect(secondUrl.searchParams.get("status")).toBe("blocked");
    expect(secondUrl.searchParams.get("query")).toBe("Alice / team");
  });

  it("creates with a UUID idempotency key and rejects a mismatched canonical identity", async () => {
    vi.spyOn(globalThis.crypto, "randomUUID").mockReturnValue("00000000-0000-4000-8000-000000000002");
    const { today: _today, ...canonical } = user;
    const fetch = vi.fn().mockResolvedValue(response(JSON.stringify({
      user_id: user.user_id,
      provisioned: true,
      limits: canonical.limits,
      user: { ...canonical, version: 1 },
    }), 200, { "Content-Type": "application/json", ETag: '"1"' }));

    const result = await api.createUser(cfg, sessionWith(fetch), {
      user_id: `  ${user.user_id}  `,
      name: "  Alice  ",
      ...canonical.limits,
    });

    expect(result.etag).toBe('"1"');
    const [, init] = fetch.mock.calls[0] as [string, RequestInit];
    expect(init.headers).toMatchObject({ "Idempotency-Key": "00000000-0000-4000-8000-000000000002" });
    expect(JSON.parse(String(init.body))).toMatchObject({ user_id: user.user_id, name: "Alice" });

    const mismatched = vi.fn().mockResolvedValue(response(JSON.stringify({
      user_id: user.user_id,
      provisioned: true,
      limits: canonical.limits,
      user: { ...canonical, user_id: "tenant/bob", version: 1 },
    }), 200, { "Content-Type": "application/json" }));
    await expect(api.createUser(cfg, sessionWith(mismatched), {
      user_id: user.user_id,
      name: "Alice",
      ...canonical.limits,
    })).rejects.toMatchObject({ code: "invalid_response" });
  });

  it("validates detail and usage response identity consistency", async () => {
    const { today: _today, ...canonical } = user;
    const detailFetch = vi.fn().mockResolvedValue(response(JSON.stringify({ user: canonical }), 200, { "Content-Type": "application/json" }));
    await expect(api.getUser(cfg, sessionWith(detailFetch), "tenant/bob"))
      .rejects.toMatchObject({ code: "invalid_response" });

    const usageFetch = vi.fn().mockResolvedValue(response(JSON.stringify({
      user_id: user.user_id,
      start: "2026-08-04",
      end: "2026-09-02",
      usage: [{ user_id: "tenant/bob", window: "2026-09-02", cost_usd: 1, input_tokens: 2, output_tokens: 3, requests: 4 }],
      next_cursor: null,
    }), 200, { "Content-Type": "application/json" }));
    await expect(api.usageHistory(cfg, sessionWith(usageFetch), user.user_id, {
      start: "2026-08-04",
      end: "2026-09-02",
    })).rejects.toMatchObject({ code: "invalid_response" });
  });

  it("validates global and per-user audit snapshots without accepting cross-user events", async () => {
    const snapshot = {
      user_id: user.user_id,
      name: user.name,
      status: user.status,
      status_reason: user.status_reason,
      status_origin: user.status_origin,
      version: user.version,
      created_at: user.created_at,
      updated_at: user.updated_at,
      limits: { daily_usd_micro: 10_000_000, daily_input_tokens: 100, daily_output_tokens: 50 },
    };
    const event = {
      user_id: user.user_id,
      event_key: "2026-09-02T10:00:00Z#event",
      event_type: "user.created",
      actor: "admin",
      auth_method: "jwt",
      reason: "admin user creation",
      request_id: "request-1",
      created_at: "2026-09-02T10:00:00Z",
      before: null,
      after: snapshot,
    };
    const globalFetch = vi.fn().mockResolvedValue(response(JSON.stringify({ events: [event], next_cursor: "next" }), 200, { "Content-Type": "application/json" }));
    await expect(api.listAuditPage(cfg, sessionWith(globalFetch))).resolves.toEqual({ events: [event], next_cursor: "next" });

    const wrongUserFetch = vi.fn().mockResolvedValue(response(JSON.stringify({ user_id: "tenant/bob", events: [event], next_cursor: null }), 200, { "Content-Type": "application/json" }));
    await expect(api.listUserAuditPage(cfg, sessionWith(wrongUserFetch), user.user_id))
      .rejects.toMatchObject({ code: "invalid_response" });
  });
});


describe("USD normalization", () => {
  it("normalizes create and limit writes to the backend micro-dollar precision before validation", async () => {
    const { today: _today, ...base } = user;
    const normalizedLimits = { ...base.limits, daily_usd: 0.123457 };
    const created = { ...base, version: 1, limits: normalizedLimits };
    const createFetch = vi.fn().mockResolvedValue(response(JSON.stringify({
      user_id: base.user_id,
      provisioned: true,
      limits: normalizedLimits,
      user: created,
    }), 200, { "Content-Type": "application/json" }));

    await expect(api.createUser(cfg, sessionWith(createFetch), {
      user_id: base.user_id,
      name: base.name,
      daily_usd: 0.1234567,
      daily_input_tokens: base.limits.daily_input_tokens,
      daily_output_tokens: base.limits.daily_output_tokens,
    })).resolves.toMatchObject({ data: { user: { limits: normalizedLimits } } });
    expect(JSON.parse(String((createFetch.mock.calls[0][1] as RequestInit).body)).daily_usd).toBe(0.123457);

    const updated = { ...base, version: base.version + 1, limits: normalizedLimits };
    const limitsFetch = vi.fn().mockImplementation(() => Promise.resolve(response(JSON.stringify({
      user_id: base.user_id,
      updated: true,
      limits: normalizedLimits,
      user: updated,
    }), 200, { "Content-Type": "application/json" })));
    await expect(api.setLimits(cfg, sessionWith(limitsFetch), base, {
      ...base.limits,
      daily_usd: 0.1234567,
    })).resolves.toMatchObject({ data: { user: { limits: normalizedLimits } } });
    const bodyWithoutReason = JSON.parse(String((limitsFetch.mock.calls[0][1] as RequestInit).body));
    expect(bodyWithoutReason.daily_usd).toBe(0.123457);
    expect(bodyWithoutReason).not.toHaveProperty("reason");

    await expect(api.setLimits(cfg, sessionWith(limitsFetch), base, {
      ...base.limits,
      daily_usd: 0.1234567,
      reason: "  Annual allocation  ",
    })).resolves.toMatchObject({ data: { user: { limits: normalizedLimits } } });
    expect(JSON.parse(String((limitsFetch.mock.calls[1][1] as RequestInit).body))).toEqual({
      ...normalizedLimits,
      reason: "Annual allocation",
    });
  });
});
