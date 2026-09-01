import type { AdminConfig } from "./config";
import type { Session } from "./auth";

// Every admin call is (1) SigV4-signed by aws4fetch so the AWS_IAM Function URL
// accepts it, and (2) carries the Cognito ID token in X-Quota-User-Token so the
// gateway's admin-by-JWT authorization (ADMIN_JWT_CLAIM/VALUE) grants it. No
// admin secret is ever held by the browser.
async function call(
  cfg: AdminConfig,
  session: Session,
  method: string,
  path: string,
  body?: unknown,
): Promise<any> {
  const headers: Record<string, string> = { "X-Quota-User-Token": session.idToken };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const resp = await session.signer.fetch(`${cfg.gatewayUrl}${path}`, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  const text = await resp.text();
  const data = text ? JSON.parse(text) : {};
  if (!resp.ok) {
    const message = data?.error?.message ?? `${method} ${path} failed (${resp.status})`;
    throw new Error(message);
  }
  return data;
}

export interface UserRow {
  user_id: string;
  name: string;
  status: string;
  status_reason: string;
  limits: { daily_usd: number; daily_input_tokens: number; daily_output_tokens: number };
  today: { cost_usd: number; input_tokens: number; output_tokens: number; requests: number };
}

export interface Summary {
  enforcement: {
    source: string;
    as_of: string;
    window: string;
    mode: string;
    credential_ttl_seconds: number;
    permission_lease_seconds: number;
    post_detection_fallback_seconds: number;
    refresh_overlap_seconds: number;
    refresh_jitter_seconds: number;
    vend_rate_limit_per_minute: number;
    revocation_policy_shards: number;
    revocation_reconcile_minutes: number;
    total_users: number;
    blocked_users: number;
    blocked_user_ids: string[];
    today: { cost_usd: number; input_tokens: number; output_tokens: number; requests: number };
  };
  observability: {
    source: string;
    delivery: string;
    metrics_namespace: string;
    detection_lag_metric: string;
  };
}

export interface Operations {
  as_of: string;
  configuration: {
    mode: string;
    credential_ttl_seconds: number;
    permission_lease_seconds: number;
    permission_lease_enabled: boolean;
    effective_permission_lease_seconds: number | null;
    post_detection_fallback_seconds: number;
    refresh_overlap_seconds: number;
    refresh_jitter_seconds: number;
    vend_rate_limit_per_minute: number;
    revocation_enabled: boolean;
    revocation_policy_shards: number;
    revocation_policy_max_characters: number;
    revocation_reconcile_minutes: number;
  };
  emergency: {
    state: string;
    desired_active: boolean;
    generation: number;
    applied_generation: number;
    requested_at: string | null;
    applied_at: string | null;
    converged: boolean;
  };
  qualification: {
    status: string;
    emergency_status: string;
    source: string;
  };
  metrics: {
    namespace: string;
    detection_lag_metric: string;
    detection_lag_p95_ms: number | null;
    detection_lag_timestamp: string | null;
    telemetry_status: string;
    last_reconciliation_at: string | null;
    reconciliation_status: string;
    revoked_identities_desired: number | null;
    recent_sync_failure_count: number | null;
    recent_overflow_count: number | null;
    recent_emergency_failure_count: number | null;
    window_minutes: number;
  };
  alarms: Array<{ key: string; state: string; updated_at: string | null }>;
  cloudwatch: { status: string; error_code?: string };
}

export const api = {
  summary: (cfg: AdminConfig, s: Session): Promise<Summary> =>
    call(cfg, s, "GET", "/admin/summary"),

  operations: (cfg: AdminConfig, s: Session): Promise<Operations> =>
    call(cfg, s, "GET", "/admin/operations"),

  listUsers: async (cfg: AdminConfig, s: Session): Promise<UserRow[]> => {
    // Follow the cursor to collect every page.
    const users: UserRow[] = [];
    let cursor: string | null = null;
    do {
      const qs = "?limit=100" + (cursor ? `&cursor=${encodeURIComponent(cursor)}` : "");
      const page = await call(cfg, s, "GET", `/admin/users${qs}`);
      users.push(...page.users);
      cursor = page.next_cursor;
    } while (cursor);
    return users;
  },

  setLimits: (cfg: AdminConfig, s: Session, userId: string, limits: object): Promise<any> =>
    call(cfg, s, "PUT", `/admin/users/${encodeURIComponent(userId)}/limits`, limits),

  setStatus: (cfg: AdminConfig, s: Session, userId: string, status: string): Promise<any> =>
    call(cfg, s, "PUT", `/admin/users/${encodeURIComponent(userId)}/status`, { status }),
};
