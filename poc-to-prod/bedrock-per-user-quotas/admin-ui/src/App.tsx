import { useEffect, useMemo, useState } from "react";
import {
  AlertCircle,
  Check,
  ChevronDown,
  CircleDollarSign,
  Gauge,
  KeyRound,
  Layers3,
  Lock,
  LogOut,
  Pencil,
  RefreshCw,
  Search,
  ShieldCheck,
  Unlock,
  UserRound,
  Users,
  X,
} from "lucide-react";
import { loadConfig, type AdminConfig } from "./config";
import { signIn, type Session } from "./auth";
import { api, type Summary, type UserRow } from "./api";

type UserFilter = "all" | "active" | "blocked";

export function App() {
  const [cfg, setCfg] = useState<AdminConfig | null>(null);
  const [cfgError, setCfgError] = useState("");
  const [session, setSession] = useState<Session | null>(null);

  useEffect(() => {
    try {
      setCfg(loadConfig());
    } catch (error) {
      setCfgError((error as Error).message);
    }
  }, []);

  if (cfgError) {
    return (
      <Centered>
        <ErrorMessage message={cfgError} />
      </Centered>
    );
  }
  if (!cfg) {
    return (
      <Centered>
        <LoadingState label="Loading console" />
      </Centered>
    );
  }
  if (!session) return <Login cfg={cfg} onSignIn={setSession} />;
  return <Dashboard cfg={cfg} session={session} onSignOut={() => setSession(null)} />;
}

function Login({ cfg, onSignIn }: { cfg: AdminConfig; onSignIn: (session: Session) => void }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      onSignIn(await signIn(cfg, email, password));
    } catch (caught) {
      setError((caught as Error).message);
    } finally {
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
          <p>Use your organization account to manage Bedrock access and daily limits.</p>
        </div>

        <form className="login-form" onSubmit={submit}>
          <label>
            <span>Email</span>
            <div className="input-with-icon">
              <UserRound aria-hidden="true" size={18} />
              <input
                autoComplete="username"
                autoFocus
                placeholder="you@example.com"
                type="email"
                value={email}
                onChange={(event) => setEmail(event.target.value)}
              />
            </div>
          </label>
          <label>
            <span>Password</span>
            <div className="input-with-icon">
              <KeyRound aria-hidden="true" size={18} />
              <input
                autoComplete="current-password"
                placeholder="Enter your password"
                type="password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
              />
            </div>
          </label>
          {error && <ErrorMessage message={error} />}
          <button className="button button-primary login-submit" disabled={busy || !email || !password}>
            {busy && <RefreshCw className="spin" aria-hidden="true" size={17} />}
            {busy ? "Signing in" : "Sign in"}
          </button>
        </form>

        <div className="login-security">
          <ShieldCheck aria-hidden="true" size={17} />
          <span>Access is authorized by your identity provider group.</span>
        </div>
      </section>
    </main>
  );
}

function Dashboard({
  cfg,
  session,
  onSignOut,
}: {
  cfg: AdminConfig;
  session: Session;
  onSignOut: () => void;
}) {
  const [summary, setSummary] = useState<Summary | null>(null);
  const [users, setUsers] = useState<UserRow[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  async function refresh() {
    setLoading(true);
    setError("");
    try {
      const [nextSummary, nextUsers] = await Promise.all([
        api.summary(cfg, session),
        api.listUsers(cfg, session),
      ]);
      setSummary(nextSummary);
      setUsers(nextUsers);
    } catch (caught) {
      setError((caught as Error).message);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void refresh();
  }, []);

  return (
    <div className="app-shell">
      <header className="app-header">
        <div className="header-inner">
          <Brand />
          <div className="header-account">
            <div className="account-copy">
              <span>{session.email}</span>
              <small>Administrator</small>
            </div>
            <IconButton
              label="Refresh data"
              disabled={loading}
              onClick={() => void refresh()}
            >
              <RefreshCw className={loading ? "spin" : ""} aria-hidden="true" size={18} />
            </IconButton>
            <IconButton label="Sign out" onClick={onSignOut}>
              <LogOut aria-hidden="true" size={18} />
            </IconButton>
          </div>
        </div>
      </header>

      <main className="dashboard">
        <div className="page-heading">
          <div>
            <p className="eyebrow">Amazon Bedrock</p>
            <h1>Quota overview</h1>
          </div>
          {summary && (
            <p className="updated-at">
              Updated {new Date(summary.enforcement.as_of).toLocaleString()}
            </p>
          )}
        </div>

        {error && <ErrorMessage message={error} />}
        {summary ? <SummaryPanel summary={summary} /> : <SummarySkeleton />}

        <UsersPanel
          cfg={cfg}
          loading={loading}
          onChanged={refresh}
          session={session}
          users={users}
        />
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
            <span>Bounded overspend</span>
          </div>
        </div>
        <SystemDetail label="Source" value={enforcement.source} />
        <SystemDetail label="Window" value={enforcement.window} />
        <SystemDetail
          label="Credential TTL"
          value={`${Math.round(enforcement.credential_ttl_seconds / 60)} min`}
        />
        <SystemDetail
          label="Telemetry"
          value={`${summary.observability.source} · ${summary.observability.delivery}`}
        />
        <SystemDetail label="Metrics" value={summary.observability.metrics_namespace} />
      </section>
    </>
  );
}

function UsersPanel({
  cfg,
  loading,
  onChanged,
  session,
  users,
}: {
  cfg: AdminConfig;
  loading: boolean;
  onChanged: () => Promise<void>;
  session: Session;
  users: UserRow[];
}) {
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<UserFilter>("all");
  const [editing, setEditing] = useState<UserRow | null>(null);
  const [busy, setBusy] = useState("");
  const [actionError, setActionError] = useState("");
  const [notice, setNotice] = useState("");

  const visibleUsers = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return users.filter((user) => {
      const matchesFilter = filter === "all" || user.status === filter;
      const matchesQuery =
        !needle ||
        user.user_id.toLowerCase().includes(needle) ||
        user.name.toLowerCase().includes(needle);
      return matchesFilter && matchesQuery;
    });
  }, [filter, query, users]);

  async function act(
    label: string,
    successMessage: string,
    fn: () => Promise<unknown>,
  ): Promise<boolean> {
    setBusy(label);
    setActionError("");
    setNotice("");
    try {
      await fn();
      await onChanged();
      setNotice(successMessage);
      return true;
    } catch (caught) {
      setActionError((caught as Error).message);
      return false;
    } finally {
      setBusy("");
    }
  }

  async function saveLimits(
    user: UserRow,
    limits: { daily_usd: number; daily_input_tokens: number; daily_output_tokens: number },
  ) {
    const saved = await act(
      `limits:${user.user_id}`,
      `Limits updated for ${displayName(user)}.`,
      () => api.setLimits(cfg, session, user.user_id, limits),
    );
    if (saved) setEditing(null);
  }

  async function toggleStatus(user: UserRow) {
    const nextStatus = user.status === "active" ? "blocked" : "active";
    await act(
      `status:${user.user_id}`,
      `${displayName(user)} is now ${nextStatus}.`,
      () => api.setStatus(cfg, session, user.user_id, nextStatus),
    );
  }

  return (
    <section className="users-panel" aria-labelledby="users-title">
      <div className="panel-heading">
        <div>
          <h2 id="users-title">Users</h2>
          <p>{users.length} identities with configured quota access</p>
        </div>
        <div className="user-tools">
          <div className="search-field">
            <Search aria-hidden="true" size={17} />
            <input
              aria-label="Search users"
              placeholder="Search users"
              type="search"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
            />
          </div>
          <div className="select-wrap">
            <select
              aria-label="Filter users by status"
              value={filter}
              onChange={(event) => setFilter(event.target.value as UserFilter)}
            >
              <option value="all">All statuses</option>
              <option value="active">Active</option>
              <option value="blocked">Blocked</option>
            </select>
            <ChevronDown aria-hidden="true" size={16} />
          </div>
        </div>
      </div>

      {actionError && <ErrorMessage message={actionError} dismiss={() => setActionError("")} />}
      {notice && <SuccessMessage message={notice} dismiss={() => setNotice("")} />}

      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>User</th>
              <th>Status</th>
              <th>USD usage</th>
              <th>Input tokens</th>
              <th>Output tokens</th>
              <th>Requests</th>
              <th><span className="sr-only">Actions</span></th>
            </tr>
          </thead>
          <tbody>
            {loading && users.length === 0 ? (
              <TableSkeleton />
            ) : (
              visibleUsers.map((user) => (
                <UserTableRow
                  busy={busy}
                  key={user.user_id}
                  onEdit={() => {
                    setActionError("");
                    setNotice("");
                    setEditing(user);
                  }}
                  onToggleStatus={() => void toggleStatus(user)}
                  user={user}
                />
              ))
            )}
          </tbody>
        </table>
        {!loading && visibleUsers.length === 0 && (
          <EmptyState hasFilters={Boolean(query) || filter !== "all"} />
        )}
      </div>

      {editing && (
        <LimitsDialog
          apiError={actionError}
          busy={busy === `limits:${editing.user_id}`}
          onClose={() => setEditing(null)}
          onSave={(limits) => void saveLimits(editing, limits)}
          user={editing}
        />
      )}
    </section>
  );
}

function UserTableRow({
  busy,
  onEdit,
  onToggleStatus,
  user,
}: {
  busy: string;
  onEdit: () => void;
  onToggleStatus: () => void;
  user: UserRow;
}) {
  const isActive = user.status === "active";
  const isBusy = busy.endsWith(`:${user.user_id}`);

  return (
    <tr>
      <td>
        <div className="user-cell">
          <div className="user-avatar" aria-hidden="true">{initials(user)}</div>
          <div>
            <strong title={user.name}>{displayName(user)}</strong>
            <span title={user.user_id}>{user.user_id}</span>
          </div>
        </div>
      </td>
      <td>
        <span className={`status-badge status-${isActive ? "active" : "blocked"}`}>
          <span aria-hidden="true" />
          {user.status}
        </span>
      </td>
      <td>
        <QuotaUsage
          current={user.today.cost_usd}
          format={(value) => formatUsd(value, 4)}
          limit={user.limits.daily_usd}
        />
      </td>
      <td>
        <QuotaUsage
          current={user.today.input_tokens}
          format={formatCompact}
          limit={user.limits.daily_input_tokens}
        />
      </td>
      <td>
        <QuotaUsage
          current={user.today.output_tokens}
          format={formatCompact}
          limit={user.limits.daily_output_tokens}
        />
      </td>
      <td className="request-count">{user.today.requests.toLocaleString()}</td>
      <td>
        <div className="row-actions">
          <IconButton label={`Edit limits for ${displayName(user)}`} onClick={onEdit}>
            <Pencil aria-hidden="true" size={17} />
          </IconButton>
          <IconButton
            danger={isActive}
            disabled={isBusy}
            label={`${isActive ? "Block" : "Unblock"} ${displayName(user)}`}
            onClick={onToggleStatus}
          >
            {isBusy ? (
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

function QuotaUsage({
  current,
  format,
  limit,
}: {
  current: number;
  format: (value: number) => string;
  limit: number;
}) {
  const percentage = limit > 0 ? (current / limit) * 100 : current > 0 ? 100 : 0;
  const level = percentage >= 100 ? "critical" : percentage >= 80 ? "warning" : "normal";

  return (
    <div className="quota-usage">
      <div>
        <strong>{format(current)}</strong>
        <span>of {format(limit)}</span>
      </div>
      <div
        aria-label={`${Math.round(percentage)} percent used`}
        aria-valuemax={100}
        aria-valuemin={0}
        aria-valuenow={Math.min(Math.round(percentage), 100)}
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

function LimitsDialog({
  apiError,
  busy,
  onClose,
  onSave,
  user,
}: {
  apiError: string;
  busy: boolean;
  onClose: () => void;
  onSave: (limits: {
    daily_usd: number;
    daily_input_tokens: number;
    daily_output_tokens: number;
  }) => void;
  user: UserRow;
}) {
  const [dailyUsd, setDailyUsd] = useState(String(user.limits.daily_usd));
  const [dailyInput, setDailyInput] = useState(String(user.limits.daily_input_tokens));
  const [dailyOutput, setDailyOutput] = useState(String(user.limits.daily_output_tokens));
  const [error, setError] = useState("");

  useEffect(() => {
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape" && !busy) onClose();
    }
    window.addEventListener("keydown", onKeyDown);
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [busy, onClose]);

  function submit(event: React.FormEvent) {
    event.preventDefault();
    const usd = Number(dailyUsd);
    const input = Number(dailyInput);
    const output = Number(dailyOutput);
    if (
      !Number.isFinite(usd) ||
      usd < 0 ||
      !Number.isInteger(input) ||
      input < 0 ||
      !Number.isInteger(output) ||
      output < 0
    ) {
      setError("Enter a non-negative USD amount and whole token values.");
      return;
    }
    onSave({
      daily_usd: usd,
      daily_input_tokens: input,
      daily_output_tokens: output,
    });
  }

  return (
    <div className="dialog-backdrop" onMouseDown={(event) => {
      if (event.target === event.currentTarget && !busy) onClose();
    }}>
      <div aria-labelledby="limits-title" aria-modal="true" className="dialog" role="dialog">
        <div className="dialog-header">
          <div>
            <p className="eyebrow">Daily allowance</p>
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
          <div className="field-grid">
            <label>
              <span>USD limit</span>
              <div className="number-input">
                <span aria-hidden="true">$</span>
                <input
                  autoFocus
                  min="0"
                  step="0.01"
                  type="number"
                  value={dailyUsd}
                  onChange={(event) => setDailyUsd(event.target.value)}
                />
              </div>
            </label>
            <label>
              <span>Input token limit</span>
              <input
                min="0"
                step="1"
                type="number"
                value={dailyInput}
                onChange={(event) => setDailyInput(event.target.value)}
              />
            </label>
            <label>
              <span>Output token limit</span>
              <input
                min="0"
                step="1"
                type="number"
                value={dailyOutput}
                onChange={(event) => setDailyOutput(event.target.value)}
              />
            </label>
          </div>
          {error && <ErrorMessage message={error} />}
          {apiError && <ErrorMessage message={apiError} />}
          <div className="dialog-actions">
            <button className="button button-secondary" disabled={busy} onClick={onClose} type="button">
              Cancel
            </button>
            <button className="button button-primary" disabled={busy} type="submit">
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

function Brand() {
  return (
    <div className="brand">
      <div className="brand-mark"><Layers3 aria-hidden="true" size={22} /></div>
      <div>
        <strong>Bedrock Quotas</strong>
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
