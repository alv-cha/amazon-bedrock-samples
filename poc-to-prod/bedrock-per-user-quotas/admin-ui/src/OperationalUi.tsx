import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent as ReactKeyboardEvent } from "react";
import {
  AlertCircle,
  ArrowLeft,
  ArrowRight,
  CalendarDays,
  ChevronRight,
  Pencil,
  RefreshCw,
  ShieldAlert,
  Unlock,
  Lock,
  UserPlus,
  X,
} from "lucide-react";
import type { AdminConfig } from "./config";
import type { Session } from "./auth";
import {
  ApiError,
  api,
  apiErrorMessage,
  normalizeDailyUsd,
  type AdminUser,
  type AuditEvent,
  type AuditUserSnapshot,
  type CreateUserRequest,
  type QuotaLimits,
  type UsageHistoryResponse,
  type UserAuditListResponse,
  type UserRow,
} from "./api";
import { useModalLifecycle } from "./modal";

const PAGE_SIZE = 25;
const RESERVED_PREFIXES = ["SESSION#", "VEND#", "REVOCATION#", "CONFIG#", "EMERGENCY_AUDIT#"];

type DrawerTab = "overview" | "usage" | "changes";

function ErrorMessage({ message }: { message: string }) {
  return <div className="message message-error" role="alert"><AlertCircle aria-hidden="true" size={18} /><span>{message}</span></div>;
}

function BusyLabel({ children }: { children: string }) {
  return <span className="inline-busy"><RefreshCw className="spin" aria-hidden="true" size={16} />{children}</span>;
}

function formatUsd(value: number, digits = 2): string {
  return new Intl.NumberFormat(undefined, {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(value);
}

function formatTimestamp(value: string | null): string {
  if (!value) return "Not available";
  const timestamp = new Date(value);
  return Number.isNaN(timestamp.getTime()) ? "Invalid timestamp" : timestamp.toLocaleString();
}

function formatLimit(value: number, formatter: (item: number) => string): string {
  return value === 0 ? "Unlimited" : formatter(value);
}

function parseLimits(usd: string, input: string, output: string): QuotaLimits | null {
  const parsedUsd = usd.trim() === "" ? Number.NaN : Number(usd);
  const parsedInput = input.trim() === "" ? Number.NaN : Number(input);
  const parsedOutput = output.trim() === "" ? Number.NaN : Number(output);
  if (!Number.isFinite(parsedUsd) || parsedUsd < 0 ||
      !Number.isInteger(parsedInput) || parsedInput < 0 ||
      !Number.isInteger(parsedOutput) || parsedOutput < 0) return null;
  return {
    daily_usd: normalizeDailyUsd(parsedUsd),
    daily_input_tokens: parsedInput,
    daily_output_tokens: parsedOutput,
  };
}

export function CreateUserWizard({
  cfg,
  session,
  onClose,
  onCreated,
}: {
  cfg: AdminConfig;
  session: Session;
  onClose: () => void;
  onCreated: (user: AdminUser, openDetails: boolean) => void;
}) {
  const [step, setStep] = useState(0);
  const [userId, setUserId] = useState("");
  const [name, setName] = useState("");
  const [dailyUsd, setDailyUsd] = useState("1");
  const [dailyInput, setDailyInput] = useState("1000000");
  const [dailyOutput, setDailyOutput] = useState("200000");
  const [unlimitedConfirmed, setUnlimitedConfirmed] = useState(false);
  const [openDetails, setOpenDetails] = useState(true);
  const [errors, setErrors] = useState<string[]>([]);
  const [requestError, setRequestError] = useState("");
  const [busy, setBusy] = useState(false);
  const dialogRef = useRef<HTMLDivElement>(null);
  const userIdRef = useRef<HTMLInputElement>(null);
  const stepHeadingRef = useRef<HTMLHeadingElement>(null);
  useModalLifecycle(busy, onClose, dialogRef, userIdRef);

  const limits = parseLimits(dailyUsd, dailyInput, dailyOutput);
  const unlimited = limits !== null && Object.values(limits).some((value) => value === 0);
  const steps = ["Identity", "Limits", "Review"];

  useEffect(() => {
    if (step > 0) stepHeadingRef.current?.focus();
  }, [step]);

  function identityErrors(): string[] {
    const next: string[] = [];
    const identity = userId.trim();
    if (!identity) next.push("Enter the immutable user identity claim value.");
    if (RESERVED_PREFIXES.some((prefix) => identity.startsWith(prefix))) {
      next.push("The user identity uses a reserved internal prefix.");
    }
    if (!name.trim()) next.push("Enter a display name.");
    return next;
  }

  function limitErrors(): string[] {
    if (!limits) return ["Enter a non-negative USD amount and whole token values. Fields cannot be blank."];
    if (unlimited && !unlimitedConfirmed) return ["Confirm that every zero limit should be Unlimited."];
    return [];
  }

  function next() {
    const nextErrors = step === 0 ? identityErrors() : limitErrors();
    setErrors(nextErrors);
    setRequestError("");
    if (nextErrors.length === 0) setStep((current) => Math.min(current + 1, 2));
  }

  async function create() {
    const nextErrors = [...identityErrors(), ...limitErrors()];
    setErrors(nextErrors);
    setRequestError("");
    if (nextErrors.length > 0 || !limits) return;
    const request: CreateUserRequest = {
      user_id: userId.trim(),
      name: name.trim(),
      ...limits,
    };
    setBusy(true);
    try {
      const result = await api.createUser(cfg, session, request);
      onCreated(result.data.user, openDetails);
      onClose();
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 409 && caught.code === "user_already_exists") {
        setRequestError(`A user with identity “${request.user_id}” already exists. Close this wizard and search for the existing user instead.`);
      } else {
        setRequestError(apiErrorMessage(caught));
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="dialog-backdrop" onMouseDown={(event) => {
      if (event.target === event.currentTarget && !busy) onClose();
    }}>
      <div
        aria-busy={busy}
        aria-labelledby="create-user-title"
        aria-modal="true"
        className="dialog create-wizard"
        ref={dialogRef}
        role="dialog"
        tabIndex={-1}
      >
        <div className="dialog-header">
          <div><p className="eyebrow">Provision access</p><h2 id="create-user-title">Create user</h2></div>
          <button aria-label="Close create user wizard" className="icon-button" disabled={busy} onClick={onClose} type="button"><X aria-hidden="true" size={19} /></button>
        </div>
        <ol aria-label="Create user progress" className="wizard-steps">
          {steps.map((label, index) => <li aria-current={step === index ? "step" : undefined} className={step >= index ? "wizard-step-active" : ""} key={label}><span>{index + 1}</span>{label}</li>)}
        </ol>
        <div className="wizard-body">
          <h3 ref={stepHeadingRef} tabIndex={-1}>{steps[step]}</h3>
          {errors.length > 0 && (
            <div aria-label="Create user errors" className="error-summary" role="alert">
              <strong>Fix the following before continuing:</strong>
              <ul>{errors.map((error) => <li key={error}>{error}</li>)}</ul>
            </div>
          )}
          {requestError && <ErrorMessage message={requestError} />}
          {step === 0 && (
            <div className="wizard-fields">
              <label><span>User identity claim value</span><input autoComplete="off" disabled={busy} onChange={(event) => { setUserId(event.target.value); setErrors([]); }} ref={userIdRef} value={userId} /></label>
              <p className="field-help">Use the exact configured JWT claim value. This becomes the immutable <code>user_id</code>.</p>
              <label><span>Display name</span><input autoComplete="off" disabled={busy} onChange={(event) => { setName(event.target.value); setErrors([]); }} value={name} /></label>
            </div>
          )}
          {step === 1 && (
            <div className="wizard-fields">
              <p className="field-help">Daily limits apply independently. Enter 0 only when that dimension must be Unlimited.</p>
              <label><span>USD limit</span><input aria-label="Create USD limit" disabled={busy} min="0" onChange={(event) => { setDailyUsd(event.target.value); setUnlimitedConfirmed(false); setErrors([]); }} step="0.000001" type="number" value={dailyUsd} /></label>
              <label><span>Input token limit</span><input aria-label="Create input token limit" disabled={busy} min="0" onChange={(event) => { setDailyInput(event.target.value); setUnlimitedConfirmed(false); setErrors([]); }} step="1" type="number" value={dailyInput} /></label>
              <label><span>Output token limit</span><input aria-label="Create output token limit" disabled={busy} min="0" onChange={(event) => { setDailyOutput(event.target.value); setUnlimitedConfirmed(false); setErrors([]); }} step="1" type="number" value={dailyOutput} /></label>
              {unlimited && (
                <label className="unlimited-confirm"><input checked={unlimitedConfirmed} disabled={busy} onChange={(event) => setUnlimitedConfirmed(event.target.checked)} type="checkbox" /><span>I confirm that each 0 value above means Unlimited.</span></label>
              )}
            </div>
          )}
          {step === 2 && limits && (
            <div className="wizard-review">
              <dl>
                <div><dt>User identity</dt><dd>{userId.trim()}</dd></div>
                <div><dt>Display name</dt><dd>{name.trim()}</dd></div>
                <div><dt>USD / day</dt><dd>{formatLimit(limits.daily_usd, (value) => formatUsd(value, 6))}</dd></div>
                <div><dt>Input tokens / day</dt><dd>{formatLimit(limits.daily_input_tokens, (value) => value.toLocaleString())}</dd></div>
                <div><dt>Output tokens / day</dt><dd>{formatLimit(limits.daily_output_tokens, (value) => value.toLocaleString())}</dd></div>
              </dl>
              <div className="safety-warning"><ShieldAlert aria-hidden="true" size={18} /><span>Creating this user grants quota-managed access for the immutable identity shown above. No request is sent until you select Create user.</span></div>
              <label className="review-option"><input checked={openDetails} disabled={busy} onChange={(event) => setOpenDetails(event.target.checked)} type="checkbox" />Open details if the new user is added to this page</label>
            </div>
          )}
        </div>
        <div className="dialog-actions wizard-actions">
          <button className="button button-secondary" disabled={busy} onClick={step === 0 ? onClose : () => { setStep((current) => current - 1); setErrors([]); setRequestError(""); }} type="button">{step === 0 ? "Cancel" : "Back"}</button>
          {step < 2 ? (
            <button className="button button-primary" disabled={busy} onClick={next} type="button">Next<ChevronRight aria-hidden="true" size={16} /></button>
          ) : (
            <button className="button button-primary" disabled={busy} onClick={() => void create()} type="button">{busy ? <BusyLabel>Creating</BusyLabel> : <><UserPlus aria-hidden="true" size={16} />Create user</>}</button>
          )}
        </div>
      </div>
    </div>
  );
}

function utcDate(offsetDays: number): string {
  const date = new Date();
  date.setUTCHours(0, 0, 0, 0);
  date.setUTCDate(date.getUTCDate() + offsetDays);
  return date.toISOString().slice(0, 10);
}

function Pagination({
  busy,
  hasNext,
  hasPrevious,
  label,
  onNext,
  onPrevious,
}: {
  busy: boolean;
  hasNext: boolean;
  hasPrevious: boolean;
  label: string;
  onNext: () => void;
  onPrevious: () => void;
}) {
  return <div className="pagination"><span>{label}</span><div><button className="button button-secondary" disabled={busy || !hasPrevious} onClick={onPrevious} type="button"><ArrowLeft aria-hidden="true" size={15} />Previous</button><button className="button button-secondary" disabled={busy || !hasNext} onClick={onNext} type="button">Next<ArrowRight aria-hidden="true" size={15} /></button></div></div>;
}

function UsageTab({ active, cfg, session, userId }: { active: boolean; cfg: AdminConfig; session: Session; userId: string }) {
  const [start, setStart] = useState(() => utcDate(-29));
  const [end, setEnd] = useState(() => utcDate(0));
  const [applied, setApplied] = useState(() => ({ start: utcDate(-29), end: utcDate(0) }));
  const [page, setPage] = useState<UsageHistoryResponse | null>(null);
  const [cursors, setCursors] = useState<Array<string | null>>([null]);
  const [pageIndex, setPageIndex] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [rangeError, setRangeError] = useState("");
  const loaded = useRef(false);
  const request = useRef(0);

  async function load(cursor: string | null, range: { start: string; end: string } | undefined, targetIndex: number, reset = false) {
    const currentRequest = ++request.current;
    setLoading(true);
    setError("");
    try {
      const next = await api.usageHistory(cfg, session, userId, { ...range, limit: PAGE_SIZE, cursor });
      if (request.current !== currentRequest) return;
      const resolvedRange = { start: next.start, end: next.end };
      setPage(next);
      setApplied(resolvedRange);
      if (range === undefined) {
        setStart(next.start);
        setEnd(next.end);
      }
      if (reset) {
        setCursors([null]);
        setPageIndex(0);
      } else {
        setCursors((current) => targetIndex > pageIndex ? [...current.slice(0, pageIndex + 1), cursor] : current);
        setPageIndex(targetIndex);
      }
      loaded.current = true;
    } catch (caught) {
      if (request.current === currentRequest) setError(apiErrorMessage(caught));
    } finally {
      if (request.current === currentRequest) setLoading(false);
    }
  }

  useEffect(() => {
    if (active && !loaded.current) void load(null, undefined, 0, true);
  }, [active]);

  function applyRange(event: FormEvent) {
    event.preventDefault();
    if (!start || !end || start > end) {
      setRangeError("Choose an inclusive start date that is not after the end date.");
      return;
    }
    setRangeError("");
    void load(null, { start, end }, 0, true);
  }

  return (
    <div aria-busy={loading} className="drawer-tab-content">
      <form className="range-form" onSubmit={applyRange}>
        <label><span>Start date</span><input max={utcDate(0)} onChange={(event) => setStart(event.target.value)} type="date" value={start} /></label>
        <label><span>End date</span><input max={utcDate(0)} onChange={(event) => setEnd(event.target.value)} type="date" value={end} /></label>
        <button className="button button-secondary" disabled={loading} type="submit"><CalendarDays aria-hidden="true" size={16} />Apply range</button>
      </form>
      <p className="field-help">Dates are inclusive. The gateway enforces the configured usage retention window.</p>
      {rangeError && <ErrorMessage message={rangeError} />}
      {error && <ErrorMessage message={error} />}
      {error && page && <span className="ops-status ops-status-amber"><span aria-hidden="true" />Showing the previous usage page</span>}
      {loading && !page ? <div className="drawer-loading"><BusyLabel>Loading usage</BusyLabel></div> : page && (
        <>
          <div aria-label="Daily usage history" className="drawer-table-scroll" role="region" tabIndex={0}>
            <table className="compact-table"><thead><tr><th>Date</th><th>USD</th><th>Input tokens</th><th>Output tokens</th><th>Requests</th></tr></thead><tbody>
              {page.usage.map((row) => <tr key={row.window}><td>{row.window}</td><td>{formatUsd(row.cost_usd, 4)}</td><td>{row.input_tokens.toLocaleString()}</td><td>{row.output_tokens.toLocaleString()}</td><td>{row.requests.toLocaleString()}</td></tr>)}
            </tbody></table>
            {page.usage.length === 0 && <div className="compact-empty">No usage was recorded in this date range.</div>}
          </div>
          <Pagination busy={loading} hasNext={Boolean(page.next_cursor)} hasPrevious={pageIndex > 0} label={`${page.usage.length} daily records on this page · ${page.start} to ${page.end}`} onNext={() => page.next_cursor && void load(page.next_cursor, applied, pageIndex + 1)} onPrevious={() => void load(cursors[pageIndex - 1], applied, pageIndex - 1)} />
        </>
      )}
    </div>
  );
}

function auditUsd(snapshot: AuditUserSnapshot): number {
  return snapshot.limits.daily_usd_micro / 1_000_000;
}

export function summarizeAuditEvent(event: AuditEvent): string {
  if (!event.before) return `Created ${event.after.status}; USD ${formatLimit(auditUsd(event.after), (value) => formatUsd(value, 6))}, input ${formatLimit(event.after.limits.daily_input_tokens, (value) => value.toLocaleString())}, output ${formatLimit(event.after.limits.daily_output_tokens, (value) => value.toLocaleString())}.`;
  const changes: string[] = [];
  if (event.before.name !== event.after.name) changes.push(`name: ${event.before.name} → ${event.after.name}`);
  if (event.before.status !== event.after.status) changes.push(`status: ${event.before.status} → ${event.after.status}`);
  if (event.before.limits.daily_usd_micro !== event.after.limits.daily_usd_micro) changes.push(`USD: ${formatLimit(auditUsd(event.before), (value) => formatUsd(value, 6))} → ${formatLimit(auditUsd(event.after), (value) => formatUsd(value, 6))}`);
  if (event.before.limits.daily_input_tokens !== event.after.limits.daily_input_tokens) changes.push(`input: ${formatLimit(event.before.limits.daily_input_tokens, (value) => value.toLocaleString())} → ${formatLimit(event.after.limits.daily_input_tokens, (value) => value.toLocaleString())}`);
  if (event.before.limits.daily_output_tokens !== event.after.limits.daily_output_tokens) changes.push(`output: ${formatLimit(event.before.limits.daily_output_tokens, (value) => value.toLocaleString())} → ${formatLimit(event.after.limits.daily_output_tokens, (value) => value.toLocaleString())}`);
  return changes.length > 0 ? changes.join("; ") : `Configuration version ${event.before.version} → ${event.after.version}.`;
}

function AuditTable({ events, label, onTarget }: { events: AuditEvent[]; label: string; onTarget?: (userId: string) => void }) {
  return <div aria-label={label} className="drawer-table-scroll" role="region" tabIndex={0}>
    <table className="compact-table audit-table"><thead><tr><th>Timestamp</th><th>Actor / auth</th><th>Event</th><th>User</th><th>Reason</th><th>Request ID</th><th>Change summary</th></tr></thead><tbody>
      {events.map((event) => <tr key={event.event_key}><td>{formatTimestamp(event.created_at)}</td><td>{event.actor}<small>{event.auth_method}</small></td><td>{event.event_type}</td><td>{onTarget ? <button className="link-button" onClick={() => onTarget(event.user_id)} type="button">{event.user_id}</button> : event.user_id}</td><td>{event.reason || "Not provided"}</td><td><code>{event.request_id || "Not provided"}</code></td><td>{summarizeAuditEvent(event)}</td></tr>)}
    </tbody></table>
    {events.length === 0 && <div className="compact-empty">No administrative changes were found.</div>}
  </div>;
}

function ChangesTab({ active, cfg, session, userId }: { active: boolean; cfg: AdminConfig; session: Session; userId: string }) {
  const [page, setPage] = useState<UserAuditListResponse | null>(null);
  const [cursors, setCursors] = useState<Array<string | null>>([null]);
  const [pageIndex, setPageIndex] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const loaded = useRef(false);

  async function load(cursor: string | null, targetIndex: number) {
    setLoading(true);
    setError("");
    try {
      const next = await api.listUserAuditPage(cfg, session, userId, { limit: PAGE_SIZE, cursor });
      setPage(next);
      setCursors((current) => targetIndex > pageIndex ? [...current.slice(0, pageIndex + 1), cursor] : current);
      setPageIndex(targetIndex);
      loaded.current = true;
    } catch (caught) {
      setError(apiErrorMessage(caught));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    if (active && !loaded.current) void load(null, 0);
  }, [active]);

  return <div aria-busy={loading} className="drawer-tab-content">
    {error && <ErrorMessage message={error} />}
    {error && page && <span className="ops-status ops-status-amber"><span aria-hidden="true" />Showing the previous changes page</span>}
    {loading && !page ? <div className="drawer-loading"><BusyLabel>Loading changes</BusyLabel></div> : page && <><AuditTable events={page.events} label={`Administrative changes for ${userId}`} /><Pagination busy={loading} hasNext={Boolean(page.next_cursor)} hasPrevious={pageIndex > 0} label={`${page.events.length} changes on this page`} onNext={() => page.next_cursor && void load(page.next_cursor, pageIndex + 1)} onPrevious={() => void load(cursors[pageIndex - 1], pageIndex - 1)} /></>}
  </div>;
}

export function UserDetailDrawer({
  cfg,
  session,
  user,
  onCanonical,
  onClose,
  onEdit,
  onStatus,
  statusActionAvailable = true,
  suspended = false,
}: {
  cfg: AdminConfig;
  session: Session;
  user: UserRow;
  onCanonical: (user: AdminUser) => void;
  onClose: () => void;
  onEdit: () => void;
  onStatus: () => void;
  statusActionAvailable?: boolean;
  suspended?: boolean;
}) {
  const [tab, setTab] = useState<DrawerTab>("overview");
  const [detailError, setDetailError] = useState("");
  const [detailLoading, setDetailLoading] = useState(true);
  const drawerRef = useRef<HTMLElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const tabRefs = useRef<Array<HTMLButtonElement | null>>([]);
  useModalLifecycle(false, onClose, drawerRef, closeRef, !suspended);
  const tabs: Array<{ id: DrawerTab; label: string }> = [
    { id: "overview", label: "Overview" },
    { id: "usage", label: "Usage" },
    { id: "changes", label: "Changes" },
  ];

  useEffect(() => {
    let active = true;
    setDetailLoading(true);
    setDetailError("");
    void api.getUser(cfg, session, user.user_id).then((result) => {
      if (active) onCanonical(result.data.user);
    }).catch((caught) => {
      if (active) setDetailError(apiErrorMessage(caught));
    }).finally(() => {
      if (active) setDetailLoading(false);
    });
    return () => { active = false; };
  }, [user.user_id]);

  function selectTab(nextIndex: number) {
    const normalized = (nextIndex + tabs.length) % tabs.length;
    setTab(tabs[normalized].id);
    tabRefs.current[normalized]?.focus();
  }

  function tabKeyDown(event: ReactKeyboardEvent<HTMLButtonElement>, index: number) {
    if (event.key === "ArrowRight") { event.preventDefault(); selectTab(index + 1); }
    else if (event.key === "ArrowLeft") { event.preventDefault(); selectTab(index - 1); }
    else if (event.key === "Home") { event.preventDefault(); selectTab(0); }
    else if (event.key === "End") { event.preventDefault(); selectTab(tabs.length - 1); }
  }

  return <div className="drawer-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
    <aside aria-hidden={suspended || undefined} aria-labelledby="user-detail-title" aria-modal={suspended ? undefined : true} className="detail-drawer" ref={drawerRef} role="dialog" tabIndex={-1}>
      <div className="drawer-header"><div><p className="eyebrow">User details</p><h2 id="user-detail-title">{user.name || "Unnamed identity"}</h2><code>{user.user_id}</code></div><button aria-label="Close user details" className="icon-button" onClick={onClose} ref={closeRef} type="button"><X aria-hidden="true" size={20} /></button></div>
      {detailError && <div className="drawer-message"><ErrorMessage message={detailError} /><span className="ops-status ops-status-amber"><span aria-hidden="true" />Showing configuration from the current users page</span></div>}
      {detailLoading && <span className="detail-refresh"><BusyLabel>Refreshing detail</BusyLabel></span>}
      <div aria-label="User detail sections" className="drawer-tabs" role="tablist">
        {tabs.map((item, index) => <button aria-controls={`user-${item.id}-panel`} aria-selected={tab === item.id} id={`user-${item.id}-tab`} key={item.id} onClick={() => setTab(item.id)} onKeyDown={(event) => tabKeyDown(event, index)} ref={(element) => { tabRefs.current[index] = element; }} role="tab" tabIndex={tab === item.id ? 0 : -1} type="button">{item.label}</button>)}
      </div>
      <section aria-labelledby="user-overview-tab" hidden={tab !== "overview"} id="user-overview-panel" role="tabpanel" tabIndex={0}>
        <div className="drawer-overview">
          <div className="drawer-actions">
            <button className="button button-secondary" onClick={onEdit} type="button"><Pencil aria-hidden="true" size={16} />Edit limits</button>
            <button
              aria-label={statusActionAvailable ? (user.status === "active" ? "Block user" : "Unblock user") : "Status change unavailable until a fresh enforcement summary loads"}
              className={`button ${user.status === "active" ? "button-danger" : "button-primary"}`}
              disabled={!statusActionAvailable}
              onClick={onStatus}
              type="button"
            >
              {user.status === "active" ? <Lock aria-hidden="true" size={16} /> : <Unlock aria-hidden="true" size={16} />}
              {user.status === "active" ? "Block user" : "Unblock user"}
            </button>
          </div>
          <section><h3>Identity and status</h3><dl className="detail-list"><div><dt>Status</dt><dd><span className={`status-badge status-${user.status}`}><span aria-hidden="true" />{user.status}</span></dd></div><div><dt>Status origin</dt><dd>{user.status_origin || "Not provided"}</dd></div><div><dt>Status reason</dt><dd>{user.status_reason || "Not provided"}</dd></div><div><dt>Created</dt><dd>{formatTimestamp(user.created_at)}</dd></div><div><dt>Updated</dt><dd>{formatTimestamp(user.updated_at)}</dd></div><div><dt>Version</dt><dd>{user.version}</dd></div></dl></section>
          <section><h3>Limits and usage today</h3><dl className="detail-list"><div><dt>USD</dt><dd>{formatUsd(user.today.cost_usd, 6)} of {formatLimit(user.limits.daily_usd, (value) => formatUsd(value, 6))}</dd></div><div><dt>Input tokens</dt><dd>{user.today.input_tokens.toLocaleString()} of {formatLimit(user.limits.daily_input_tokens, (value) => value.toLocaleString())}</dd></div><div><dt>Output tokens</dt><dd>{user.today.output_tokens.toLocaleString()} of {formatLimit(user.limits.daily_output_tokens, (value) => value.toLocaleString())}</dd></div><div><dt>Requests</dt><dd>{user.today.requests.toLocaleString()}</dd></div></dl></section>
        </div>
      </section>
      <section aria-labelledby="user-usage-tab" hidden={tab !== "usage"} id="user-usage-panel" role="tabpanel" tabIndex={0}><UsageTab active={tab === "usage"} cfg={cfg} session={session} userId={user.user_id} /></section>
      <section aria-labelledby="user-changes-tab" hidden={tab !== "changes"} id="user-changes-panel" role="tabpanel" tabIndex={0}><ChangesTab active={tab === "changes"} cfg={cfg} session={session} userId={user.user_id} /></section>
    </aside>
  </div>;
}

type AuditLoadRequest = Readonly<{
  cursor: string | null;
  targetIndex: number;
  reset: boolean;
}>;

export function GlobalAuditView({ cfg, session, onTargetUser }: { cfg: AdminConfig; session: Session; onTargetUser: (userId: string) => void }) {
  const [events, setEvents] = useState<AuditEvent[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [cursors, setCursors] = useState<Array<string | null>>([null]);
  const [pageIndex, setPageIndex] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [failedLoad, setFailedLoad] = useState<AuditLoadRequest | null>(null);
  const [lastSuccessfulAt, setLastSuccessfulAt] = useState<number | null>(null);
  const request = useRef(0);

  async function load(loadRequest: AuditLoadRequest) {
    const { cursor, targetIndex, reset } = loadRequest;
    const currentRequest = ++request.current;
    setLoading(true);
    setError("");
    setFailedLoad(null);
    try {
      const page = await api.listAuditPage(cfg, session, { limit: PAGE_SIZE, cursor });
      if (request.current !== currentRequest) return;
      setEvents(page.events);
      setNextCursor(page.next_cursor);
      if (reset) {
        setCursors([null]);
        setPageIndex(0);
      } else {
        setCursors((current) => targetIndex > pageIndex ? [...current.slice(0, pageIndex + 1), cursor] : current);
        setPageIndex(targetIndex);
      }
      setLastSuccessfulAt(Date.now());
    } catch (caught) {
      if (request.current === currentRequest) {
        setError(apiErrorMessage(caught));
        setFailedLoad(loadRequest);
      }
    } finally {
      if (request.current === currentRequest) setLoading(false);
    }
  }

  useEffect(() => {
    void load({ cursor: null, targetIndex: 0, reset: true });
    return () => { request.current += 1; };
  }, []);

  const loadedAt = lastSuccessfulAt === null
    ? null
    : new Date(lastSuccessfulAt).toLocaleString();

  return <section aria-busy={loading} aria-labelledby="audit-log-title" className="audit-panel">
    <div className="panel-heading">
      <div><p className="eyebrow">Administrative history</p><h2 id="audit-log-title">Audit log</h2><p>Newest-first create, limit, and status changes retained by the gateway.</p></div>
      <div className="audit-heading-actions">
        {loadedAt !== null && <p className="updated-at" role="status">Loaded {loadedAt}</p>}
        <button className="button button-secondary" onClick={() => void load({ cursor: null, targetIndex: 0, reset: true })} type="button"><RefreshCw className={loading ? "spin" : ""} aria-hidden="true" size={16} />Refresh audit log</button>
      </div>
    </div>
    {error && <ErrorMessage message={error} />}
    {failedLoad && <button className="button button-secondary audit-stale" onClick={() => void load(failedLoad)} type="button">Retry</button>}
    {error && loadedAt !== null && <span className="ops-status ops-status-amber audit-stale" role="status"><span aria-hidden="true" />Showing cached audit data loaded {loadedAt}.</span>}
    {loading && lastSuccessfulAt === null ? <div className="audit-loading"><BusyLabel>Loading audit log</BusyLabel></div> : lastSuccessfulAt !== null ? <><AuditTable events={events} label="Global administrative audit log" onTarget={onTargetUser} /><Pagination busy={loading} hasNext={Boolean(nextCursor)} hasPrevious={pageIndex > 0} label={`${events.length} events on this page`} onNext={() => nextCursor && void load({ cursor: nextCursor, targetIndex: pageIndex + 1, reset: false })} onPrevious={() => void load({ cursor: cursors[pageIndex - 1], targetIndex: pageIndex - 1, reset: false })} /></> : <div className="compact-empty">The audit log could not be loaded. Retry the request.</div>}
  </section>;
}
