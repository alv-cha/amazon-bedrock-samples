import { useEffect, useState } from "react";
import { loadConfig, type AdminConfig } from "./config";
import { signIn, type Session } from "./auth";
import { api, type Summary, type UserRow } from "./api";

export function App() {
  const [cfg, setCfg] = useState<AdminConfig | null>(null);
  const [cfgError, setCfgError] = useState<string>("");
  const [session, setSession] = useState<Session | null>(null);

  useEffect(() => {
    try {
      setCfg(loadConfig());
    } catch (e) {
      setCfgError((e as Error).message);
    }
  }, []);

  if (cfgError) return <Centered><Error msg={cfgError} /></Centered>;
  if (!cfg) return <Centered>Loading…</Centered>;
  if (!session) return <Login cfg={cfg} onSignIn={setSession} />;
  return <Dashboard cfg={cfg} session={session} onSignOut={() => setSession(null)} />;
}

function Login({ cfg, onSignIn }: { cfg: AdminConfig; onSignIn: (s: Session) => void }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setErr("");
    try {
      onSignIn(await signIn(cfg, email, password));
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Centered>
      <form onSubmit={submit} style={{ display: "grid", gap: 10, width: 320 }}>
        <h2>Bedrock Quota Admin</h2>
        <input placeholder="email" value={email} onChange={(e) => setEmail(e.target.value)} />
        <input placeholder="password" type="password" value={password}
               onChange={(e) => setPassword(e.target.value)} />
        <button disabled={busy || !email || !password}>{busy ? "Signing in…" : "Sign in"}</button>
        {err && <Error msg={err} />}
        <small style={{ color: "#666" }}>
          Sign in with your corporate account. Admin access is granted by your
          IdP group claim — no shared secret is used or stored in the browser.
        </small>
      </form>
    </Centered>
  );
}

function Dashboard({ cfg, session, onSignOut }: {
  cfg: AdminConfig; session: Session; onSignOut: () => void;
}) {
  const [summary, setSummary] = useState<Summary | null>(null);
  const [users, setUsers] = useState<UserRow[]>([]);
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(true);

  async function refresh() {
    setLoading(true);
    setErr("");
    try {
      const [s, u] = await Promise.all([api.summary(cfg, session), api.listUsers(cfg, session)]);
      setSummary(s);
      setUsers(u);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { void refresh(); }, []);

  return (
    <div style={{ maxWidth: 1100, margin: "20px auto", fontFamily: "system-ui, sans-serif" }}>
      <header style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <h2>Bedrock Quota Admin</h2>
        <div>
          <span style={{ color: "#666", marginRight: 12 }}>{session.email}</span>
          <button onClick={() => void refresh()} disabled={loading}>Refresh</button>
          <button onClick={onSignOut} style={{ marginLeft: 8 }}>Sign out</button>
        </div>
      </header>
      {err && <Error msg={err} />}
      {summary && <SummaryCards summary={summary} />}
      <UsersTable cfg={cfg} session={session} users={users} onChanged={refresh} />
    </div>
  );
}

function SummaryCards({ summary }: { summary: Summary }) {
  const e = summary.enforcement;
  return (
    <section style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16, margin: "16px 0" }}>
      <Card title="Enforcement (authoritative)">
        <p style={{ color: "#666", marginTop: 0 }}>
          Source: DynamoDB · as of {new Date(e.as_of).toLocaleString()} · window {e.window}
        </p>
        <Kv k="Users" v={e.total_users} />
        <Kv k="Blocked" v={`${e.blocked_users}${e.blocked_user_ids.length ? " (" + e.blocked_user_ids.join(", ") + ")" : ""}`} />
        <Kv k="Today spend" v={`$${e.today.cost_usd.toFixed(4)}`} />
        <Kv k="Today requests" v={e.today.requests} />
      </Card>
      <Card title="Observability (history)">
        <p style={{ color: "#666", marginTop: 0 }}>Source: {summary.observability.source}</p>
        <Kv k="Metrics namespace" v={summary.observability.metrics_namespace} />
        <Kv k="Delivery" v={summary.observability.delivery} />
        <Kv k="Credential TTL" v={`${e.credential_ttl_seconds / 60} min`} />
      </Card>
      <div style={{ gridColumn: "1 / -1", fontSize: 13, color: "#555" }}>
        <strong>Enforcement:</strong> bounded overspend. A blocked identity
        cannot refresh credentials; credentials already issued remain valid
        until their STS expiration.
      </div>
    </section>
  );
}

function UsersTable({ cfg, session, users, onChanged }: {
  cfg: AdminConfig; session: Session; users: UserRow[]; onChanged: () => Promise<void>;
}) {
  const [busy, setBusy] = useState<string>("");

  function promptNumber(label: string, current: number): number | null {
    const raw = prompt(label, String(current));
    if (raw === null) return null;
    const value = Number(raw);
    if (!Number.isFinite(value) || value < 0) {
      alert("Enter a non-negative number.");
      return null;
    }
    return value;
  }

  async function editLimits(user: UserRow) {
    const dailyUsd = promptNumber(`Daily USD for ${user.user_id}`, user.limits.daily_usd);
    if (dailyUsd === null) return;
    const dailyInput = promptNumber(
      `Daily input tokens for ${user.user_id}`,
      user.limits.daily_input_tokens,
    );
    if (dailyInput === null) return;
    const dailyOutput = promptNumber(
      `Daily output tokens for ${user.user_id}`,
      user.limits.daily_output_tokens,
    );
    if (dailyOutput === null) return;
    await act("limits", () => api.setLimits(cfg, session, user.user_id, {
      daily_usd: dailyUsd,
      daily_input_tokens: Math.trunc(dailyInput),
      daily_output_tokens: Math.trunc(dailyOutput),
    }));
  }

  async function act(label: string, fn: () => Promise<unknown>) {
    setBusy(label);
    try {
      await fn();
      await onChanged();
    } catch (e) {
      alert((e as Error).message);
    } finally {
      setBusy("");
    }
  }

  return (
    <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 14 }}>
      <thead>
        <tr style={{ textAlign: "left", borderBottom: "2px solid #ddd" }}>
          <th>User</th><th>Status</th><th>Daily $</th><th>Today $</th>
          <th>Daily input</th><th>Daily output</th><th>Today tokens</th><th>Actions</th>
        </tr>
      </thead>
      <tbody>
        {users.map((u) => (
          <tr key={u.user_id} style={{ borderBottom: "1px solid #eee" }}>
            <td>{u.user_id}<br /><small style={{ color: "#888" }}>{u.name}</small></td>
            <td style={{ color: u.status === "active" ? "#2a7" : "#c33" }}>{u.status}</td>
            <td>{u.limits.daily_usd}</td>
            <td>${u.today.cost_usd.toFixed(4)}</td>
            <td>{u.limits.daily_input_tokens.toLocaleString()}</td>
            <td>{u.limits.daily_output_tokens.toLocaleString()}</td>
            <td>{u.today.input_tokens.toLocaleString()} / {u.today.output_tokens.toLocaleString()}</td>
            <td style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
              <button disabled={!!busy} onClick={() => void editLimits(u)}>Edit limits</button>
              <button disabled={!!busy} onClick={() =>
                void act("status", () => api.setStatus(cfg, session, u.user_id,
                  u.status === "active" ? "blocked" : "active"))
              }>{u.status === "active" ? "Block" : "Unblock"}</button>
            </td>
          </tr>
        ))}
        {users.length === 0 && <tr><td colSpan={8} style={{ color: "#999", padding: 16 }}>No users yet.</td></tr>}
      </tbody>
    </table>
  );
}

const Card = ({ title, children }: { title: string; children: React.ReactNode }) => (
  <div style={{ border: "1px solid #ddd", borderRadius: 8, padding: 16 }}>
    <h3 style={{ marginTop: 0 }}>{title}</h3>{children}
  </div>
);
const Kv = ({ k, v }: { k: string; v: React.ReactNode }) => (
  <div style={{ display: "flex", justifyContent: "space-between", padding: "2px 0" }}>
    <span style={{ color: "#666" }}>{k}</span><strong>{v}</strong>
  </div>
);
const Error = ({ msg }: { msg: string }) => (
  <div style={{ background: "#fdd", border: "1px solid #c33", borderRadius: 6, padding: "8px 12px", color: "#900" }}>{msg}</div>
);
const Centered = ({ children }: { children: React.ReactNode }) => (
  <div style={{ display: "grid", placeItems: "center", minHeight: "80vh", fontFamily: "system-ui, sans-serif" }}>{children}</div>
);
