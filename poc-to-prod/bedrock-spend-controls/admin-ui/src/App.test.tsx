import { useState } from "react";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  Dashboard,
  LimitsDialog,
  mergeCanonicalUser,
  mergeRefreshedUsers,
  QuotaUsage,
  StatusDialog,
  UsersPanel,
} from "./App";
import { ApiError, api, type AdminUser, type AuditEvent, type CurrentUsage, type Operations, type QuotaPeriod, type Summary, type UsageMetrics, type UserRow } from "./api";
import type { Session } from "./auth";
import type { AdminConfig } from "./config";

const cfg: AdminConfig = {
  gatewayUrl: "https://gateway.example.test",
  region: "us-east-1",
  userPoolId: "us-east-1_pool",
  userPoolClientId: "client",
  identityPoolId: "us-east-1:identity",
  cognitoDomain: "https://login.example.test",
  cognitoIssuer: "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_pool",
};

const session: Session = {
  email: "admin@example.test",
  authorization: vi.fn(),
  reauthenticate: vi.fn(),
  logout: vi.fn(),
};

const emptyUsageMetrics: UsageMetrics = {
  status: "available",
  as_of: "2026-09-02T10:00:00Z",
  start: "2026-08-31",
  end: "2026-09-02",
  period: "daily",
  days: ["2026-08-31", "2026-09-01", "2026-09-02"],
  models: [],
  totals: { cost_usd: 0, requests: 0, input_tokens: 0, output_tokens: 0 },
  top_users: [],
};

// Every Dashboard render mounts the Overview usage charts; keep their fetch
// deterministic by default. restoreMocks unwinds this spy after each test.
beforeEach(() => {
  vi.spyOn(api, "usageMetrics").mockResolvedValue(emptyUsageMetrics);
});

const summary: Summary = {
  enforcement: {
    source: "dynamodb",
    as_of: "2026-09-02T10:00:00Z",
    window: "2026-09-02",
    mode: "layered",
    credential_ttl_seconds: 900,
    permission_lease_seconds: 300,
    post_detection_fallback_seconds: 900,
    refresh_overlap_seconds: 10,
    refresh_jitter_seconds: 5,
    vend_rate_limit_per_minute: 6,
    revocation_policy_shards: 19,
    revocation_reconcile_minutes: 5,
    total_users: 1,
    blocked_users: 0,
    blocked_user_ids: [],
    today: { cost_usd: 5, input_tokens: 50, output_tokens: 10, requests: 4 },
  },
  observability: {
    source: "bedrock_model_invocation_logs",
    delivery: "cloudwatch_logs_subscription",
    metrics_namespace: "BedrockSpendControls",
    detection_lag_metric: "DetectionLagMilliseconds",
  },
};

const operations: Operations = {
  as_of: "2026-09-02T10:00:00Z",
  configuration: {
    mode: "layered",
    credential_ttl_seconds: 900,
    permission_lease_seconds: 300,
    permission_lease_enabled: false,
    effective_permission_lease_seconds: null,
    post_detection_fallback_seconds: 900,
    refresh_overlap_seconds: 10,
    refresh_jitter_seconds: 5,
    vend_rate_limit_per_minute: 6,
    revocation_enabled: false,
    revocation_policy_shards: 19,
    revocation_policy_max_characters: 6144,
    revocation_reconcile_minutes: 5,
  },
  emergency: {
    state: "inactive",
    desired_active: false,
    generation: 0,
    applied_generation: 0,
    requested_at: null,
    applied_at: null,
    converged: true,
  },
  qualification: {
    status: "baseline_qualified",
    emergency_status: "baseline_qualified",
    source: "deployment_metadata",
  },
  metrics: {
    namespace: "BedrockSpendControls",
    detection_lag_metric: "DetectionLagMilliseconds",
    detection_lag_p95_ms: 100,
    detection_lag_timestamp: "2026-09-02T09:59:00Z",
    telemetry_status: "complete",
    last_reconciliation_at: null,
    reconciliation_status: "not_applicable",
    revoked_identities_desired: null,
    recent_sync_failure_count: 0,
    recent_overflow_count: 0,
    recent_emergency_failure_count: 0,
    window_minutes: 15,
  },
  alarms: [],
  cloudwatch: { status: "available" },
};

function currentUsageRow(period: QuotaPeriod, overrides = {}) {
  const starts = { daily: "2026-09-02", weekly: "2026-08-31", monthly: "2026-09-01" };
  const ends = { daily: "2026-09-03", weekly: "2026-09-07", monthly: "2026-10-01" };
  return {
    period,
    window: starts[period],
    window_start: `${starts[period]}T00:00:00+00:00`,
    window_end: `${ends[period]}T00:00:00+00:00`,
    resets_at: `${ends[period]}T00:00:00+00:00`,
    cost_usd: 5,
    input_tokens: 50,
    output_tokens: 10,
    requests: 4,
    ...overrides,
  };
}

const currentUsage: CurrentUsage = {
  daily: currentUsageRow("daily"),
  weekly: currentUsageRow("weekly", { cost_usd: 7, requests: 6 }),
  monthly: currentUsageRow("monthly", { cost_usd: 9, requests: 8 }),
};

const alice: UserRow = {
  user_id: "tenant/alice",
  name: "Alice Example",
  status: "active",
  status_reason: "User created",
  status_origin: "admin",
  version: 1,
  created_at: "2026-09-01T10:00:00Z",
  updated_at: "2026-09-01T10:00:00Z",
  limits: {
    daily: { usd: 10, input_tokens: 100, output_tokens: 20 },
    weekly: { usd: 20, input_tokens: 200, output_tokens: 40 },
    monthly: null,
  },
  today: { cost_usd: 5, input_tokens: 50, output_tokens: 10, requests: 4 },
  current_usage: currentUsage,
};

function canonical(overrides: Partial<AdminUser> = {}): AdminUser {
  const { today: _today, current_usage: _currentUsage, ...base } = alice;
  return { ...base, ...overrides };
}

function UsersHarness({ summaryRefresh = vi.fn().mockResolvedValue(undefined) }: { summaryRefresh?: () => Promise<void> }) {
  const [users, setUsers] = useState([alice]);
  return (
    <UsersPanel
      cfg={cfg}
      enforcement={summary.enforcement}
      error=""
      loading={false}
      onSummaryRefresh={summaryRefresh}
      onUserChanged={(updated) => setUsers((current) => current.map((item) =>
        item.user_id === updated.user_id ? { ...updated, today: item.today, current_usage: item.current_usage } : item,
      ))}
      session={session}
      stale={false}
      users={users}
    />
  );
}

// The dashboard opens on Overview; user management lives on its own tab.
async function openTab(actor: ReturnType<typeof userEvent.setup>, name: "Overview" | "Users" | "Operations" | "Audit log") {
  await actor.click(screen.getByRole("button", { name }));
}

describe("quota presentation", () => {
  it("renders zero limits as Unlimited while keeping nonzero usage visible and no progressbar", () => {
    render(<QuotaUsage current={25} format={(value) => String(value)} limit={0} />);

    expect(screen.getByText("25")).toBeInTheDocument();
    expect(screen.getByText("of Unlimited")).toBeInTheDocument();
    expect(screen.queryByRole("progressbar")).not.toBeInTheDocument();
    expect(document.querySelector(".progress-critical")).not.toBeInTheDocument();
  });

  it("retains finite normal, warning, and critical states with accessible value text", () => {
    const { rerender } = render(<QuotaUsage current={5} format={(value) => `${value} units`} limit={10} />);
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuetext", "5 units of 10 units used (50 percent)");
    expect(document.querySelector(".progress-normal")).toBeInTheDocument();

    rerender(<QuotaUsage current={8} format={(value) => `${value} units`} limit={10} />);
    expect(document.querySelector(".progress-warning")).toBeInTheDocument();

    rerender(<QuotaUsage current={12} format={(value) => `${value} units`} limit={10} />);
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuetext", "12 units of 10 units used (120 percent)");
    expect(document.querySelector(".progress-critical")).toBeInTheDocument();
  });

  it("switches the displayed calendar usage while surfacing highest utilization", async () => {
    const actor = userEvent.setup();
    render(<UsersHarness />);

    expect(screen.getByText("Highest: Daily 50%")).toBeInTheDocument();
    await actor.selectOptions(screen.getByLabelText("Usage period"), "weekly");
    expect(screen.getByRole("columnheader", { name: "Weekly USD" })).toBeInTheDocument();
    expect(screen.getByText("$7.000000")).toBeInTheDocument();
    await actor.selectOptions(screen.getByLabelText("Usage period"), "monthly");
    expect(screen.getAllByText("Disabled").length).toBeGreaterThanOrEqual(3);
  });
});

describe("limit safety dialog", () => {
  it("requires confirmation and a reason for Unlimited or below-usage limits", async () => {
    const actor = userEvent.setup();
    const onSave = vi.fn();
    render(<LimitsDialog apiError="" busy={false} onClose={vi.fn()} onSave={onSave} user={alice} />);

    expect(screen.getByText(/Enter 0 for an Unlimited/)).toBeInTheDocument();
    const usd = screen.getByLabelText("Daily USD limit");
    await actor.clear(usd);
    await actor.type(usd, "0");
    const input = screen.getByLabelText("Daily input token limit");
    await actor.clear(input);
    await actor.type(input, "40");

    expect(screen.getByRole("status")).toHaveTextContent("input tokens");
    const save = screen.getByRole("button", { name: "Save limits" });
    expect(save).toBeDisabled();
    const confirmation = screen.getByRole("checkbox", { name: /should change from a finite value to Unlimited/ });
    await actor.click(confirmation);
    expect(save).toBeDisabled();
    const reason = screen.getByLabelText(/Reason/);
    expect(reason).toHaveAttribute("aria-required", "true");
    await actor.type(reason, "   ");
    expect(save).toBeDisabled();
    await actor.clear(reason);
    await actor.type(reason, "  Capacity exception review  ");
    expect(save).toBeEnabled();
    await actor.click(save);

    expect(onSave).toHaveBeenCalledWith({
      limits: {
        daily: { usd: 0, input_tokens: 40, output_tokens: 20 },
        weekly: alice.limits.weekly,
        monthly: null,
      },
      reason: "Capacity exception review",
    });
  });

  it("requires a reason when a finite limit is below current usage", async () => {
    const actor = userEvent.setup();
    const onSave = vi.fn();
    render(<LimitsDialog apiError="" busy={false} onClose={vi.fn()} onSave={onSave} user={alice} />);

    const input = screen.getByLabelText("Daily input token limit");
    await actor.clear(input);
    await actor.type(input, "40");
    const save = screen.getByRole("button", { name: "Save limits" });
    expect(screen.queryByRole("checkbox", { name: /should change from a finite value to Unlimited/ })).not.toBeInTheDocument();
    expect(save).toBeDisabled();
    await actor.type(screen.getByLabelText(/Reason/), "Below-usage test");
    expect(save).toBeEnabled();
    await actor.click(save);

    expect(onSave).toHaveBeenCalledWith({
      limits: {
        daily: { usd: 10, input_tokens: 40, output_tokens: 20 },
        weekly: alice.limits.weekly,
        monthly: null,
      },
      reason: "Below-usage test",
    });
  });

  it("includes an optional trimmed reason for an ordinary limit change", async () => {
    const actor = userEvent.setup();
    const onSave = vi.fn();
    render(<LimitsDialog apiError="" busy={false} onClose={vi.fn()} onSave={onSave} user={alice} />);

    const input = screen.getByLabelText("Daily input token limit");
    await actor.clear(input);
    await actor.type(input, "101");
    const reason = screen.getByLabelText(/Reason/);
    expect(reason).toHaveAttribute("aria-required", "false");
    await actor.type(reason, "  Annual allocation  ");
    await actor.click(screen.getByRole("button", { name: "Save limits" }));

    expect(onSave).toHaveBeenCalledWith({
      limits: {
        daily: { usd: 10, input_tokens: 101, output_tokens: 20 },
        weekly: alice.limits.weekly,
        monthly: null,
      },
      reason: "Annual allocation",
    });
  });

  it("requires confirmation and a reason when enabling an all-Unlimited period", async () => {
    const actor = userEvent.setup();
    const onSave = vi.fn();
    render(<LimitsDialog apiError="" busy={false} onClose={vi.fn()} onSave={onSave} user={alice} />);

    await actor.click(screen.getByRole("checkbox", { name: "Monthly" }));
    expect(screen.getByText(/include usage accumulated since their UTC boundary/)).toBeInTheDocument();
    const save = screen.getByRole("button", { name: "Save limits" });
    expect(save).toBeDisabled();
    await actor.click(screen.getByRole("checkbox", { name: /monthly usd.*Unlimited/i }));
    await actor.type(screen.getByLabelText(/Reason/), "Enable monthly accounting");
    await actor.click(save);

    expect(onSave).toHaveBeenCalledWith({
      limits: {
        daily: alice.limits.daily,
        weekly: alice.limits.weekly,
        monthly: { usd: 0, input_tokens: 0, output_tokens: 0 },
      },
      reason: "Enable monthly accounting",
    });
  });
});

describe("canonical local reconciliation", () => {
  it("keeps the latest cached usage while applying returned configuration", () => {
    const latestUsage = { ...alice, today: { ...alice.today, cost_usd: 9, requests: 8 } };
    const updated = canonical({ status: "blocked", version: 2, status_reason: "Policy request" });

    expect(mergeCanonicalUser([latestUsage], updated)[0]).toEqual({
      ...updated,
      today: latestUsage.today,
      current_usage: latestUsage.current_usage,
    });
  });

  it("keeps newer canonical configuration when an eventually consistent refresh is older", () => {
    const current = { ...alice, status: "blocked" as const, version: 2, status_reason: "Policy request" };
    const staleRefresh = { ...alice, today: { ...alice.today, cost_usd: 9, requests: 8 } };

    expect(mergeRefreshedUsers([current], [staleRefresh])[0]).toEqual({
      ...current,
      today: staleRefresh.today,
      current_usage: staleRefresh.current_usage,
    });
  });
});

describe("status safety dialog", () => {
  it("requires a trimmed reason, describes bounded overspend, cancels with Escape, and restores focus", async () => {
    const actor = userEvent.setup();
    const onConfirm = vi.fn();

    function Harness() {
      const [open, setOpen] = useState(false);
      return (
        <>
          <button onClick={() => setOpen(true)} type="button">Open status</button>
          {open && (
            <StatusDialog
              apiError=""
              busy={false}
              enforcement={summary.enforcement}
              onClose={() => setOpen(false)}
              onConfirm={onConfirm}
              user={alice}
            />
          )}
        </>
      );
    }

    render(<Harness />);
    const opener = screen.getByRole("button", { name: "Open status" });
    await actor.click(opener);
    expect(screen.getByText("Current status").parentElement).toHaveTextContent("active");
    expect(screen.getByText("Next status").parentElement).toHaveTextContent("blocked");
    expect(screen.getByText(/bounded overspend is limited/)).toHaveTextContent("15 min");
    const reason = screen.getByLabelText(/Reason/);
    expect(reason).toHaveFocus();
    expect(screen.getByRole("button", { name: "Block user" })).toBeDisabled();
    await actor.type(reason, "   ");
    expect(screen.getByRole("button", { name: "Block user" })).toBeDisabled();
    await actor.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(opener).toHaveFocus();

    await actor.click(opener);
    await actor.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(opener).toHaveFocus();
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("keeps focus inside the modal while a status write is busy", async () => {
    const actor = userEvent.setup();
    const props = {
      apiError: "",
      enforcement: summary.enforcement,
      onClose: vi.fn(),
      onConfirm: vi.fn(),
      user: alice,
    };
    const { rerender } = render(<StatusDialog {...props} busy={false} />);

    rerender(<StatusDialog {...props} busy />);
    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveFocus();
    await actor.tab();
    expect(dialog).toHaveFocus();
  });

  it("persists a trimmed reason and replaces the row locally even when summary refresh fails", async () => {
    const actor = userEvent.setup();
    const summaryRefresh = vi.fn().mockRejectedValue(new Error("summary failed"));
    const updated = canonical({
      status: "blocked",
      status_reason: "Policy request",
      status_origin: "admin",
      version: 2,
      updated_at: "2026-09-02T10:05:00Z",
    });
    const setStatus = vi.spyOn(api, "setStatus").mockResolvedValue({
      data: { user_id: alice.user_id, status: "blocked", reason: "Policy request", user: updated },
      etag: '"2"',
      requestId: "status-request",
      status: 200,
    });
    render(<UsersHarness summaryRefresh={summaryRefresh} />);

    await actor.click(screen.getByRole("button", { name: "Block Alice Example" }));
    await actor.type(screen.getByLabelText(/Reason/), "  Policy request  ");
    await actor.click(screen.getByRole("button", { name: "Block user" }));

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(setStatus).toHaveBeenCalledWith(cfg, session, alice, "blocked", "Policy request");
    expect(screen.getByText("blocked", { selector: ".status-badge" })).toBeInTheDocument();
    expect(screen.getByText("Policy request")).toBeInTheDocument();
    expect(screen.getByText("Alice Example is now blocked.")).toBeInTheDocument();
    expect(summaryRefresh).toHaveBeenCalledOnce();
  });

  it("keeps the confirmation open and reports a distinct mutation error", async () => {
    const actor = userEvent.setup();
    vi.spyOn(api, "setStatus").mockRejectedValue(new ApiError("Unavailable", 503, "transaction_unavailable"));
    render(<UsersHarness />);

    await actor.click(screen.getByRole("button", { name: "Block Alice Example" }));
    await actor.type(screen.getByLabelText(/Reason/), "Policy request");
    await actor.click(screen.getByRole("button", { name: "Block user" }));

    expect(await screen.findByText("The quota service is temporarily unavailable. Try again.")).toBeInTheDocument();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getByText("active", { selector: ".status-badge" })).toBeInTheDocument();
  });

  it("rejects a version-conflict payload for a different identity", async () => {
    const actor = userEvent.setup();
    const wrongUser = canonical({ user_id: "tenant/bob", name: "Bob", version: 2 });
    vi.spyOn(api, "setStatus").mockRejectedValue(new ApiError(
      "Changed",
      409,
      "version_conflict",
      { current_user: wrongUser },
      "conflict-request",
    ));
    render(<UsersHarness />);

    await actor.click(screen.getByRole("button", { name: "Block Alice Example" }));
    await actor.type(screen.getByLabelText(/Reason/), "Policy request");
    await actor.click(screen.getByRole("button", { name: "Block user" }));

    expect(await screen.findByText(/version conflict response did not identify the requested user/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Alice Example" })).toBeInTheDocument();
    expect(screen.queryByText("Bob")).not.toBeInTheDocument();
  });
});

describe("independent dashboard refresh state", () => {
  it("keeps only a failed summary cached while successful users and operations stay fresh", async () => {
    const actor = userEvent.setup();
    vi.spyOn(api, "summary")
      .mockResolvedValueOnce(summary)
      .mockRejectedValueOnce(new ApiError("Summary unavailable", 503, "service_unavailable"));
    vi.spyOn(api, "listUsersPage").mockResolvedValue({ users: [alice], next_cursor: null });
    vi.spyOn(api, "operations").mockResolvedValue(operations);
    render(<Dashboard cfg={cfg} onSignOut={vi.fn()} session={session} />);

    await openTab(actor, "Users");
    expect(await screen.findByText("Alice Example")).toBeInTheDocument();
    await actor.click(screen.getByRole("button", { name: "Refresh data" }));

    expect(screen.queryByText("Cached users · refresh failed")).not.toBeInTheDocument();
    expect(await screen.findByRole("button", { name: /until a fresh enforcement summary loads/ })).toBeDisabled();

    await openTab(actor, "Overview");
    expect(await screen.findByText("Cached summary · refresh failed")).toBeInTheDocument();
    expect(screen.getByText("The quota service is temporarily unavailable. Try again.")).toBeInTheDocument();
    expect(within(screen.getByLabelText("Quota summary")).getByText("1")).toBeInTheDocument();

    await openTab(actor, "Operations");
    expect(screen.queryByText(/Cached from/)).not.toBeInTheDocument();
  });

  it("shows users as unavailable rather than as a verified empty population", async () => {
    const actor = userEvent.setup();
    vi.spyOn(api, "summary").mockResolvedValue(summary);
    vi.spyOn(api, "listUsersPage").mockRejectedValue(new ApiError("Users unavailable", 503, "service_unavailable"));
    vi.spyOn(api, "operations").mockResolvedValue(operations);
    render(<Dashboard cfg={cfg} onSignOut={vi.fn()} session={session} />);

    await openTab(actor, "Users");
    expect(await screen.findByText("Users unavailable")).toBeInTheDocument();
    expect(screen.queryByText("No users yet")).not.toBeInTheDocument();

    await openTab(actor, "Overview");
    expect(screen.getByLabelText("Quota summary")).toBeInTheDocument();

    await openTab(actor, "Operations");
    expect(screen.getByRole("heading", { name: "Operations", level: 2 })).toBeInTheDocument();
  });
});


const bob: UserRow = {
  ...alice,
  user_id: "tenant/bob",
  name: "Bob Example",
  status: "blocked",
  status_reason: "Policy request",
  version: 2,
};

function auditFor(user: UserRow): AuditEvent {
  const snapshot = {
    user_id: user.user_id,
    name: user.name,
    status: user.status,
    status_reason: user.status_reason,
    status_origin: user.status_origin,
    version: user.version,
    created_at: user.created_at,
    updated_at: user.updated_at,
    limits: Object.fromEntries((["daily", "weekly", "monthly"] as const).map((period) => {
      const limits = user.limits[period];
      return [period, limits ? {
        usd_micro: limits.usd * 1_000_000,
        input_tokens: limits.input_tokens,
        output_tokens: limits.output_tokens,
      } : null];
    })) as unknown as AuditEvent["after"]["limits"],
  };
  return {
    user_id: user.user_id,
    event_key: `2026-09-02T10:00:00Z#${user.user_id}`,
    event_type: "user.created",
    actor: "admin@example.test",
    auth_method: "jwt",
    reason: "admin user creation",
    request_id: `create-${user.user_id}`,
    created_at: "2026-09-02T10:00:00Z",
    before: null,
    after: snapshot,
  };
}

describe("server-side user pagination", () => {
  it("loads one page, navigates opaque cursor history, and resets cursors for explicit query/status filters", async () => {
    const actor = userEvent.setup();
    const listUsers = vi.spyOn(api, "listUsersPage")
      .mockResolvedValueOnce({ users: [alice], next_cursor: "cursor-one" })
      .mockResolvedValueOnce({ users: [bob], next_cursor: "cursor-two" })
      .mockResolvedValueOnce({ users: [alice], next_cursor: "cursor-one" })
      .mockResolvedValueOnce({ users: [bob], next_cursor: null })
      .mockResolvedValueOnce({ users: [bob], next_cursor: null });
    vi.spyOn(api, "summary").mockResolvedValue(summary);
    vi.spyOn(api, "operations").mockResolvedValue(operations);
    render(<Dashboard cfg={cfg} onSignOut={vi.fn()} session={session} />);
    await openTab(actor, "Users");

    expect(await screen.findByRole("button", { name: "Alice Example" })).toBeInTheDocument();
    expect(listUsers).toHaveBeenCalledTimes(1);
    expect(listUsers.mock.calls[0][2]).toEqual({ limit: 25, cursor: null, status: undefined, query: "" });
    expect(screen.getByText("1 identities on this page", { selector: ".users-panel .panel-heading p" })).toBeInTheDocument();

    await actor.click(screen.getByRole("button", { name: "Next" }));
    expect(await screen.findByRole("button", { name: "Bob Example" })).toBeInTheDocument();
    expect(listUsers.mock.calls[1][2]).toMatchObject({ cursor: "cursor-one" });
    await actor.click(screen.getByRole("button", { name: "Previous" }));
    expect(await screen.findByRole("button", { name: "Alice Example" })).toBeInTheDocument();
    expect(listUsers.mock.calls[2][2]).toMatchObject({ cursor: null });

    await actor.type(screen.getByLabelText("Search users"), "bob");
    expect(listUsers).toHaveBeenCalledTimes(3);
    await actor.click(screen.getByRole("button", { name: "Search" }));
    expect(await screen.findByRole("button", { name: "Bob Example" })).toBeInTheDocument();
    expect(listUsers.mock.calls[3][2]).toMatchObject({ cursor: null, query: "bob" });

    await actor.selectOptions(screen.getByLabelText("Filter users"), "blocked");
    await waitFor(() => expect(listUsers).toHaveBeenCalledTimes(5));
    expect(listUsers.mock.calls[4][2]).toMatchObject({ cursor: null, query: "bob", status: "blocked" });
  });

  it("keeps the current page visible when Next fails", async () => {
    const actor = userEvent.setup();
    vi.spyOn(api, "summary").mockResolvedValue(summary);
    vi.spyOn(api, "operations").mockResolvedValue(operations);
    vi.spyOn(api, "listUsersPage")
      .mockResolvedValueOnce({ users: [alice], next_cursor: "cursor-one" })
      .mockRejectedValueOnce(new ApiError("Unavailable", 503, "service_unavailable"));
    render(<Dashboard cfg={cfg} onSignOut={vi.fn()} session={session} />);
    await openTab(actor, "Users");

    await screen.findByRole("button", { name: "Alice Example" });
    await actor.click(screen.getByRole("button", { name: "Next" }));
    expect(await screen.findByText("The quota service is temporarily unavailable. Try again.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Alice Example" })).toBeInTheDocument();
    expect(screen.getByText("Cached users · refresh failed")).toBeInTheDocument();
  });
});

describe("dashboard operational navigation", () => {
  it("loads global audit only after navigation and opens a current-page target in the detail drawer", async () => {
    const actor = userEvent.setup();
    vi.spyOn(api, "summary").mockResolvedValue(summary);
    vi.spyOn(api, "operations").mockResolvedValue(operations);
    vi.spyOn(api, "listUsersPage").mockResolvedValue({ users: [alice], next_cursor: null });
    const audit = vi.spyOn(api, "listAuditPage").mockResolvedValue({ events: [auditFor(alice)], next_cursor: null });
    vi.spyOn(api, "getUser").mockResolvedValue({ data: { user: canonical(), current_usage: currentUsage }, etag: '"1"', requestId: null, status: 200 });
    render(<Dashboard cfg={cfg} onSignOut={vi.fn()} session={session} />);
    await openTab(actor, "Users");

    await screen.findByRole("button", { name: "Alice Example" });
    expect(audit).not.toHaveBeenCalled();
    await actor.click(screen.getByRole("button", { name: "Audit log" }));
    expect(await screen.findByRole("button", { name: alice.user_id })).toBeInTheDocument();
    await actor.click(screen.getByRole("button", { name: alice.user_id }));
    expect(await screen.findByRole("dialog", { name: "Alice Example" })).toBeInTheDocument();
  });

  it("uses a fresh server-side user search for an audit target outside the current page", async () => {
    const actor = userEvent.setup();
    vi.spyOn(api, "summary").mockResolvedValue(summary);
    vi.spyOn(api, "operations").mockResolvedValue(operations);
    const listUsers = vi.spyOn(api, "listUsersPage")
      .mockResolvedValueOnce({ users: [alice], next_cursor: null })
      .mockResolvedValueOnce({ users: [bob], next_cursor: null });
    vi.spyOn(api, "listAuditPage").mockResolvedValue({ events: [auditFor(bob)], next_cursor: null });
    render(<Dashboard cfg={cfg} onSignOut={vi.fn()} session={session} />);
    await openTab(actor, "Users");

    await screen.findByRole("button", { name: "Alice Example" });
    await actor.click(screen.getByRole("button", { name: "Audit log" }));
    await actor.click(await screen.findByRole("button", { name: bob.user_id }));
    expect(await screen.findByRole("button", { name: "Bob Example" })).toBeInTheDocument();
    expect(listUsers.mock.calls[1][2]).toMatchObject({ cursor: null, query: bob.user_id, status: undefined });
  });
});

describe("create and canonical detail synchronization", () => {
  it("adds a successful create with zero today usage and can open its details", async () => {
    const actor = userEvent.setup();
    const created = canonical({ user_id: "tenant/new", name: "New User", version: 1 });
    const zeroUsage = Object.fromEntries((["daily", "weekly", "monthly"] as const).map((period) => [period, { ...currentUsage[period], cost_usd: 0, input_tokens: 0, output_tokens: 0, requests: 0 }])) as unknown as CurrentUsage;
    vi.spyOn(api, "summary").mockResolvedValue({ ...summary, enforcement: { ...summary.enforcement, total_users: 0 } });
    vi.spyOn(api, "operations").mockResolvedValue(operations);
    vi.spyOn(api, "listUsersPage").mockResolvedValue({ users: [], next_cursor: null });
    vi.spyOn(api, "createUser").mockResolvedValue({ data: { user_id: created.user_id, provisioned: true, limits: created.limits, user: created }, etag: '"1"', requestId: "create-new", status: 200 });
    vi.spyOn(api, "getUser").mockResolvedValue({ data: { user: created, current_usage: zeroUsage }, etag: '"1"', requestId: null, status: 200 });
    render(<Dashboard cfg={cfg} onSignOut={vi.fn()} session={session} />);
    await openTab(actor, "Users");

    await screen.findByText("No users yet");
    await actor.click(screen.getByRole("button", { name: "Create user" }));
    const wizard = screen.getByRole("dialog", { name: "Create user" });
    await actor.type(within(wizard).getByLabelText("User identity claim value"), created.user_id);
    await actor.type(within(wizard).getByLabelText("Display name"), created.name);
    await actor.click(within(wizard).getByRole("button", { name: "Next" }));
    await actor.click(within(wizard).getByRole("button", { name: "Next" }));
    await actor.click(within(wizard).getByRole("button", { name: "Create user" }));

    expect(await screen.findByRole("button", { name: "New User" })).toBeInTheDocument();
    expect(await screen.findByRole("dialog", { name: "New User" })).toHaveTextContent("$0.000000");
    expect(screen.getByText("New User was created.")).toBeInTheDocument();
  });

  it("keeps an open detail drawer synchronized with canonical status mutations", async () => {
    const actor = userEvent.setup();
    const updated = canonical({ status: "blocked", status_reason: "Policy request", version: 2 });
    vi.spyOn(api, "summary").mockResolvedValue(summary);
    vi.spyOn(api, "operations").mockResolvedValue(operations);
    vi.spyOn(api, "listUsersPage").mockResolvedValue({ users: [alice], next_cursor: null });
    vi.spyOn(api, "getUser").mockResolvedValue({ data: { user: canonical(), current_usage: currentUsage }, etag: '"1"', requestId: null, status: 200 });
    vi.spyOn(api, "setStatus").mockResolvedValue({ data: { user_id: alice.user_id, status: "blocked", reason: "Policy request", user: updated }, etag: '"2"', requestId: "status-two", status: 200 });
    render(<Dashboard cfg={cfg} onSignOut={vi.fn()} session={session} />);
    await openTab(actor, "Users");

    await actor.click(await screen.findByRole("button", { name: "Alice Example" }));
    await actor.click(screen.getByRole("button", { name: "Block user" }));
    const statusDialog = screen.getByRole("dialog", { name: "Confirm block" });
    await actor.type(within(statusDialog).getByLabelText(/Reason/), "Policy request");
    await actor.click(within(statusDialog).getByRole("button", { name: "Block user" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Confirm block" })).not.toBeInTheDocument());

    const statuses = screen.getAllByText("blocked", { selector: ".status-badge" });
    expect(statuses).toHaveLength(2);
    expect(screen.getByRole("dialog", { name: "Alice Example" })).toHaveTextContent("Policy request");
  });
});


describe("additional canonical and modal safety", () => {
  it("never lets a late detail response replace a newer canonical configuration", () => {
    const current = { ...alice, status: "blocked" as const, status_reason: "Newer mutation", version: 3 };
    const delayedDetail = canonical({ status: "active", status_reason: "Older detail", version: 2 });

    expect(mergeCanonicalUser([current], delayedDetail)).toEqual([current]);
  });

  it("removes a status mutation that no longer matches the active server-side filter", async () => {
    const actor = userEvent.setup();
    const updated = canonical({ status: "blocked", status_reason: "Policy request", version: 2 });
    vi.spyOn(api, "summary").mockResolvedValue(summary);
    vi.spyOn(api, "operations").mockResolvedValue(operations);
    vi.spyOn(api, "listUsersPage")
      .mockResolvedValueOnce({ users: [alice], next_cursor: null })
      .mockResolvedValueOnce({ users: [alice], next_cursor: null });
    vi.spyOn(api, "setStatus").mockResolvedValue({ data: { user_id: alice.user_id, status: "blocked", reason: "Policy request", user: updated }, etag: '"2"', requestId: "status-filter", status: 200 });
    render(<Dashboard cfg={cfg} onSignOut={vi.fn()} session={session} />);
    await openTab(actor, "Users");

    await screen.findByRole("button", { name: "Alice Example" });
    await actor.selectOptions(screen.getByLabelText("Filter users"), "active");
    await waitFor(() => expect(screen.getByLabelText("Filter users")).toHaveValue("active"));
    await actor.click(screen.getByRole("button", { name: "Block Alice Example" }));
    await actor.type(screen.getByLabelText(/Reason/), "Policy request");
    await actor.click(screen.getByRole("button", { name: "Block user" }));

    expect(await screen.findByText("No matching users")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Alice Example" })).not.toBeInTheDocument();
  });

  it("suspends the drawer so Escape closes only a nested status dialog", async () => {
    const actor = userEvent.setup();
    vi.spyOn(api, "summary").mockResolvedValue(summary);
    vi.spyOn(api, "operations").mockResolvedValue(operations);
    vi.spyOn(api, "listUsersPage").mockResolvedValue({ users: [alice], next_cursor: null });
    vi.spyOn(api, "getUser").mockResolvedValue({ data: { user: canonical(), current_usage: currentUsage }, etag: '"1"', requestId: null, status: 200 });
    render(<Dashboard cfg={cfg} onSignOut={vi.fn()} session={session} />);
    await openTab(actor, "Users");

    const opener = await screen.findByRole("button", { name: "Alice Example" });
    await actor.click(opener);
    const drawerAction = screen.getByRole("button", { name: "Block user" });
    await actor.click(drawerAction);
    expect(screen.getByRole("dialog", { name: "Confirm block" })).toBeInTheDocument();
    await actor.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "Confirm block" })).not.toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: "Alice Example" })).toBeInTheDocument();
    expect(drawerAction).toHaveFocus();
    await actor.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "Alice Example" })).not.toBeInTheDocument();
    expect(opener).toHaveFocus();
  });
});


describe("filtered detail and precision edge cases", () => {
  it("does not evict a newer filtered row when an older detail response has another status", async () => {
    const actor = userEvent.setup();
    const current = { ...bob, version: 3 };
    let resolveDetail!: (value: { data: { user: AdminUser; current_usage: CurrentUsage }; etag: string; requestId: null; status: number }) => void;
    const delayedDetail = new Promise<{ data: { user: AdminUser; current_usage: CurrentUsage }; etag: string; requestId: null; status: number }>((resolve) => { resolveDetail = resolve; });
    vi.spyOn(api, "summary").mockResolvedValue(summary);
    vi.spyOn(api, "operations").mockResolvedValue(operations);
    vi.spyOn(api, "listUsersPage").mockResolvedValue({ users: [current], next_cursor: null });
    vi.spyOn(api, "getUser").mockReturnValue(delayedDetail);
    render(<Dashboard cfg={cfg} onSignOut={vi.fn()} session={session} />);
    await openTab(actor, "Users");

    await screen.findByRole("button", { name: "Bob Example" });
    await actor.selectOptions(screen.getByLabelText("Filter users"), "blocked");
    await actor.click(await screen.findByRole("button", { name: "Bob Example" }));
    resolveDetail({ data: { user: canonical({ user_id: bob.user_id, name: bob.name, status: "active", version: 2 }), current_usage: currentUsage }, etag: '"2"', requestId: null, status: 200 });

    await waitFor(() => expect(screen.getByRole("button", { name: "Bob Example" })).toBeInTheDocument());
    expect(screen.getAllByText("blocked", { selector: ".status-badge" })).toHaveLength(2);
  });

  it("supports micro-dollar limits in the edit form without native step rejection", async () => {
    const actor = userEvent.setup();
    const onSave = vi.fn();
    render(<LimitsDialog apiError="" busy={false} onClose={vi.fn()} onSave={onSave} user={{
      ...alice,
      limits: { ...alice.limits, daily: { ...alice.limits.daily!, usd: 0.123457 } },
      today: { ...alice.today, cost_usd: 0.1 },
      current_usage: { ...alice.current_usage, daily: { ...alice.current_usage.daily, cost_usd: 0.1 } },
    }} />);

    const usd = screen.getByLabelText("Daily USD limit");
    expect(usd).toHaveAttribute("step", "0.000001");
    expect((usd as HTMLInputElement).checkValidity()).toBe(true);
    const input = screen.getByLabelText("Daily input token limit");
    await actor.clear(input);
    await actor.type(input, "101");
    await actor.click(screen.getByRole("button", { name: "Save limits" }));
    expect(onSave).toHaveBeenCalledWith({
      limits: {
        daily: { usd: 0.123457, input_tokens: 101, output_tokens: 20 },
        weekly: alice.limits.weekly,
        monthly: null,
      },
    });
  });

  it("disables drawer status changes until a fresh enforcement summary is available", async () => {
    const actor = userEvent.setup();
    vi.spyOn(api, "summary").mockRejectedValue(new ApiError("Summary unavailable", 503, "service_unavailable"));
    vi.spyOn(api, "operations").mockResolvedValue(operations);
    vi.spyOn(api, "listUsersPage").mockResolvedValue({ users: [alice], next_cursor: null });
    vi.spyOn(api, "getUser").mockResolvedValue({ data: { user: canonical(), current_usage: currentUsage }, etag: '"1"', requestId: null, status: 200 });
    render(<Dashboard cfg={cfg} onSignOut={vi.fn()} session={session} />);
    await openTab(actor, "Users");

    await actor.click(await screen.findByRole("button", { name: "Alice Example" }));
    const drawer = screen.getByRole("dialog", { name: "Alice Example" });
    expect(within(drawer).getByRole("button", { name: /Status change unavailable/ })).toBeDisabled();
    expect(screen.queryByRole("dialog", { name: "Confirm block" })).not.toBeInTheDocument();
  });
});

describe("workload mode", () => {
  const paymentsWorkload: UserRow = {
    ...alice,
    user_id: "workload:payments",
    name: "payments",
    granularity: "workload",
    enforcement_ready: true,
  };
  const reportsWorkload: UserRow = {
    ...alice,
    user_id: "workload:reports",
    name: "reports",
    granularity: "workload",
    enforcement_ready: false,
  };

  it("filters by granularity and renders workload badges with enforcement state", async () => {
    const actor = userEvent.setup();
    const listUsers = vi.spyOn(api, "listUsersPage")
      .mockResolvedValueOnce({ users: [alice, paymentsWorkload, reportsWorkload], next_cursor: null })
      .mockResolvedValueOnce({ users: [paymentsWorkload, reportsWorkload], next_cursor: null });
    vi.spyOn(api, "summary").mockResolvedValue(summary);
    vi.spyOn(api, "operations").mockResolvedValue(operations);
    render(<Dashboard cfg={cfg} onSignOut={vi.fn()} session={session} />);
    await openTab(actor, "Users");

    expect(await screen.findByRole("button", { name: "Alice Example" })).toBeInTheDocument();
    expect(screen.getAllByText("workload")).toHaveLength(2);
    // Only the role-less workload warns that it cannot be hard-blocked.
    expect(screen.getAllByText("metering only")).toHaveLength(1);

    await actor.selectOptions(screen.getByLabelText("Filter users"), "workloads");
    await waitFor(() => expect(listUsers).toHaveBeenCalledTimes(2));
    expect(listUsers.mock.calls[1][2]).toMatchObject({
      cursor: null,
      granularity: "workload",
      status: undefined,
    });
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Alice Example" })).not.toBeInTheDocument(),
    );
  });
});
