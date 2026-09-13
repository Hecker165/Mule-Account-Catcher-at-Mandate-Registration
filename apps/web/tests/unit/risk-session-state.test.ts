import { describe, expect, it } from "vitest";
import type { components } from "../../src/lib/api-types";
import { ApiError } from "../../src/lib/api-client";
import {
  precheckFailed,
  precheckResolved,
  reset,
  sessionCreated,
  startCheckout,
  telemetryRecorded,
} from "../../src/lib/risk-session-state";

type Schemas = components["schemas"];
type RiskSession = Schemas["RiskSession"];
type RiskAssessment = Schemas["RiskAssessment"];

const SESSION_ID = "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d";

function session(): RiskSession {
  return { risk_session_id: SESSION_ID } as RiskSession;
}

function assessment(decision: string): RiskAssessment {
  return {
    assessment_id: "d4e5f6a7-b8c9-4d0e-1f2a-3b4c5d6e7f80",
    risk_session_id: SESSION_ID,
    decision,
  } as RiskAssessment;
}

describe("risk-session-state", () => {
  it("test_checkout_starts_idle_and_transitions_to_ready", () => {
    const idle = startCheckout();
    expect(idle.kind).toBe("idle");
    const ready = sessionCreated(session());
    expect(ready.kind).toBe("ready");
    expect(ready.session?.risk_session_id).toBe(SESSION_ID);
  });

  it("test_telemetry_then_precheck_transitions_to_allowed_on_allow", () => {
    const ready = sessionCreated(session());
    const checking = telemetryRecorded(ready, session());
    expect(checking.kind).toBe("checking");
    const allowed = precheckResolved(checking, assessment("ALLOW"));
    expect(allowed.kind).toBe("allowed");
    expect(allowed.assessment?.decision).toBe("ALLOW");
  });

  it("test_precheck_challenge_transitions_to_challenged", () => {
    const checking = telemetryRecorded(sessionCreated(session()), session());
    const challenged = precheckResolved(checking, assessment("CHALLENGE"));
    expect(challenged.kind).toBe("challenged");
  });

  it("test_precheck_503_transitions_to_error_precheck_unavailable", () => {
    const checking = telemetryRecorded(sessionCreated(session()), session());
    const failed = precheckFailed(checking, new ApiError(503, "unavailable", "down"));
    expect(failed.kind).toBe("error_precheck_unavailable");
    expect(failed.session?.risk_session_id).toBe(SESSION_ID);
  });

  it("test_blocked_assessment_is_rendered_not_hidden", () => {
    const checking = telemetryRecorded(sessionCreated(session()), session());
    const blocked = precheckResolved(checking, assessment("BLOCK"));
    expect(blocked.kind).toBe("blocked");
    expect(blocked.assessment?.decision).toBe("BLOCK");
  });

  it("test_reset_returns_to_idle_and_clears_context", () => {
    const allowed = precheckResolved(
      telemetryRecorded(sessionCreated(session()), session()),
      assessment("ALLOW")
    );
    expect(allowed.kind).toBe("allowed");
    const idle = reset();
    expect(idle).toEqual({ kind: "idle" });
  });

  it("test_no_transition_skips_the_telemetry_step", () => {
    const idle = startCheckout();
    const direct = precheckResolved(idle, assessment("ALLOW"));
    expect(direct.kind).toBe("error_session");
    const ready = sessionCreated(session());
    const skipped = precheckResolved(ready, assessment("ALLOW"));
    expect(skipped.kind).toBe("error_session");
  });
});
