import type { AdminConfig } from "./config";
import type { Session } from "./auth";

export interface TransportResponse<T> {
  data: T;
  etag: string | null;
  requestId: string | null;
  status: number;
}

export class ApiError<TDetails = unknown> extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: TDetails | undefined;
  readonly requestId: string | null;

  constructor(
    message: string,
    status: number,
    code: string,
    details?: TDetails,
    requestId: string | null = null,
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
    this.requestId = requestId;
  }
}

type JsonObject = Record<string, unknown>;

interface TransportOptions<T> {
  body?: unknown;
  headers?: Record<string, string>;
  validate?: (data: unknown) => data is T;
}

function isObject(value: unknown): value is JsonObject {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function defaultErrorCode(status: number): string {
  if (status === 401) return "unauthorized";
  if (status === 403) return "forbidden";
  if (status === 409) return "conflict";
  if (status === 429) return "rate_limited";
  if (status === 503) return "service_unavailable";
  return "http_error";
}

function requestIdFrom(response: Response, data: unknown): string | null {
  const header = response.headers.get("X-Request-Id");
  if (header) return header;
  if (isObject(data) && typeof data.request_id === "string") return data.request_id;
  return null;
}

async function readResponseBody(response: Response, method: string, path: string): Promise<unknown> {
  let text: string;
  try {
    text = await response.text();
  } catch (caught) {
    throw new ApiError(
      caught instanceof Error ? caught.message : "The response body could not be read.",
      0,
      "network_error",
    );
  }
  if (!text) return undefined;

  try {
    return JSON.parse(text) as unknown;
  } catch {
    const contentType = response.headers.get("Content-Type") ?? "";
    if (contentType.toLowerCase().includes("json")) {
      throw new ApiError(
        `${method} ${path} returned malformed JSON (${response.status}).`,
        response.status,
        response.ok ? "invalid_response" : "invalid_error_response",
        undefined,
        response.headers.get("X-Request-Id"),
      );
    }
    return text;
  }
}

// Every admin call is (1) SigV4-signed by aws4fetch so the AWS_IAM Function URL
// accepts it, and (2) carries the Cognito ID token in X-Quota-User-Token so the
// gateway's admin-by-JWT authorization grants it. No admin secret is held by the browser.
export async function transport<T>(
  cfg: AdminConfig,
  session: Session,
  method: string,
  path: string,
  options: TransportOptions<T> = {},
): Promise<TransportResponse<T>> {
  let response: Response;
  try {
    const authorization = await session.authorization();
    const headers: Record<string, string> = {
      "X-Quota-User-Token": authorization.idToken,
      ...options.headers,
    };
    if (options.body !== undefined) headers["Content-Type"] = "application/json";
    response = await authorization.signer.fetch(`${cfg.gatewayUrl}${path}`, {
      method,
      headers,
      body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
    });
  } catch (caught) {
    throw new ApiError(
      caught instanceof Error ? caught.message : "The quota gateway could not be reached.",
      0,
      "network_error",
    );
  }

  if (response.status === 401) {
    await session.reauthenticate();
  }
  const data = await readResponseBody(response, method, path);
  const requestId = requestIdFrom(response, data);
  if (!response.ok) {
    const error = isObject(data) && isObject(data.error) ? data.error : undefined;
    const message =
      (error && typeof error.message === "string" && error.message) ||
      (typeof data === "string" && data.trim()) ||
      `${method} ${path} failed (${response.status}).`;
    const code =
      (error && typeof error.code === "string" && error.code) ||
      (error && typeof error.type === "string" && error.type) ||
      defaultErrorCode(response.status);
    throw new ApiError(
      message,
      response.status,
      code,
      error?.details,
      requestId,
    );
  }

  if (options.validate && !options.validate(data)) {
    throw new ApiError(
      `${method} ${path} returned an invalid response (${response.status}).`,
      response.status,
      "invalid_response",
      undefined,
      requestId,
    );
  }

  return {
    data: data as T,
    etag: response.headers.get("ETag"),
    requestId,
    status: response.status,
  };
}

export type UserStatus = "active" | "blocked";

export interface QuotaLimits {
  daily_usd: number;
  daily_input_tokens: number;
  daily_output_tokens: number;
}

export interface UsageTotals {
  cost_usd: number;
  input_tokens: number;
  output_tokens: number;
  requests: number;
}

export interface AdminUser {
  user_id: string;
  name: string;
  status: UserStatus;
  status_reason: string;
  status_origin: string;
  version: number;
  created_at: string | null;
  updated_at: string | null;
  limits: QuotaLimits;
}

export interface UserRow extends AdminUser {
  today: UsageTotals;
}

export interface UserListResponse {
  users: UserRow[];
  next_cursor: string | null;
}

export interface ListUsersOptions {
  limit?: number;
  cursor?: string | null;
  status?: UserStatus;
  query?: string;
}

export interface CreateUserRequest extends QuotaLimits {
  user_id: string;
  name: string;
}

export interface CreateUserResponse {
  user_id: string;
  provisioned: boolean;
  limits: QuotaLimits;
  user: AdminUser;
}

export interface UserDetailResponse {
  user: AdminUser;
}

export interface UsageHistoryRow extends UsageTotals {
  user_id: string;
  window: string;
}

export interface UsageHistoryOptions {
  start?: string;
  end?: string;
  limit?: number;
  cursor?: string | null;
}

export interface UsageHistoryResponse {
  user_id: string;
  start: string;
  end: string;
  usage: UsageHistoryRow[];
  next_cursor: string | null;
}

export interface AuditSnapshotLimits {
  daily_usd_micro: number;
  daily_input_tokens: number;
  daily_output_tokens: number;
}

export interface AuditUserSnapshot {
  user_id: string;
  name: string;
  status: UserStatus;
  status_reason: string;
  status_origin: string;
  version: number;
  created_at: string | null;
  updated_at: string | null;
  limits: AuditSnapshotLimits;
}

export interface AuditEvent {
  user_id: string;
  event_key: string;
  event_type: string;
  actor: string;
  auth_method: string;
  reason: string;
  request_id: string;
  created_at: string;
  before: AuditUserSnapshot | null;
  after: AuditUserSnapshot;
}

export interface AuditListOptions {
  user_id?: string;
  limit?: number;
  cursor?: string | null;
}

export interface AuditListResponse {
  events: AuditEvent[];
  next_cursor: string | null;
}

export interface UserAuditListResponse extends AuditListResponse {
  user_id: string;
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
    today: UsageTotals;
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

export interface SetLimitsRequest extends QuotaLimits {
  reason?: string;
}

export interface SetLimitsResponse {
  user_id: string;
  updated: boolean;
  limits: QuotaLimits;
  user: AdminUser;
}

export interface SetStatusRequest {
  status: UserStatus;
  reason: string;
}

export interface SetStatusResponse {
  user_id: string;
  status: UserStatus;
  reason: string;
  user: AdminUser;
}

export interface VersionConflictDetails {
  current_user?: AdminUser;
}

function hasString(value: JsonObject, key: string): boolean {
  return typeof value[key] === "string";
}

function hasNumber(value: JsonObject, key: string): boolean {
  return typeof value[key] === "number" && Number.isFinite(value[key]);
}

function hasBoolean(value: JsonObject, key: string): boolean {
  return typeof value[key] === "boolean";
}

function hasNullableString(value: JsonObject, key: string): boolean {
  return value[key] === null || typeof value[key] === "string";
}

function hasNullableNumber(value: JsonObject, key: string): boolean {
  return value[key] === null || hasNumber(value, key);
}

function isNonNegativeNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0;
}

function isNonNegativeInteger(value: unknown): value is number {
  return isNonNegativeNumber(value) && Number.isInteger(value);
}

function isUsageTotals(value: unknown): value is UsageTotals {
  return isObject(value) &&
    isNonNegativeNumber(value.cost_usd) &&
    isNonNegativeInteger(value.input_tokens) &&
    isNonNegativeInteger(value.output_tokens) &&
    isNonNegativeInteger(value.requests);
}

function isQuotaLimits(value: unknown): value is QuotaLimits {
  return isObject(value) &&
    isNonNegativeNumber(value.daily_usd) &&
    isNonNegativeInteger(value.daily_input_tokens) &&
    isNonNegativeInteger(value.daily_output_tokens);
}

export function isAdminUser(value: unknown): value is AdminUser {
  return isObject(value) &&
    hasString(value, "user_id") &&
    hasString(value, "name") &&
    (value.status === "active" || value.status === "blocked") &&
    hasString(value, "status_reason") &&
    hasString(value, "status_origin") &&
    isNonNegativeInteger(value.version) &&
    hasNullableString(value, "created_at") &&
    hasNullableString(value, "updated_at") &&
    isQuotaLimits(value.limits);
}

function isUserRow(value: unknown): value is UserRow {
  return isAdminUser(value) && isObject(value) && isUsageTotals(value.today);
}

function isUserListResponse(value: unknown): value is UserListResponse {
  return isObject(value) &&
    Array.isArray(value.users) && value.users.every(isUserRow) &&
    (value.next_cursor === null || typeof value.next_cursor === "string");
}

function isCreateUserResponse(value: unknown): value is CreateUserResponse {
  return isObject(value) && hasString(value, "user_id") &&
    value.provisioned === true && isQuotaLimits(value.limits) &&
    isAdminUser(value.user);
}

function isUserDetailResponse(value: unknown): value is UserDetailResponse {
  return isObject(value) && isAdminUser(value.user);
}

function isIsoDate(value: unknown): value is string {
  return typeof value === "string" && /^\d{4}-\d{2}-\d{2}$/.test(value);
}

function isUsageHistoryRow(value: unknown): value is UsageHistoryRow {
  return isObject(value) && hasString(value, "user_id") &&
    isIsoDate(value.window) && isUsageTotals(value);
}

function isUsageHistoryResponse(value: unknown): value is UsageHistoryResponse {
  return isObject(value) && hasString(value, "user_id") &&
    isIsoDate(value.start) && isIsoDate(value.end) &&
    Array.isArray(value.usage) && value.usage.every(isUsageHistoryRow) &&
    (value.next_cursor === null || typeof value.next_cursor === "string");
}

function isAuditSnapshotLimits(value: unknown): value is AuditSnapshotLimits {
  return isObject(value) &&
    isNonNegativeInteger(value.daily_usd_micro) &&
    isNonNegativeInteger(value.daily_input_tokens) &&
    isNonNegativeInteger(value.daily_output_tokens);
}

function isAuditUserSnapshot(value: unknown): value is AuditUserSnapshot {
  return isObject(value) && hasString(value, "user_id") && hasString(value, "name") &&
    (value.status === "active" || value.status === "blocked") &&
    hasString(value, "status_reason") && hasString(value, "status_origin") &&
    isNonNegativeInteger(value.version) && hasNullableString(value, "created_at") &&
    hasNullableString(value, "updated_at") && isAuditSnapshotLimits(value.limits);
}

function isAuditEvent(value: unknown): value is AuditEvent {
  if (!isObject(value) || ![
    "user_id", "event_key", "event_type", "actor", "auth_method",
    "reason", "request_id", "created_at",
  ].every((key) => hasString(value, key)) ||
    !isAuditUserSnapshot(value.after) ||
    !(value.before === null || isAuditUserSnapshot(value.before))) return false;
  return value.after.user_id === value.user_id &&
    (value.before === null || value.before.user_id === value.user_id);
}

function isAuditListResponse(value: unknown): value is AuditListResponse {
  return isObject(value) && Array.isArray(value.events) &&
    value.events.every(isAuditEvent) &&
    (value.next_cursor === null || typeof value.next_cursor === "string");
}

function isUserAuditListResponse(value: unknown): value is UserAuditListResponse {
  return isAuditListResponse(value) && isObject(value) && hasString(value, "user_id");
}

function isSummary(value: unknown): value is Summary {
  if (!isObject(value) || !isObject(value.enforcement) || !isObject(value.observability)) return false;
  const enforcement = value.enforcement;
  const observability = value.observability;
  return ["source", "as_of", "window", "mode"].every((key) => hasString(enforcement, key)) &&
    [
      "credential_ttl_seconds",
      "permission_lease_seconds",
      "post_detection_fallback_seconds",
      "refresh_overlap_seconds",
      "refresh_jitter_seconds",
      "vend_rate_limit_per_minute",
      "revocation_policy_shards",
      "revocation_reconcile_minutes",
      "total_users",
      "blocked_users",
    ].every((key) => hasNumber(enforcement, key)) &&
    Array.isArray(enforcement.blocked_user_ids) && enforcement.blocked_user_ids.every((item) => typeof item === "string") &&
    isUsageTotals(enforcement.today) &&
    ["source", "delivery", "metrics_namespace", "detection_lag_metric"].every((key) => hasString(observability, key));
}

function isOperations(value: unknown): value is Operations {
  if (!isObject(value) || !hasString(value, "as_of") ||
      !isObject(value.configuration) || !isObject(value.emergency) ||
      !isObject(value.qualification) || !isObject(value.metrics) ||
      !isObject(value.cloudwatch) || !Array.isArray(value.alarms)) return false;
  const config = value.configuration;
  const emergency = value.emergency;
  const qualification = value.qualification;
  const metrics = value.metrics;
  return hasString(config, "mode") &&
    [
      "credential_ttl_seconds", "permission_lease_seconds", "post_detection_fallback_seconds",
      "refresh_overlap_seconds", "refresh_jitter_seconds", "vend_rate_limit_per_minute",
      "revocation_policy_shards", "revocation_policy_max_characters", "revocation_reconcile_minutes",
    ].every((key) => hasNumber(config, key)) &&
    hasBoolean(config, "permission_lease_enabled") && hasNullableNumber(config, "effective_permission_lease_seconds") &&
    hasBoolean(config, "revocation_enabled") &&
    hasString(emergency, "state") && hasBoolean(emergency, "desired_active") &&
    hasNumber(emergency, "generation") && hasNumber(emergency, "applied_generation") &&
    hasNullableString(emergency, "requested_at") && hasNullableString(emergency, "applied_at") &&
    hasBoolean(emergency, "converged") &&
    ["status", "emergency_status", "source"].every((key) => hasString(qualification, key)) &&
    ["namespace", "detection_lag_metric", "telemetry_status", "reconciliation_status"].every((key) => hasString(metrics, key)) &&
    ["detection_lag_p95_ms", "recent_sync_failure_count", "recent_overflow_count", "recent_emergency_failure_count", "revoked_identities_desired"].every((key) => hasNullableNumber(metrics, key)) &&
    ["detection_lag_timestamp", "last_reconciliation_at"].every((key) => hasNullableString(metrics, key)) &&
    hasNumber(metrics, "window_minutes") &&
    value.alarms.every((alarm) => isObject(alarm) && hasString(alarm, "key") && hasString(alarm, "state") && hasNullableString(alarm, "updated_at")) &&
    hasString(value.cloudwatch, "status") &&
    (value.cloudwatch.error_code === undefined || typeof value.cloudwatch.error_code === "string");
}

function isSetLimitsResponse(value: unknown): value is SetLimitsResponse {
  return isObject(value) && hasString(value, "user_id") && value.updated === true &&
    isQuotaLimits(value.limits) && isAdminUser(value.user);
}

function isSetStatusResponse(value: unknown): value is SetStatusResponse {
  return isObject(value) && hasString(value, "user_id") &&
    (value.status === "active" || value.status === "blocked") &&
    hasString(value, "reason") && isAdminUser(value.user);
}

export function normalizeDailyUsd(value: number): number {
  let micro = Math.round(value * 1_000_000);
  if (value > 0 && micro === 0) micro = 1;
  return micro / 1_000_000;
}

function sameLimits(left: QuotaLimits, right: QuotaLimits): boolean {
  return left.daily_usd === right.daily_usd &&
    left.daily_input_tokens === right.daily_input_tokens &&
    left.daily_output_tokens === right.daily_output_tokens;
}

function mutationHeaders(user: AdminUser): Record<string, string> {
  return {
    "Idempotency-Key": globalThis.crypto.randomUUID(),
    "If-Match": `"${user.version}"`,
  };
}

export function apiErrorMessage(caught: unknown): string {
  if (!(caught instanceof ApiError)) {
    return caught instanceof Error ? caught.message : String(caught);
  }

  const suffix = caught.requestId ? ` Request ID: ${caught.requestId}.` : "";
  if (caught.status === 0) return `Unable to reach the quota gateway. Check your connection and try again.${suffix}`;
  if (caught.status === 401) return `Your session expired. Sign in again.${suffix}`;
  if (caught.status === 403) return `You are not authorized to perform this action.${suffix}`;
  if (caught.status === 429) return `Too many admin requests. Wait and try again.${suffix}`;
  if (caught.status === 503) return `The quota service is temporarily unavailable. Try again.${suffix}`;
  if (caught.status === 409 && caught.code === "version_conflict") {
    return `This user changed since you opened it. The latest state is shown; review it before retrying.${suffix}`;
  }
  return `${caught.message}${suffix}`;
}

export const api = {
  summary: async (cfg: AdminConfig, session: Session): Promise<Summary> =>
    (await transport<Summary>(cfg, session, "GET", "/admin/summary", { validate: isSummary })).data,

  operations: async (cfg: AdminConfig, session: Session): Promise<Operations> =>
    (await transport<Operations>(cfg, session, "GET", "/admin/operations", { validate: isOperations })).data,

  listUsersPage: async (
    cfg: AdminConfig,
    session: Session,
    options: ListUsersOptions = {},
  ): Promise<UserListResponse> => {
    const params = new URLSearchParams({ limit: String(options.limit ?? 25) });
    if (options.cursor) params.set("cursor", options.cursor);
    if (options.status) params.set("status", options.status);
    if (options.query?.trim()) params.set("query", options.query.trim());
    return (await transport<UserListResponse>(
      cfg,
      session,
      "GET",
      `/admin/users?${params.toString()}`,
      { validate: isUserListResponse },
    )).data;
  },

  createUser: (
    cfg: AdminConfig,
    session: Session,
    request: CreateUserRequest,
  ): Promise<TransportResponse<CreateUserResponse>> => {
    const body: CreateUserRequest = {
      ...request,
      user_id: request.user_id.trim(),
      name: request.name.trim(),
      daily_usd: normalizeDailyUsd(request.daily_usd),
    };
    return transport(cfg, session, "POST", "/admin/users", {
      body,
      headers: { "Idempotency-Key": globalThis.crypto.randomUUID() },
      validate: (value): value is CreateUserResponse =>
        isCreateUserResponse(value) &&
        value.user_id === body.user_id &&
        value.user.user_id === body.user_id &&
        value.user.name === body.name &&
        sameLimits(value.limits, value.user.limits) &&
        sameLimits(value.user.limits, body),
    });
  },

  getUser: (
    cfg: AdminConfig,
    session: Session,
    userId: string,
  ): Promise<TransportResponse<UserDetailResponse>> => {
    const params = new URLSearchParams({ user_id: userId });
    return transport(cfg, session, "GET", `/admin/user?${params.toString()}`, {
      validate: (value): value is UserDetailResponse =>
        isUserDetailResponse(value) && value.user.user_id === userId,
    });
  },

  usageHistory: async (
    cfg: AdminConfig,
    session: Session,
    userId: string,
    options: UsageHistoryOptions = {},
  ): Promise<UsageHistoryResponse> => {
    const params = new URLSearchParams({
      user_id: userId,
      limit: String(options.limit ?? 25),
    });
    if (options.start) params.set("start", options.start);
    if (options.end) params.set("end", options.end);
    if (options.cursor) params.set("cursor", options.cursor);
    return (await transport<UsageHistoryResponse>(
      cfg,
      session,
      "GET",
      `/admin/user/usage-history?${params.toString()}`,
      {
        validate: (value): value is UsageHistoryResponse =>
          isUsageHistoryResponse(value) && value.user_id === userId &&
          value.usage.every((row) => row.user_id === userId) &&
          (options.start === undefined || value.start === options.start) &&
          (options.end === undefined || value.end === options.end),
      },
    )).data;
  },

  listAuditPage: async (
    cfg: AdminConfig,
    session: Session,
    options: AuditListOptions = {},
  ): Promise<AuditListResponse> => {
    const params = new URLSearchParams({ limit: String(options.limit ?? 25) });
    if (options.user_id) params.set("user_id", options.user_id);
    if (options.cursor) params.set("cursor", options.cursor);
    return (await transport<AuditListResponse>(
      cfg,
      session,
      "GET",
      `/admin/audit?${params.toString()}`,
      {
        validate: (value): value is AuditListResponse =>
          isAuditListResponse(value) &&
          (options.user_id === undefined || value.events.every((event) => event.user_id === options.user_id)),
      },
    )).data;
  },

  listUserAuditPage: async (
    cfg: AdminConfig,
    session: Session,
    userId: string,
    options: Omit<AuditListOptions, "user_id"> = {},
  ): Promise<UserAuditListResponse> => {
    const params = new URLSearchParams({
      user_id: userId,
      limit: String(options.limit ?? 25),
    });
    if (options.cursor) params.set("cursor", options.cursor);
    return (await transport<UserAuditListResponse>(
      cfg,
      session,
      "GET",
      `/admin/user/audit?${params.toString()}`,
      {
        validate: (value): value is UserAuditListResponse =>
          isUserAuditListResponse(value) && value.user_id === userId &&
          value.events.every((event) => event.user_id === userId),
      },
    )).data;
  },

  setLimits: (
    cfg: AdminConfig,
    session: Session,
    user: AdminUser,
    limits: SetLimitsRequest,
  ): Promise<TransportResponse<SetLimitsResponse>> => {
    const { reason, ...quotaLimits } = limits;
    const trimmedReason = reason?.trim();
    const normalized: SetLimitsRequest = {
      ...quotaLimits,
      daily_usd: normalizeDailyUsd(quotaLimits.daily_usd),
      ...(trimmedReason ? { reason: trimmedReason } : {}),
    };
    const params = new URLSearchParams({ user_id: user.user_id });
    return transport(cfg, session, "PUT", `/admin/user/limits?${params.toString()}`, {
      body: normalized,
      headers: mutationHeaders(user),
      validate: (value): value is SetLimitsResponse =>
        isSetLimitsResponse(value) &&
        value.user_id === user.user_id &&
        value.user.user_id === user.user_id &&
        value.user.version > user.version &&
        sameLimits(value.limits, value.user.limits) &&
        sameLimits(value.user.limits, normalized),
    });
  },

  setStatus: (
    cfg: AdminConfig,
    session: Session,
    user: AdminUser,
    status: UserStatus,
    reason: string,
  ): Promise<TransportResponse<SetStatusResponse>> => {
    const params = new URLSearchParams({ user_id: user.user_id });
    return transport(cfg, session, "PUT", `/admin/user/status?${params.toString()}`, {
      body: { status, reason: reason.trim() } satisfies SetStatusRequest,
      headers: mutationHeaders(user),
      validate: (value): value is SetStatusResponse =>
        isSetStatusResponse(value) &&
        value.user_id === user.user_id &&
        value.user.user_id === user.user_id &&
        value.status === status &&
        value.user.status === status &&
        value.reason === reason.trim() &&
        value.user.status_reason === reason.trim() &&
        value.user.version > user.version,
    });
  },
};
