import { useEffect, useRef, useState } from "react";
import {
  AlertCircle,
  Check,
  ChevronDown,
  CircleDollarSign,
  Gauge,
  Layers3,
  Lock,
  LogOut,
  Pencil,
  RefreshCw,
  ScrollText,
  Search,
  ShieldAlert,
  ShieldCheck,
  SlidersHorizontal,
  Unlock,
  Users,
  X,
} from "lucide-react";
import { loadConfig, type AdminConfig } from "./config";
import { beginSignIn, handleAuthCallback, type Session } from "./auth";
import {
  ApiError,
  api,
  apiErrorMessage,
  isAdminUser,
  type AdminUser,
  type CurrentUsage,
  type Operations,
  type QuotaLimits,
  type QuotaPeriod,
  type SetLimitsRequest,
  type Summary,
  type TransportResponse,
  type UserRow,
  type UserStatus,
} from "./api";
import { CreateUserWizard, GlobalAuditView, UserDetailDrawer } from "./OperationalUi";
import { OverviewCharts } from "./OverviewCharts";
import { OperationsView } from "./Operations";
import { useModalLifecycle } from "./modal";

export type UserFilter = "all" | "active" | "blocked" | "users" | "workloads";
export type DashboardView = "overview" | "users" | "operations" | "audit";

export function matchesUserFilter(user: AdminUser, filter: UserFilter): boolean {
  if (filter === "active" || filter === "blocked") return user.status === filter;
  if (filter === "workloads") return user.user_id.startsWith("workload:");
  if (filter === "users") return !user.user_id.startsWith("workload:");
  return true;
}
const USER_PAGE_SIZE = 25;
const QUOTA_PERIODS: QuotaPeriod[] = ["daily", "weekly", "monthly"];

function periodBounds(period: QuotaPeriod, now = new Date()): { start: Date; end: Date } {
  const start = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()));
  if (period === "weekly") start.setUTCDate(start.getUTCDate() - ((start.getUTCDay() + 6) % 7));
  if (period === "monthly") start.setUTCDate(1);
  const end = new Date(start);
  if (period === "daily") end.setUTCDate(end.getUTCDate() + 1);
  else if (period === "weekly") end.setUTCDate(end.getUTCDate() + 7);
  else end.setUTCMonth(end.getUTCMonth() + 1);
  return { start, end };
}

function emptyCurrentUsage(): CurrentUsage {
  return Object.fromEntries(QUOTA_PERIODS.map((period) => {
    const { start, end } = periodBounds(period);
    return [period, {
      period,
      window: start.toISOString().slice(0, 10),
      window_start: start.toISOString(),
      window_end: end.toISOString(),
      resets_at: end.toISOString(),
      cost_usd: 0,
      input_tokens: 0,
      output_tokens: 0,
      requests: 0,
    }];
  })) as unknown as CurrentUsage;
}

export function mergeCanonicalUser(rows: UserRow[], updated: AdminUser): UserRow[] {
  return rows.map((user) => user.user_id === updated.user_id && updated.version >= user.version
    ? { ...updated, today: user.today, current_usage: user.current_usage }
    : user);
}

export function mergeRefreshedUsers(current: UserRow[], refreshed: UserRow[]): UserRow[] {
  const currentById = new Map(current.map((user) => [user.user_id, user]));
  return refreshed.map((user) => {
    const cached = currentById.get(user.user_id);
    return cached && cached.version > user.version
      ? { ...cached, today: user.today, current_usage: user.current_usage }
      : user;
  });
}

export function App() {
  const [cfg, setCfg] = useState<AdminConfig | null>(null);
  const [cfgError, setCfgError] = useState("");
  const [authError, setAuthError] = useState("");
  const [authReady, setAuthReady] = useState(false);
  const [session, setSession] = useState<Session | null>(null);
  const [initialAuthHref] = useState(() => window.location.href);

  useEffect(() => {
    let active = true;
    let config: AdminConfig;
    try {
      config = loadConfig();
      setCfg(config);
    } catch (error) {
      setCfgError((error as Error).message);
      return () => {
        active = false;
      };
    }

    void handleAuthCallback(config, {
      onReauthenticate: () => {
        if (active) setSession(null);
      },
    }, initialAuthHref).then((nextSession) => {
      if (active) setSession(nextSession);
    }).catch((error: unknown) => {
      if (active) setAuthError((error as Error).message);
    }).finally(() => {
      if (active) setAuthReady(true);
    });

    return () => {
      active = false;
    };
  }, []);

  if (cfgError) {
    return (
      <Centered>
        <ErrorMessage message={cfgError} />
      </Centered>
    );
  }
  if (!cfg || !authReady) {
    return (
      <Centered>
        <LoadingState label={cfg ? "Completing sign in" : "Loading console"} />
      </Centered>
    );
  }
  if (!session) return <Login cfg={cfg} initialError={authError} />;
  return (
    <Dashboard
      cfg={cfg}
      session={session}
      onSignOut={() => {
        setSession(null);
        session.logout();
      }}
    />
  );
}

function Login({ cfg, initialError }: { cfg: AdminConfig; initialError: string }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(initialError);

  async function submit() {
    setBusy(true);
    setError("");
    try {
      await beginSignIn(cfg);
    } catch (caught) {
      setError((caught as Error).message);
      setBusy(false);
    }
  }

  return (
    <main className="login-page">
      <section className="login-card" aria-labelledby="login-title">
        <Brand />
        <div className="login-heading">
          <p className="eyebrow">Administration</p>
          <h1 id="login-title">Sign in to quota controls</h1>
          <p>Continue to the Cognito managed login for your organization account.</p>
        </div>

        {error && <ErrorMessage message={error} />}
        <button
          className="button button-primary login-submit"
          disabled={busy}
          onClick={() => void submit()}
          type="button"
        >
          {busy && <RefreshCw className="spin" aria-hidden="true" size={17} />}
          {busy ? "Redirecting" : "Continue to sign in"}
        </button>

        <div className="login-security">
          <ShieldCheck aria-hidden="true" size={17} />
          <span>PKCE protects the redirect; access still requires the configured admin group.</span>
        </div>
      </section>
    </main>
  );
}

export function Dashboard({
  cfg,
  session,
  onSignOut,
}: {
  cfg: AdminConfig;
  session: Session;
  onSignOut: () => void;
}) {
  const [view, setView] = useState<DashboardView>("overview");
  const [summary, setSummary] = useState<Summary | null>(null);
  const [operations, setOperations] = useState<Operations | null>(null);
  const [users, setUsers] = useState<UserRow[]>([]);
  const [usersNextCursor, setUsersNextCursor] = useState<string | null>(null);
  const [userCursors, setUserCursors] = useState<Array<string | null>>([null]);
  const [userPageIndex, setUserPageIndex] = useState(0);
  const [userQuery, setUserQuery] = useState("");
  const [userFilter, setUserFilter] = useState<UserFilter>("all");
  const [selectedUserId, setSelectedUserId] = useState<string | null>(null);
  const [summaryError, setSummaryError] = useState("");
  const [usersError, setUsersError] = useState("");
  const [operationsError, setOperationsError] = useState("");
  const [summaryStale, setSummaryStale] = useState(false);
  const [usersStale, setUsersStale] = useState(false);
  const [operationsStale, setOperationsStale] = useState(false);
  const [summaryLoading, setSummaryLoading] = useState(true);
  const [usersLoading, setUsersLoading] = useState(true);
  const [operationsLoading, setOperationsLoading] = useState(true);
  const summaryRequest = useRef(0);
  const usersRequest = useRef(0);
  const operationsRequest = useRef(0);

  async function refreshSummary() {
    const request = ++summaryRequest.current;
    setSummaryLoading(true);
    setSummaryError("");
    try {
      const next = await api.summary(cfg, session);
      if (request !== summaryRequest.current) return;
      setSummary(next);
      setSummaryStale(false);
    } catch (caught) {
      if (request !== summaryRequest.current) return;
      setSummaryError(apiErrorMessage(caught));
      setSummaryStale(true);
    } finally {
      if (request === summaryRequest.current) setSummaryLoading(false);
    }
  }

  async function loadUsersPage({
    cursor = userCursors[userPageIndex],
    targetIndex = userPageIndex,
    query = userQuery,
    filter = userFilter,
    resetHistory = false,
  }: {
    cursor?: string | null;
    targetIndex?: number;
    query?: string;
    filter?: UserFilter;
    resetHistory?: boolean;
  } = {}): Promise<boolean> {
    const request = ++usersRequest.current;
    setUsersLoading(true);
    setUsersError("");
    try {
      const page = await api.listUsersPage(cfg, session, {
        limit: USER_PAGE_SIZE,
        cursor,
        status: filter === "active" || filter === "blocked" ? filter : undefined,
        granularity: filter === "workloads" ? "workload" : filter === "users" ? "user" : undefined,
        query,
      });
      if (request !== usersRequest.current) return false;
      setUsers((current) => mergeRefreshedUsers(current, page.users));
      setUsersNextCursor(page.next_cursor);
      setUserQuery(query);
      setUserFilter(filter);
      if (resetHistory) {
        setUserCursors([null]);
        setUserPageIndex(0);
      } else {
        setUserCursors((current) => targetIndex > userPageIndex
          ? [...current.slice(0, userPageIndex + 1), cursor]
          : current);
        setUserPageIndex(targetIndex);
      }
      setSelectedUserId((current) => current && page.users.some((user) => user.user_id === current) ? current : null);
      setUsersStale(false);
      return true;
    } catch (caught) {
      if (request !== usersRequest.current) return false;
      setUsersError(apiErrorMessage(caught));
      setUsersStale(true);
      return false;
    } finally {
      if (request === usersRequest.current) setUsersLoading(false);
    }
  }

  async function refreshOperations() {
    const request = ++operationsRequest.current;
    setOperationsLoading(true);
    setOperationsError("");
    try {
      const next = await api.operations(cfg, session);
      if (request !== operationsRequest.current) return;
      setOperations(next);
      setOperationsStale(false);
    } catch (caught) {
      if (request !== operationsRequest.current) return;
      setOperationsError(apiErrorMessage(caught));
      setOperationsStale(true);
    } finally {
      if (request === operationsRequest.current) setOperationsLoading(false);
    }
  }

  async function refresh() {
    await Promise.allSettled([refreshSummary(), loadUsersPage(), refreshOperations()]);
  }

  function replaceUser(updated: AdminUser) {
    usersRequest.current += 1;
    setUsersLoading(false);
    setUsers((current) => {
      const cached = current.find((user) => user.user_id === updated.user_id);
      if (cached && updated.version < cached.version) return current;
      return !matchesUserFilter(updated, userFilter)
        ? current.filter((user) => user.user_id !== updated.user_id)
        : mergeCanonicalUser(current, updated);
    });
  }

  function addCreatedUser(created: AdminUser, openDetails: boolean) {
    const needle = userQuery.trim().toLocaleLowerCase();
    const matches = matchesUserFilter(created, userFilter) &&
      (!needle || created.user_id.toLocaleLowerCase().includes(needle) || created.name.toLocaleLowerCase().includes(needle));
    const fitsCurrentPage = userPageIndex === 0 && users.length < USER_PAGE_SIZE && matches;
    if (fitsCurrentPage) {
      const row: UserRow = {
        ...created,
        today: { cost_usd: 0, input_tokens: 0, output_tokens: 0, requests: 0 },
        current_usage: emptyCurrentUsage(),
      };
      setUsers((current) => [row, ...current.filter((user) => user.user_id !== created.user_id)]);
      if (openDetails) setSelectedUserId(created.user_id);
    }
    void refreshSummary();
  }

  function openAuditTarget(userId: string) {
    setView("users");
    if (users.some((user) => user.user_id === userId)) {
      setSelectedUserId(userId);
      return;
    }
    setSelectedUserId(null);
    void loadUsersPage({ cursor: null, targetIndex: 0, query: userId, filter: "all", resetHistory: true });
  }

  useEffect(() => { void refresh(); }, []);

  const refreshing = summaryLoading || usersLoading || operationsLoading;
  const tabs: Array<{ id: DashboardView; label: string; icon: React.ReactNode }> = [
    { id: "overview", label: "Overview", icon: <Gauge aria-hidden="true" size={15} /> },
    { id: "users", label: "Users", icon: <Users aria-hidden="true" size={15} /> },
    { id: "operations", label: "Operations", icon: <SlidersHorizontal aria-hidden="true" size={15} /> },
    { id: "audit", label: "Audit log", icon: <ScrollText aria-hidden="true" size={15} /> },
  ];

  return (
    <div className="app-shell">
      <header className="app-header">
        <div className="header-inner">
          <Brand />
          <div className="header-account">
            <div className="account-copy"><span>{session.email}</span><small>Administrator</small></div>
            <IconButton label="Refresh data" onClick={() => void refresh()}><RefreshCw className={refreshing ? "spin" : ""} aria-hidden="true" size={18} /></IconButton>
            <IconButton label="Sign out" onClick={onSignOut}><LogOut aria-hidden="true" size={18} /></IconButton>
          </div>
        </div>
      </header>

      <nav aria-label="Primary" className="primary-nav">
        <div>
          {tabs.map((tab) => (
            <button aria-current={view === tab.id ? "page" : undefined} key={tab.id} onClick={() => setView(tab.id)} type="button">
              {tab.icon}
              {tab.label}
            </button>
          ))}
        </div>
      </nav>

      <main className="dashboard">
        {view === "overview" && (
          <>
            <div className="page-heading">
              <div><p className="eyebrow">Amazon Bedrock</p><h1>Quota overview</h1></div>
              {summary && <p className="updated-at">Updated {new Date(summary.enforcement.as_of).toLocaleString()}</p>}
            </div>

            {(summaryError || (summaryStale && summary)) && (
              <div className="panel-feedback" aria-label="Summary refresh status">
                {summaryStale && summary && <span className="ops-status ops-status-amber"><span aria-hidden="true" />Cached summary · refresh failed</span>}
                {summaryError && <ErrorMessage message={summaryError} />}
              </div>
            )}
            {summary ? <SummaryPanel summary={summary} /> : summaryLoading ? <SummarySkeleton /> : <UnavailableState label="Summary unavailable" />}

            <OverviewCharts cfg={cfg} refreshKey={summary?.enforcement.as_of} session={session} users={users} />
          </>
        )}

        {view === "users" && (
          <>
            <div className="page-heading">
              <div><p className="eyebrow">Amazon Bedrock</p><h1>User management</h1></div>
            </div>
            <UsersPanel
              cfg={cfg}
              enforcement={!summaryStale ? summary?.enforcement ?? null : null}
              error={usersError}
              filter={userFilter}
              hasNext={Boolean(usersNextCursor)}
              hasPrevious={userPageIndex > 0}
              loading={usersLoading}
              onCreateUser={addCreatedUser}
              onFilterChange={(filter) => void loadUsersPage({ cursor: null, targetIndex: 0, query: userQuery, filter, resetHistory: true })}
              onNext={() => usersNextCursor && void loadUsersPage({ cursor: usersNextCursor, targetIndex: userPageIndex + 1 })}
              onPrevious={() => void loadUsersPage({ cursor: userCursors[userPageIndex - 1], targetIndex: userPageIndex - 1 })}
              onSearch={(query) => void loadUsersPage({ cursor: null, targetIndex: 0, query, filter: userFilter, resetHistory: true })}
              onSelectedUserChange={setSelectedUserId}
              onSummaryRefresh={refreshSummary}
              onUserChanged={replaceUser}
              query={userQuery}
              selectedUserId={selectedUserId}
              session={session}
              stale={usersStale}
              users={users}
            />
          </>
        )}

        {view === "operations" && (
          <>
            <div className="page-heading">
              <div><p className="eyebrow">Amazon Bedrock</p><h1>Operations</h1></div>
            </div>
            <OperationsView
              cfg={cfg}
              error={operationsError}
              loading={operationsLoading}
              onChanged={() => { void refreshOperations(); void refreshSummary(); }}
              operations={operations}
              session={session}
              stale={operationsStale}
            />
          </>
        )}

        {view === "audit" && (
          <>
            <div className="page-heading"><div><p className="eyebrow">Amazon Bedrock</p><h1>Administrative history</h1></div></div>
            <GlobalAuditView cfg={cfg} onTargetUser={openAuditTarget} session={session} />
          </>
        )}
      </main>
    </div>
  );
}

function SummaryPanel({ summary }: { summary: Summary }) {
  const enforcement = summary.enforcement;
  const metrics = [
    {
      label: "Managed users",
      value: enforcement.total_users.toLocaleString(),
      icon: <Users aria-hidden="true" size={20} />,
      tone: "blue",
    },
    {
      label: "Blocked",
      value: enforcement.blocked_users.toLocaleString(),
      icon: <Lock aria-hidden="true" size={20} />,
      tone: enforcement.blocked_users > 0 ? "red" : "green",
    },
    {
      label: "Spend today",
      value: formatUsd(enforcement.today.cost_usd, 4),
      icon: <CircleDollarSign aria-hidden="true" size={20} />,
      tone: "orange",
    },
    {
      label: "Requests today",
      value: enforcement.today.requests.toLocaleString(),
      icon: <Gauge aria-hidden="true" size={20} />,
      tone: "green",
    },
  ];

  return (
    <>
      <section className="metric-grid" aria-label="Quota summary">
        {metrics.map((metric) => (
          <article className="metric-card" key={metric.label}>
            <div className={`metric-icon metric-icon-${metric.tone}`}>{metric.icon}</div>
            <div>
              <p>{metric.label}</p>
              <strong>{metric.value}</strong>
            </div>
          </article>
        ))}
      </section>

      <section className="system-strip" aria-label="Enforcement details">
        <div className="system-status">
          <span className="status-dot" aria-hidden="true" />
          <div>
            <strong>Enforcement active</strong>
            <span>{enforcement.mode.replace(/_/g, " ")}</span>
          </div>
        </div>
        <SystemDetail label="Source" value={enforcement.source} />
        <SystemDetail label="Window" value={enforcement.window} />
        <SystemDetail
          label="STS lifetime"
          value={`${Math.round(enforcement.credential_ttl_seconds / 60)} min`}
        />
        <SystemDetail
          label="Permission cutoff"
          value={`${Math.round(enforcement.post_detection_fallback_seconds / 60)} min fallback`}
        />
        <SystemDetail
          label="Refresh"
          value={`${enforcement.refresh_overlap_seconds}s overlap · ${enforcement.refresh_jitter_seconds}s jitter`}
        />
        <SystemDetail
          label="Telemetry"
          value={`${summary.observability.source} · ${summary.observability.detection_lag_metric}`}
        />
        <SystemDetail label="Metrics" value={summary.observability.metrics_namespace} />
      </section>
    </>
  );
}

function formatOperationalLabel(value: string): string {
  return value.replace(/_/g, " ");
}

function formatDuration(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  return `${seconds / 60} min`;
}

export function UsersPanel({
  cfg,
  enforcement,
  error,
  filter = "all",
  hasNext = false,
  hasPrevious = false,
  loading,
  onCreateUser,
  onFilterChange = () => undefined,
  onNext = () => undefined,
  onPrevious = () => undefined,
  onSearch = () => undefined,
  onSelectedUserChange = () => undefined,
  onSummaryRefresh,
  onUserChanged,
  query = "",
  selectedUserId = null,
  session,
  stale,
  users,
}: {
  cfg: AdminConfig;
  enforcement: Summary["enforcement"] | null;
  error: string;
  filter?: UserFilter;
  hasNext?: boolean;
  hasPrevious?: boolean;
  loading: boolean;
  onCreateUser?: (user: AdminUser, openDetails: boolean) => void;
  onFilterChange?: (filter: UserFilter) => void;
  onNext?: () => void;
  onPrevious?: () => void;
  onSearch?: (query: string) => void;
  onSelectedUserChange?: (userId: string | null) => void;
  onSummaryRefresh: () => Promise<void>;
  onUserChanged: (user: AdminUser) => void;
  query?: string;
  selectedUserId?: string | null;
  session: Session;
  stale: boolean;
  users: UserRow[];
}) {
  const [searchDraft, setSearchDraft] = useState(query);
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<UserRow | null>(null);
  const [statusChanging, setStatusChanging] = useState<UserRow | null>(null);
  const [busyUsers, setBusyUsers] = useState<Set<string>>(() => new Set());
  const [period, setPeriod] = useState<QuotaPeriod>("daily");
  const [actionError, setActionError] = useState("");
  const [notice, setNotice] = useState("");
  const selectedUser = selectedUserId ? users.find((user) => user.user_id === selectedUserId) ?? null : null;

  useEffect(() => { setSearchDraft(query); }, [query]);
  useEffect(() => {
    if (!enforcement) setStatusChanging(null);
  }, [enforcement]);

  function setUserBusy(userId: string, busy: boolean) {
    setBusyUsers((current) => {
      const next = new Set(current);
      if (busy) next.add(userId);
      else next.delete(userId);
      return next;
    });
  }

  function applyCanonicalUser(existing: UserRow, canonical: AdminUser): UserRow {
    onUserChanged(canonical);
    return {
      ...canonical,
      today: existing.today,
      current_usage: existing.current_usage,
    };
  }

  async function mutate(
    user: UserRow,
    successMessage: string,
    fn: () => Promise<TransportResponse<{ user: AdminUser }>>,
  ): Promise<boolean> {
    setUserBusy(user.user_id, true);
    setActionError("");
    setNotice("");
    try {
      const result = await fn();
      applyCanonicalUser(user, result.data.user);
      setNotice(successMessage);
      void onSummaryRefresh().catch(() => undefined);
      return true;
    } catch (caught) {
      let message = apiErrorMessage(caught);
      if (caught instanceof ApiError && caught.status === 409 && caught.code === "version_conflict") {
        const details = caught.details as { current_user?: unknown } | undefined;
        const current = details?.current_user;
        if (isAdminUser(current) && current.user_id === user.user_id && current.version > user.version) {
          const reconciled = applyCanonicalUser(user, current);
          if (editing?.user_id === user.user_id) setEditing(reconciled);
          if (statusChanging?.user_id === user.user_id) setStatusChanging(reconciled);
        } else {
          message = apiErrorMessage(new ApiError(
            "The version conflict response did not identify the requested user.",
            409,
            "invalid_response",
            undefined,
            caught.requestId,
          ));
        }
      }
      setActionError(message);
      return false;
    } finally {
      setUserBusy(user.user_id, false);
    }
  }

  async function saveLimits(user: UserRow, limits: SetLimitsRequest) {
    const saved = await mutate(user, `Limits saved for ${displayName(user)}.`, () => api.setLimits(cfg, session, user, limits));
    if (saved) setEditing(null);
  }

  async function saveStatus(user: UserRow, reason: string) {
    const nextStatus: UserStatus = user.status === "active" ? "blocked" : "active";
    const saved = await mutate(user, `${displayName(user)} is now ${nextStatus}.`, () => api.setStatus(cfg, session, user, nextStatus, reason));
    if (saved) setStatusChanging(null);
  }

  function edit(user: UserRow) {
    setActionError("");
    setNotice("");
    setEditing(user);
  }

  function changeStatus(user: UserRow) {
    setActionError("");
    setNotice("");
    setStatusChanging(user);
  }

  return (
    <section className="users-panel" aria-labelledby="users-title" aria-busy={loading}>
      <div className="panel-heading">
        <div><h2 id="users-title">Users</h2><p>{error && users.length === 0 ? "User data unavailable" : `${users.length} identities on this page`}</p></div>
        <div className="users-heading-actions">
          {stale && users.length > 0 && <span className="ops-status ops-status-amber"><span aria-hidden="true" />Cached users · refresh failed</span>}
          <button className="button button-primary" onClick={() => { setActionError(""); setNotice(""); setCreating(true); }} type="button">Create user</button>
          <div className="user-tools">
            <div className="select-wrap"><select aria-label="Usage period" value={period} onChange={(event) => setPeriod(event.target.value as QuotaPeriod)}><option value="daily">Daily window</option><option value="weekly">Weekly window</option><option value="monthly">Monthly window</option></select><ChevronDown aria-hidden="true" size={16} /></div>
            <form className="search-form" onSubmit={(event) => { event.preventDefault(); onSearch(searchDraft.trim()); }}>
              <div className="search-field"><Search aria-hidden="true" size={17} /><input aria-label="Search users" placeholder="Search users" type="search" value={searchDraft} onChange={(event) => setSearchDraft(event.target.value)} /></div>
              <button className="button button-secondary" disabled={loading} type="submit">Search</button>
            </form>
            <div className="select-wrap"><select aria-label="Filter users" disabled={loading} value={filter} onChange={(event) => onFilterChange(event.target.value as UserFilter)}><option value="all">All subjects</option><option value="active">Active</option><option value="blocked">Blocked</option><option value="users">Users (JWT)</option><option value="workloads">Workloads</option></select><ChevronDown aria-hidden="true" size={16} /></div>
          </div>
        </div>
      </div>

      {error && <ErrorMessage message={error} />}
      {actionError && !editing && !statusChanging && <ErrorMessage message={actionError} dismiss={() => setActionError("")} />}
      {notice && <SuccessMessage message={notice} dismiss={() => setNotice("")} />}
      <p className="period-display-note">Showing {period} usage. All enabled calendar periods are enforced concurrently.</p>

      <div aria-label="Users on current page" className="table-scroll" role="region" tabIndex={0}>
        <table>
          <thead><tr><th>User</th><th>Status</th><th>{periodLabel(period)} USD</th><th>{periodLabel(period)} input</th><th>{periodLabel(period)} output</th><th>Requests</th><th><span className="sr-only">Actions</span></th></tr></thead>
          <tbody>{loading && users.length === 0 ? <TableSkeleton /> : users.map((user) => (
            <UserTableRow
              busy={busyUsers.has(user.user_id)}
              key={user.user_id}
              onEdit={() => edit(user)}
              onOpen={() => onSelectedUserChange(user.user_id)}
              onRequestStatus={() => changeStatus(user)}
              period={period}
              statusActionAvailable={enforcement !== null}
              user={user}
            />
          ))}</tbody>
        </table>
        {!loading && users.length === 0 && (error ? <UnavailableState label="Users unavailable" /> : <EmptyState hasFilters={Boolean(query) || filter !== "all"} />)}
      </div>
      <div className="pagination users-pagination"><span>{users.length} identities on this page</span><div><button className="button button-secondary" disabled={loading || !hasPrevious} onClick={onPrevious} type="button">Previous</button><button className="button button-secondary" disabled={loading || !hasNext} onClick={onNext} type="button">Next</button></div></div>

      {creating && <CreateUserWizard cfg={cfg} onClose={() => setCreating(false)} onCreated={(created, openDetails) => { onCreateUser?.(created, openDetails); setNotice(`${created.name || created.user_id} was created.`); }} session={session} />}
      {selectedUser && <UserDetailDrawer cfg={cfg} onCanonical={onUserChanged} onClose={() => onSelectedUserChange(null)} onEdit={() => edit(selectedUser)} onStatus={() => changeStatus(selectedUser)} session={session} statusActionAvailable={enforcement !== null} suspended={Boolean(editing || statusChanging)} user={selectedUser} />}
      {editing && <LimitsDialog apiError={actionError} busy={busyUsers.has(editing.user_id)} key={`${editing.user_id}:${editing.version}`} onClose={() => setEditing(null)} onSave={(limits) => void saveLimits(editing, limits)} user={editing} />}
      {statusChanging && enforcement && <StatusDialog apiError={actionError} busy={busyUsers.has(statusChanging.user_id)} enforcement={enforcement} key={`${statusChanging.user_id}:${statusChanging.version}`} onClose={() => setStatusChanging(null)} onConfirm={(reason) => void saveStatus(statusChanging, reason)} user={statusChanging} />}
    </section>
  );
}

function UserTableRow({
  busy,
  onEdit,
  onOpen,
  onRequestStatus,
  period,
  statusActionAvailable,
  user,
}: {
  busy: boolean;
  onEdit: () => void;
  onOpen: () => void;
  onRequestStatus: () => void;
  period: QuotaPeriod;
  statusActionAvailable: boolean;
  user: UserRow;
}) {
  const isActive = user.status === "active";
  const usage = user.current_usage[period];
  const limits = user.limits[period];
  const highest = highestUtilization(user);

  return (
    <tr>
      <td>
        <div className="user-cell">
          <div className="user-avatar" aria-hidden="true">{initials(user)}</div>
          <div>
            <button className="user-name-button" disabled={busy} onClick={onOpen} title={user.name} type="button">{displayName(user)}</button>
            <span title={user.user_id}>{user.user_id}</span>
            {highest && <span className={`highest-utilization highest-${highest.level}`}>Highest: {periodLabel(highest.period)} {highest.percent}%</span>}
            {user.granularity === "workload" && (
              <span className="granularity-stack">
                <span className="status-badge status-workload">workload</span>
                {user.enforcement_ready === false && (
                  <span className="status-badge status-not-enforced" title="No IAM role configured: this workload is metered and alerted but cannot be hard-blocked. Add role_arn to the workloads config and redeploy.">
                    metering only
                  </span>
                )}
              </span>
            )}
          </div>
        </div>
      </td>
      <td>
        <div className="status-stack">
          <span className={`status-badge status-${isActive ? "active" : "blocked"}`}>
            <span aria-hidden="true" />
            {user.status}
          </span>
          <details className="status-details">
            <summary>Status details</summary>
            <dl>
              <div><dt>Origin</dt><dd>{user.status_origin || "Not provided"}</dd></div>
              <div><dt>Reason</dt><dd>{user.status_reason || "Not provided"}</dd></div>
            </dl>
          </details>
        </div>
      </td>
      <td>
        <QuotaUsage
          current={usage.cost_usd}
          enabled={limits !== null}
          format={(value) => formatUsd(value, 6)}
          limit={limits?.usd ?? 0}
        />
      </td>
      <td>
        <QuotaUsage
          current={usage.input_tokens}
          enabled={limits !== null}
          format={formatCompact}
          limit={limits?.input_tokens ?? 0}
        />
      </td>
      <td>
        <QuotaUsage
          current={usage.output_tokens}
          enabled={limits !== null}
          format={formatCompact}
          limit={limits?.output_tokens ?? 0}
        />
      </td>
      <td className="request-count">{usage.requests.toLocaleString()}</td>
      <td>
        <div className="row-actions">
          <IconButton disabled={busy} label={`Edit limits for ${displayName(user)}`} onClick={onEdit}>
            <Pencil aria-hidden="true" size={17} />
          </IconButton>
          <IconButton
            danger={isActive}
            disabled={busy || !statusActionAvailable}
            label={statusActionAvailable
              ? `${isActive ? "Block" : "Unblock"} ${displayName(user)}`
              : `Status change unavailable for ${displayName(user)} until a fresh enforcement summary loads`}
            onClick={onRequestStatus}
          >
            {busy ? (
              <RefreshCw className="spin" aria-hidden="true" size={17} />
            ) : isActive ? (
              <Lock aria-hidden="true" size={17} />
            ) : (
              <Unlock aria-hidden="true" size={17} />
            )}
          </IconButton>
        </div>
      </td>
    </tr>
  );
}

export function QuotaUsage({
  current,
  enabled = true,
  format,
  limit,
}: {
  current: number;
  enabled?: boolean;
  format: (value: number) => string;
  limit: number;
}) {
  if (!enabled) {
    return (
      <div className="quota-usage quota-disabled">
        <div><strong>{format(current)}</strong><span>period disabled</span></div>
        <span className="unlimited-label">Disabled</span>
      </div>
    );
  }
  if (limit === 0) {
    return (
      <div className="quota-usage quota-unlimited">
        <div>
          <strong>{format(current)}</strong>
          <span>of Unlimited</span>
        </div>
        <span className="unlimited-label">Unlimited</span>
      </div>
    );
  }

  const percentage = (current / limit) * 100;
  const level = percentage >= 100 ? "critical" : percentage >= 80 ? "warning" : "normal";
  const roundedPercentage = Math.round(percentage);

  return (
    <div className="quota-usage">
      <div>
        <strong>{format(current)}</strong>
        <span>of {format(limit)}</span>
      </div>
      <div
        aria-label={`${roundedPercentage} percent used`}
        aria-valuemax={100}
        aria-valuemin={0}
        aria-valuenow={Math.min(roundedPercentage, 100)}
        aria-valuetext={`${format(current)} of ${format(limit)} used (${roundedPercentage} percent)`}
        className="progress-track"
        role="progressbar"
      >
        <span
          className={`progress-fill progress-${level}`}
          style={{ width: `${Math.min(percentage, 100)}%` }}
        />
      </div>
    </div>
  );
}

type PeriodLimitDraft = {
  enabled: boolean;
  usd: string;
  input_tokens: string;
  output_tokens: string;
};

type LimitDraft = Record<QuotaPeriod, PeriodLimitDraft>;

function limitDraft(limits: QuotaLimits): LimitDraft {
  return Object.fromEntries(QUOTA_PERIODS.map((period) => {
    const value = limits[period];
    return [period, {
      enabled: value !== null,
      usd: String(value?.usd ?? 0),
      input_tokens: String(value?.input_tokens ?? 0),
      output_tokens: String(value?.output_tokens ?? 0),
    }];
  })) as unknown as LimitDraft;
}

function parseLimitDraft(draft: LimitDraft): QuotaLimits | null {
  const parsed: Partial<QuotaLimits> = {};
  for (const period of QUOTA_PERIODS) {
    const value = draft[period];
    if (!value.enabled) {
      parsed[period] = null;
      continue;
    }
    const usd = value.usd.trim() === "" ? Number.NaN : Number(value.usd);
    const input = value.input_tokens.trim() === "" ? Number.NaN : Number(value.input_tokens);
    const output = value.output_tokens.trim() === "" ? Number.NaN : Number(value.output_tokens);
    if (!Number.isFinite(usd) || usd < 0 || !Number.isInteger(input) || input < 0 || !Number.isInteger(output) || output < 0) return null;
    parsed[period] = { usd, input_tokens: input, output_tokens: output };
  }
  return Object.values(parsed).some((value) => value !== null)
    ? parsed as QuotaLimits
    : null;
}

export function LimitsDialog({
  apiError,
  busy,
  onClose,
  onSave,
  user,
}: {
  apiError: string;
  busy: boolean;
  onClose: () => void;
  onSave: (limits: SetLimitsRequest) => void;
  user: UserRow;
}) {
  // Editable snapshot of the user's limits. The parent keys this dialog by
  // user id and version, so a changed prop remounts it with a fresh draft.
  const [draft, setDraft] = useState<LimitDraft>(() => limitDraft(user.limits)); // nosemgrep
  const [reason, setReason] = useState("");
  const [unlimitedConfirmed, setUnlimitedConfirmed] = useState(false);
  const [error, setError] = useState("");
  const dialogRef = useRef<HTMLDivElement>(null);
  const firstFieldRef = useRef<HTMLInputElement>(null);
  useModalLifecycle(busy, onClose, dialogRef, firstFieldRef);

  const parsedLimits = parseLimitDraft(draft);
  const periodChanges = parsedLimits ? QUOTA_PERIODS.filter((period) => (user.limits[period] === null) !== (parsedLimits[period] === null)).map((period) => `${period} period`) : [];
  const unlimitedFields: string[] = [];
  const belowUsageFields: string[] = [];
  if (parsedLimits) {
    for (const period of QUOTA_PERIODS) {
      const previous = user.limits[period];
      const next = parsedLimits[period];
      if (!next) continue;
      const usage = user.current_usage[period];
      for (const dimension of ["usd", "input_tokens", "output_tokens"] as const) {
        if ((!previous || previous[dimension] > 0) && next[dimension] === 0) unlimitedFields.push(`${period} ${dimension.replace(/_/g, " ")}`);
        const current = dimension === "usd" ? usage.cost_usd : usage[dimension];
        if (next[dimension] > 0 && next[dimension] < current) belowUsageFields.push(`${period} ${dimension.replace(/_/g, " ")}`);
      }
    }
  }
  const reasonRequired = periodChanges.length > 0 || unlimitedFields.length > 0 || belowUsageFields.length > 0;

  function changed(period: QuotaPeriod, patch: Partial<PeriodLimitDraft>) {
    setDraft((current) => ({ ...current, [period]: { ...current[period], ...patch } }));
    setUnlimitedConfirmed(false);
    setError("");
  }

  function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!parsedLimits) {
      setError(Object.values(draft).some((value) => value.enabled)
        ? "Enter a non-negative USD amount and whole token values. Fields cannot be blank."
        : "Enable at least one calendar quota period.");
      return;
    }
    if (unlimitedFields.length > 0 && !unlimitedConfirmed) {
      setError("Confirm that the selected limits should become Unlimited.");
      return;
    }
    const trimmedReason = reason.trim();
    if (reasonRequired && !trimmedReason) {
      setError("Enter a reason for this sensitive limit change.");
      return;
    }
    onSave({
      limits: parsedLimits,
      ...(trimmedReason ? { reason: trimmedReason } : {}),
    });
  }

  return (
    <div className="dialog-backdrop" onMouseDown={(event) => {
      if (event.target === event.currentTarget && !busy) onClose();
    }}>
      <div
        aria-busy={busy}
        aria-describedby="limits-zero-help"
        aria-labelledby="limits-title"
        aria-modal="true"
        className="dialog"
        ref={dialogRef}
        role="dialog"
        tabIndex={-1}
      >
        <div className="dialog-header">
          <div>
            <p className="eyebrow">Calendar allowances</p>
            <h2 id="limits-title">Edit quota limits</h2>
          </div>
          <IconButton label="Close dialog" disabled={busy} onClick={onClose}>
            <X aria-hidden="true" size={19} />
          </IconButton>
        </div>
        <div className="dialog-user">
          <div className="user-avatar" aria-hidden="true">{initials(user)}</div>
          <div>
            <strong>{displayName(user)}</strong>
            <span>{user.user_id}</span>
          </div>
        </div>
        <form onSubmit={submit}>
          <p className="field-help" id="limits-zero-help">Calendar windows use UTC. Enter 0 for an Unlimited dimension. Weekly and monthly limits include usage already recorded since the current period began.</p>
          <div className="quota-limit-matrix">
            {QUOTA_PERIODS.map((period, index) => (
              <fieldset className="quota-period-card" key={period}>
                <legend>
                  <label className="quota-period-toggle">
                    <input checked={draft[period].enabled} disabled={busy} onChange={(event) => changed(period, { enabled: event.target.checked })} type="checkbox" />
                    <span>{periodLabel(period)}</span>
                  </label>
                  <small>Resets {formatTimestamp(user.current_usage[period].resets_at)}</small>
                </legend>
                <div className="field-grid">
                  <label><span>{periodLabel(period)} USD limit</span><div className="number-input"><span aria-hidden="true">$</span><input aria-describedby="limits-zero-help" aria-label={`${periodLabel(period)} USD limit`} disabled={busy || !draft[period].enabled} min="0" ref={index === 0 ? firstFieldRef : undefined} step="0.000001" type="number" value={draft[period].usd} onChange={(event) => changed(period, { usd: event.target.value })} /></div></label>
                  <label><span>{periodLabel(period)} input token limit</span><input aria-describedby="limits-zero-help" aria-label={`${periodLabel(period)} input token limit`} disabled={busy || !draft[period].enabled} min="0" step="1" type="number" value={draft[period].input_tokens} onChange={(event) => changed(period, { input_tokens: event.target.value })} /></label>
                  <label><span>{periodLabel(period)} output token limit</span><input aria-describedby="limits-zero-help" aria-label={`${periodLabel(period)} output token limit`} disabled={busy || !draft[period].enabled} min="0" step="1" type="number" value={draft[period].output_tokens} onChange={(event) => changed(period, { output_tokens: event.target.value })} /></label>
                </div>
              </fieldset>
            ))}
          </div>
          {periodChanges.length > 0 && (
            <div className="safety-warning" role="status"><ShieldAlert aria-hidden="true" size={18} /><span>Enabling or disabling {formatList(periodChanges)} changes enforcement immediately. Newly enabled periods include usage accumulated since their UTC boundary.</span></div>
          )}
          {belowUsageFields.length > 0 && (
            <div className="safety-warning" role="status">
              <ShieldAlert aria-hidden="true" size={18} />
              <span>The new finite {formatList(belowUsageFields)} limit is below current-period usage. Additional use may be blocked immediately.</span>
            </div>
          )}
          {unlimitedFields.length > 0 && (
            <div className="safety-warning safety-warning-critical">
              <ShieldAlert aria-hidden="true" size={18} />
              <label>
                <input
                  checked={unlimitedConfirmed}
                  disabled={busy}
                  onChange={(event) => setUnlimitedConfirmed(event.target.checked)}
                  type="checkbox"
                />
                <span>I confirm the {formatList(unlimitedFields)} limit should change from a finite value to Unlimited.</span>
              </label>
            </div>
          )}
          <label className="reason-field">
            <span>Reason {reasonRequired && <strong aria-hidden="true">*</strong>}</span>
            <textarea
              aria-describedby="limits-reason-help"
              aria-required={reasonRequired}
              disabled={busy}
              onChange={(event) => {
                setReason(event.target.value);
                setError("");
              }}
              required={reasonRequired}
              rows={3}
              value={reason}
            />
          </label>
          <p className="field-help" id="limits-reason-help">
            {reasonRequired
              ? "Required for period enable/disable, Unlimited limits, or finite limits below current-period usage. "
              : "Optional for this limit change. "}
            When provided, the trimmed reason is stored with the immutable limit-change audit event.
          </p>
          {error && <ErrorMessage message={error} />}
          {apiError && <ErrorMessage message={apiError} />}
          <div className="dialog-actions">
            <button className="button button-secondary" disabled={busy} onClick={onClose} type="button">
              Cancel
            </button>
            <button
              className="button button-primary"
              disabled={
                busy ||
                (unlimitedFields.length > 0 && !unlimitedConfirmed) ||
                (reasonRequired && !reason.trim())
              }
              type="submit"
            >
              {busy ? (
                <RefreshCw className="spin" aria-hidden="true" size={17} />
              ) : (
                <Check aria-hidden="true" size={17} />
              )}
              {busy ? "Saving" : "Save limits"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

export function StatusDialog({
  apiError,
  busy,
  enforcement,
  onClose,
  onConfirm,
  user,
}: {
  apiError: string;
  busy: boolean;
  enforcement: Summary["enforcement"];
  onClose: () => void;
  onConfirm: (reason: string) => void;
  user: UserRow;
}) {
  const [reason, setReason] = useState("");
  const dialogRef = useRef<HTMLDivElement>(null);
  const reasonRef = useRef<HTMLTextAreaElement>(null);
  const nextStatus: UserStatus = user.status === "active" ? "blocked" : "active";
  const blocking = nextStatus === "blocked";
  useModalLifecycle(busy, onClose, dialogRef, reasonRef);

  function submit(event: React.FormEvent) {
    event.preventDefault();
    const trimmed = reason.trim();
    if (!trimmed) return;
    onConfirm(trimmed);
  }

  return (
    <div className="dialog-backdrop" onMouseDown={(event) => {
      if (event.target === event.currentTarget && !busy) onClose();
    }}>
      <div
        aria-busy={busy}
        aria-describedby="status-enforcement-message"
        aria-labelledby="status-title"
        aria-modal="true"
        className="dialog status-dialog"
        ref={dialogRef}
        role="dialog"
        tabIndex={-1}
      >
        <div className="dialog-header">
          <div>
            <p className="eyebrow">Access status</p>
            <h2 id="status-title">Confirm {blocking ? "block" : "unblock"}</h2>
          </div>
          <IconButton label="Close dialog" disabled={busy} onClick={onClose}>
            <X aria-hidden="true" size={19} />
          </IconButton>
        </div>
        <div className="dialog-user">
          <div className="user-avatar" aria-hidden="true">{initials(user)}</div>
          <div>
            <strong>{displayName(user)}</strong>
            <span>{user.user_id}</span>
          </div>
        </div>
        <form onSubmit={submit}>
          <div className="status-transition" aria-label={`Status changes from ${user.status} to ${nextStatus}`}>
            <div><span>Current status</span><strong>{user.status}</strong></div>
            <span aria-hidden="true">→</span>
            <div><span>Next status</span><strong>{nextStatus}</strong></div>
          </div>
          <dl className="usage-snapshot" aria-label="Current usage and limits">
            {QUOTA_PERIODS.map((period) => {
              const limits = user.limits[period];
              const usage = user.current_usage[period];
              return <div key={period}><dt>{periodLabel(period)}</dt><dd>{limits ? `${formatUsd(usage.cost_usd, 4)} / ${formatLimit(limits.usd, (value) => formatUsd(value, 4))} · ${formatCompact(usage.input_tokens)} / ${formatLimit(limits.input_tokens, formatCompact)} input · ${formatCompact(usage.output_tokens)} / ${formatLimit(limits.output_tokens, formatCompact)} output` : "Disabled"}</dd></div>;
            })}
          </dl>
          <div className={`enforcement-warning${blocking ? " enforcement-warning-destructive" : ""}`} id="status-enforcement-message">
            <ShieldAlert aria-hidden="true" size={18} />
            <div>
              <strong>{formatOperationalLabel(enforcement.mode)} mode</strong>
              <p>{statusEnforcementMessage(enforcement, nextStatus)}</p>
            </div>
          </div>
          <label className="reason-field">
            <span>Reason <strong aria-hidden="true">*</strong></span>
            <textarea
              aria-describedby="status-reason-help"
              disabled={busy}
              onChange={(event) => setReason(event.target.value)}
              ref={reasonRef}
              required
              rows={3}
              value={reason}
            />
          </label>
          <p className="field-help" id="status-reason-help">Required. This trimmed reason is stored in the administrative audit trail.</p>
          {apiError && <ErrorMessage message={apiError} />}
          <div className="dialog-actions">
            <button className="button button-secondary" disabled={busy} onClick={onClose} type="button">
              Cancel
            </button>
            <button
              className={`button ${blocking ? "button-danger" : "button-primary"}`}
              disabled={busy || !reason.trim()}
              type="submit"
            >
              {busy ? <RefreshCw className="spin" aria-hidden="true" size={17} /> : blocking ? <Lock aria-hidden="true" size={17} /> : <Unlock aria-hidden="true" size={17} />}
              {busy ? "Saving" : blocking ? "Block user" : "Unblock user"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

export function statusEnforcementMessage(
  enforcement: Summary["enforcement"],
  nextStatus: UserStatus,
): string {
  if (nextStatus === "active") {
    return "Unblocking allows new credentials to be issued. All configured calendar limits continue to apply.";
  }
  const window = formatDuration(enforcement.post_detection_fallback_seconds);
  return `Blocking prevents new credentials from being issued and requests active-session revocation. Existing permissions expire with their lease (up to ${window} after detection); revocation usually cuts them earlier, so bounded overspend is limited to whichever ends first.`;
}

function formatLimit(limit: number, format: (value: number) => string): string {
  return limit === 0 ? "Unlimited" : format(limit);
}

function formatList(values: string[]): string {
  if (values.length < 2) return values[0] ?? "";
  if (values.length === 2) return `${values[0]} and ${values[1]}`;
  return `${values.slice(0, -1).join(", ")}, and ${values[values.length - 1]}`;
}

function Brand() {
  return (
    <div className="brand">
      <div className="brand-mark"><Layers3 aria-hidden="true" size={22} /></div>
      <div>
        <strong>Bedrock Spend Controls</strong>
        <span>Admin console</span>
      </div>
    </div>
  );
}

function SystemDetail({ label, value }: { label: string; value: string }) {
  return (
    <div className="system-detail">
      <span>{label}</span>
      <strong title={value}>{value}</strong>
    </div>
  );
}

function IconButton({
  children,
  danger = false,
  disabled = false,
  label,
  onClick,
}: {
  children: React.ReactNode;
  danger?: boolean;
  disabled?: boolean;
  label: string;
  onClick: () => void;
}) {
  return (
    <button
      aria-label={label}
      className={`icon-button${danger ? " icon-button-danger" : ""}`}
      disabled={disabled}
      onClick={onClick}
      title={label}
      type="button"
    >
      {children}
    </button>
  );
}

function ErrorMessage({ message, dismiss }: { message: string; dismiss?: () => void }) {
  return (
    <div className="message message-error" role="alert">
      <AlertCircle aria-hidden="true" size={18} />
      <span>{message}</span>
      {dismiss && (
        <button aria-label="Dismiss error" onClick={dismiss} title="Dismiss" type="button">
          <X aria-hidden="true" size={16} />
        </button>
      )}
    </div>
  );
}

function SuccessMessage({ message, dismiss }: { message: string; dismiss: () => void }) {
  return (
    <div className="message message-success" role="status">
      <Check aria-hidden="true" size={18} />
      <span>{message}</span>
      <button aria-label="Dismiss notification" onClick={dismiss} title="Dismiss" type="button">
        <X aria-hidden="true" size={16} />
      </button>
    </div>
  );
}

function LoadingState({ label }: { label: string }) {
  return (
    <div className="loading-state">
      <RefreshCw className="spin" aria-hidden="true" size={24} />
      <span>{label}</span>
    </div>
  );
}

function SummarySkeleton() {
  return (
    <section className="metric-grid" aria-label="Loading quota summary">
      {[0, 1, 2, 3].map((item) => (
        <div className="metric-card skeleton-card" key={item}>
          <span className="skeleton skeleton-square" />
          <div>
            <span className="skeleton skeleton-line" />
            <span className="skeleton skeleton-value" />
          </div>
        </div>
      ))}
    </section>
  );
}

function TableSkeleton() {
  return (
    <>
      {[0, 1, 2].map((row) => (
        <tr className="table-skeleton" key={row}>
          <td><span className="skeleton skeleton-row-wide" /></td>
          <td><span className="skeleton skeleton-row-small" /></td>
          <td><span className="skeleton skeleton-row-medium" /></td>
          <td><span className="skeleton skeleton-row-medium" /></td>
          <td><span className="skeleton skeleton-row-medium" /></td>
          <td><span className="skeleton skeleton-row-small" /></td>
          <td />
        </tr>
      ))}
    </>
  );
}

function UnavailableState({ label }: { label: string }) {
  return (
    <section className="unavailable-state" aria-label={label}>
      <AlertCircle aria-hidden="true" size={22} />
      <span>{label}</span>
    </section>
  );
}

function EmptyState({ hasFilters }: { hasFilters: boolean }) {
  return (
    <div className="empty-state">
      <Users aria-hidden="true" size={24} />
      <strong>{hasFilters ? "No matching users" : "No users yet"}</strong>
      <span>{hasFilters ? "Try a different search or status." : "Users appear after they request access."}</span>
    </div>
  );
}

function Centered({ children }: { children: React.ReactNode }) {
  return <main className="centered">{children}</main>;
}

function displayName(user: UserRow): string {
  return user.name || "Unnamed identity";
}

function initials(user: UserRow): string {
  const source = displayName(user).trim();
  const parts = source.split(/\s+/).filter(Boolean);
  if (parts.length > 1) return `${parts[0][0]}${parts[1][0]}`.toUpperCase();
  return source.slice(0, 2).toUpperCase();
}

function highestUtilization(user: UserRow): { period: QuotaPeriod; percent: number; level: string } | null {
  let highest: { period: QuotaPeriod; ratio: number } | null = null;
  for (const period of QUOTA_PERIODS) {
    const limits = user.limits[period];
    if (!limits) continue;
    const usage = user.current_usage[period];
    const ratios = [
      limits.usd > 0 ? usage.cost_usd / limits.usd : 0,
      limits.input_tokens > 0 ? usage.input_tokens / limits.input_tokens : 0,
      limits.output_tokens > 0 ? usage.output_tokens / limits.output_tokens : 0,
    ];
    const ratio = Math.max(...ratios);
    if (highest === null || ratio > highest.ratio) highest = { period, ratio };
  }
  if (highest === null) return null;
  return {
    period: highest.period,
    percent: Math.round(highest.ratio * 100),
    level: highest.ratio >= 1 ? "critical" : highest.ratio >= 0.8 ? "warning" : "normal",
  };
}

function periodLabel(period: QuotaPeriod): string {
  return period[0].toUpperCase() + period.slice(1);
}

function formatTimestamp(value: string): string {
  const timestamp = new Date(value);
  return Number.isNaN(timestamp.getTime()) ? "Invalid timestamp" : timestamp.toLocaleString();
}

function formatUsd(value: number, digits = 2): string {
  return new Intl.NumberFormat(undefined, {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(value);
}

function formatCompact(value: number): string {
  if (Math.abs(value) < 1000) return value.toLocaleString();
  return new Intl.NumberFormat(undefined, {
    notation: "compact",
    maximumFractionDigits: 1,
  }).format(value);
}
