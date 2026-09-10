import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { EmergencyStopCard, EnforcementDialCard, OperationsView, leaseWindowLabel } from "./Operations";
import { ApiError, api, type EnforcementConfig, type Operations } from "./api";
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

const enforcement: EnforcementConfig = {
  permission_lease_seconds: 300,
  source: "deployment_default",
  generation: 0,
  actor: "",
  reason: "",
  updated_at: null,
  valid_permission_lease_seconds: [60, 300, 900],
  default_permission_lease_seconds: 300,
};

const operations: Operations = {
  as_of: "2026-09-09T10:00:00Z",
  configuration: {
    mode: "layered",
    credential_ttl_seconds: 900,
    permission_lease_seconds: 300,
    permission_lease_enabled: true,
    permission_lease_source: "runtime",
    effective_permission_lease_seconds: 300,
    post_detection_fallback_seconds: 900,
    refresh_overlap_seconds: 10,
    refresh_jitter_seconds: 5,
    vend_rate_limit_per_minute: 6,
    revocation_enabled: true,
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
    detection_lag_timestamp: "2026-09-09T09:59:00Z",
    telemetry_status: "complete",
    last_reconciliation_at: "2026-09-09T09:58:00Z",
    reconciliation_status: "current",
    revoked_identities_desired: 0,
    recent_sync_failure_count: 0,
    recent_overflow_count: 0,
    recent_emergency_failure_count: 0,
    window_minutes: 15,
  },
  alarms: [
    { key: "enforcement_dispatch_dlq", state: "OK", updated_at: "2026-09-09T09:00:00Z" },
  ],
  cloudwatch: { status: "available" },
};

describe("lease window labels", () => {
  it("renders minutes for round values and seconds otherwise", () => {
    expect(leaseWindowLabel(60)).toBe("1 minute");
    expect(leaseWindowLabel(300)).toBe("5 minutes");
    expect(leaseWindowLabel(900)).toBe("15 minutes");
    expect(leaseWindowLabel(90)).toBe("90 seconds");
  });
});

describe("enforcement dial", () => {
  it("offers only the gateway-provided windows and applies a change with the reason", async () => {
    const actor = userEvent.setup();
    vi.spyOn(api, "getEnforcement").mockResolvedValue(enforcement);
    const put = vi.spyOn(api, "setEnforcement").mockResolvedValue({
      permission_lease_seconds: 60,
      source: "runtime",
      generation: 1,
      actor: "admin@example.test",
      reason: "demo",
      updated_at: "2026-09-09T10:05:00Z",
    });
    const onApplied = vi.fn();
    render(<EnforcementDialCard cfg={cfg} onApplied={onApplied} session={session} />);

    const group = await screen.findByRole("radiogroup", { name: "Permission lease window" });
    const radios = within(group).getAllByRole("radio");
    expect(radios.map((radio) => radio.textContent)).toEqual([
      "1 minute",
      "5 minutesdeployment default",
      "15 minutes",
    ]);
    expect(within(group).getByRole("radio", { name: /^5 minutes/ })).toHaveAttribute("aria-checked", "true");
    // No pending change: no Apply button visible yet.
    expect(screen.queryByRole("button", { name: /Apply/ })).not.toBeInTheDocument();

    await actor.click(within(group).getByRole("radio", { name: "1 minute" }));
    expect(screen.getByRole("button", { name: "Apply 1 minute lease" })).toBeDisabled();
    await actor.type(screen.getByLabelText("Reason for lease window change"), "demo");
    await actor.click(screen.getByRole("button", { name: "Apply 1 minute lease" }));

    expect(put).toHaveBeenCalledWith(cfg, session, 60, "demo", 0);
    expect(await screen.findByText("Permission lease is now 1 minute for newly vended credentials.")).toBeInTheDocument();
    expect(onApplied).toHaveBeenCalledTimes(1);
    // The status chip reflects the runtime source after the change.
    expect(screen.getByText("1 minute · runtime dial")).toBeInTheDocument();
  });

  it("keeps the pending selection and shows the gateway validation error", async () => {
    const actor = userEvent.setup();
    vi.spyOn(api, "getEnforcement").mockResolvedValue(enforcement);
    vi.spyOn(api, "setEnforcement").mockRejectedValue(
      new ApiError("permission_lease_seconds must be one of 60, 300, 900.", 400, "invalid_request_error"),
    );
    const onApplied = vi.fn();
    render(<EnforcementDialCard cfg={cfg} onApplied={onApplied} session={session} />);

    const group = await screen.findByRole("radiogroup", { name: "Permission lease window" });
    await actor.click(within(group).getByRole("radio", { name: "15 minutes" }));
    await actor.type(screen.getByLabelText("Reason for lease window change"), "validation test");
    await actor.click(screen.getByRole("button", { name: "Apply 15 minutes lease" }));

    expect(await screen.findByText(/must be one of 60, 300, 900/)).toBeInTheDocument();
    expect(within(group).getByRole("radio", { name: "15 minutes" })).toHaveAttribute("aria-checked", "true");
    expect(onApplied).not.toHaveBeenCalled();
  });

  it("reconciles a version conflict to the latest runtime dial", async () => {
    const actor = userEvent.setup();
    vi.spyOn(api, "getEnforcement").mockResolvedValue(enforcement);
    vi.spyOn(api, "setEnforcement").mockRejectedValue(
      new ApiError("Changed", 409, "version_conflict", {
        current_enforcement: {
          ...enforcement,
          permission_lease_seconds: 900,
          source: "runtime",
          generation: 4,
        },
      }),
    );
    render(<EnforcementDialCard cfg={cfg} onApplied={vi.fn()} session={session} />);

    const group = await screen.findByRole("radiogroup", { name: "Permission lease window" });
    await actor.click(within(group).getByRole("radio", { name: "1 minute" }));
    await actor.type(screen.getByLabelText("Reason for lease window change"), "stale change");
    await actor.click(screen.getByRole("button", { name: "Apply 1 minute lease" }));

    expect(await screen.findByText(/changed in another session/)).toBeInTheDocument();
    expect(within(group).getByRole("radio", { name: "15 minutes" })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByText("15 minutes · runtime dial")).toBeInTheDocument();
  });

  it("supports arrow-key navigation as a single accessible radio group", async () => {
    const actor = userEvent.setup();
    vi.spyOn(api, "getEnforcement").mockResolvedValue(enforcement);
    render(<EnforcementDialCard cfg={cfg} onApplied={vi.fn()} session={session} />);

    const group = await screen.findByRole("radiogroup", { name: "Permission lease window" });
    const current = within(group).getByRole("radio", { name: /^5 minutes/ });
    current.focus();
    await actor.keyboard("{ArrowRight}");

    const next = within(group).getByRole("radio", { name: "15 minutes" });
    expect(next).toHaveAttribute("aria-checked", "true");
    expect(next).toHaveFocus();
    expect(current).toHaveAttribute("tabindex", "-1");
    expect(next).toHaveAttribute("tabindex", "0");
  });

  it("recovers from a failed load with Retry", async () => {
    const actor = userEvent.setup();
    vi.spyOn(api, "getEnforcement")
      .mockRejectedValueOnce(new ApiError("Unavailable", 503, "service_unavailable"))
      .mockResolvedValueOnce(enforcement);
    render(<EnforcementDialCard cfg={cfg} onApplied={vi.fn()} session={session} />);

    expect(await screen.findByText(/temporarily unavailable/)).toBeInTheDocument();
    await actor.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("radiogroup", { name: "Permission lease window" })).toBeInTheDocument();
  });
});

describe("emergency stop", () => {
  it("requires the key, the exact phrase, and a reason before stopping all sessions", async () => {
    const actor = userEvent.setup();
    const post = vi.spyOn(api, "setEmergencyStop").mockResolvedValue({
      state: "activating",
      desired_active: true,
      generation: 1,
      requested_at: "2026-09-09T10:06:00Z",
      idempotent: false,
      retry: false,
    });
    const onApplied = vi.fn();
    render(<EmergencyStopCard cfg={cfg} emergency={operations.emergency} onApplied={onApplied} session={session} />);

    await actor.click(screen.getByRole("button", { name: "Activate emergency stop" }));
    const dialog = screen.getByRole("dialog", { name: "Activate emergency stop" });
    const submit = within(dialog).getByRole("button", { name: "Stop all sessions" });
    expect(submit).toBeDisabled();

    await actor.type(within(dialog).getByLabelText("Emergency key"), "break-glass-secret");
    await actor.type(within(dialog).getByLabelText("Emergency reason"), "incident 4711");
    expect(submit).toBeDisabled();
    await actor.type(within(dialog).getByLabelText("Confirmation phrase"), "STOP_ALL_BEDROCK");
    expect(submit).toBeDisabled();
    await actor.type(within(dialog).getByLabelText("Confirmation phrase"), "_SESSIONS");
    expect(submit).toBeEnabled();
    await actor.click(submit);

    expect(post).toHaveBeenCalledWith(cfg, session, {
      action: "activate",
      confirmation: "STOP_ALL_BEDROCK_SESSIONS",
      reason: "incident 4711",
      emergencyKey: "break-glass-secret",
    });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(await screen.findByText(/Emergency stop requested/)).toBeInTheDocument();
    expect(onApplied).toHaveBeenCalledTimes(1);
  });

  it("offers recovery with its own phrase when the stop is active", async () => {
    const actor = userEvent.setup();
    vi.spyOn(api, "setEmergencyStop").mockResolvedValue({
      state: "recovering",
      desired_active: false,
      generation: 2,
      requested_at: "2026-09-09T10:07:00Z",
    });
    render(
      <EmergencyStopCard
        cfg={cfg}
        emergency={{ ...operations.emergency, state: "active", desired_active: true, converged: true }}
        onApplied={vi.fn()}
        session={session}
      />,
    );

    await actor.click(screen.getByRole("button", { name: "Recover from emergency stop" }));
    const dialog = screen.getByRole("dialog", { name: "Recover from emergency stop" });
    expect(within(dialog).getByText("RESTORE_ALL_BEDROCK_SESSIONS", { selector: "code" })).toBeInTheDocument();
  });

  it("keeps the dialog open and surfaces a rejected break-glass key", async () => {
    const actor = userEvent.setup();
    vi.spyOn(api, "setEmergencyStop").mockRejectedValue(
      new ApiError("Break-glass emergency authorization required.", 403, "forbidden"),
    );
    render(<EmergencyStopCard cfg={cfg} emergency={operations.emergency} onApplied={vi.fn()} session={session} />);

    await actor.click(screen.getByRole("button", { name: "Activate emergency stop" }));
    const dialog = screen.getByRole("dialog", { name: "Activate emergency stop" });
    await actor.type(within(dialog).getByLabelText("Emergency key"), "wrong-key");
    await actor.type(within(dialog).getByLabelText("Confirmation phrase"), "STOP_ALL_BEDROCK_SESSIONS");
    await actor.type(within(dialog).getByLabelText("Emergency reason"), "incident");
    await actor.click(within(dialog).getByRole("button", { name: "Stop all sessions" }));

    expect(await within(dialog).findByText(/not authorized/)).toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: "Activate emergency stop" })).toBeInTheDocument();
  });
});

describe("operations view", () => {
  it("renders controls, health cards, and alarms together", async () => {
    vi.spyOn(api, "getEnforcement").mockResolvedValue(enforcement);
    render(
      <OperationsView
        cfg={cfg}
        error=""
        loading={false}
        onChanged={vi.fn()}
        operations={operations}
        session={session}
        stale={false}
      />,
    );

    expect(await screen.findByRole("radiogroup", { name: "Permission lease window" })).toBeInTheDocument();
    expect(screen.getByText("Permission lease dial")).toBeInTheDocument();
    expect(screen.getByText("Emergency stop", { selector: "#emergency-title" })).toBeInTheDocument();
    expect(screen.getByText("Always on · 19 shards")).toBeInTheDocument();
    expect(screen.getByText("5 min · runtime dial")).toBeInTheDocument();
    expect(within(screen.getByLabelText("Operational alarms")).getByText(/enforcement dispatch dlq/)).toBeInTheDocument();
  });

  it("keeps the controls reachable when operational telemetry is unavailable", async () => {
    vi.spyOn(api, "getEnforcement").mockResolvedValue(enforcement);
    render(
      <OperationsView
        cfg={cfg}
        error="Operations unavailable"
        loading={false}
        onChanged={vi.fn()}
        operations={null}
        session={session}
        stale={false}
      />,
    );

    expect(screen.getByText("Operations unavailable")).toBeInTheDocument();
    expect(await screen.findByRole("radiogroup", { name: "Permission lease window" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Activate emergency stop" })).toBeDisabled();
  });
});
