import type { components } from "./api-types";
import { ApiError } from "./api-client";

type Schemas = components["schemas"];
type RiskSession = Schemas["RiskSession"];
type RiskAssessment = Schemas["RiskAssessment"];
type MandateIntent = Schemas["MandateIntent"];

export const MERCHANT_NAMESPACE = "demo_merchant";
export const DEVICE_FINGERPRINT_KEY = "mg:demo_device_fingerprint";
export const CHALLENGE_CONTEXT_KEY = "mg:challenge_context";

export type CheckoutKind =
  | "idle"
  | "ready"
  | "checking"
  | "allowed"
  | "challenged"
  | "blocked"
  | "error_precheck_unavailable"
  | "error_session"
  | "error_network";

export interface CheckoutState {
  kind: CheckoutKind;
  session?: RiskSession;
  assessment?: RiskAssessment;
  message?: string;
}

export interface ChallengeContext {
  risk_session_id: string;
  score: number;
  engine_version: string;
  assessment_id: string;
}

function readStorage(key: string): string | null {
  if (typeof sessionStorage === "undefined") {
    return null;
  }
  try {
    return sessionStorage.getItem(key);
  } catch {
    return null;
  }
}

function writeStorage(key: string, value: string): void {
  if (typeof sessionStorage === "undefined") {
    return;
  }
  try {
    sessionStorage.setItem(key, value);
  } catch {
    // Storage is best-effort; the in-memory state machine stays authoritative.
  }
}

export function makeCheckoutOrderRef(): string {
  return `demo-${crypto.randomUUID().replace(/-/g, "").slice(0, 8)}`;
}

export function makeDeviceFingerprint(): string {
  const existing = readStorage(DEVICE_FINGERPRINT_KEY);
  if (existing) {
    return existing;
  }
  const fresh = crypto.randomUUID();
  writeStorage(DEVICE_FINGERPRINT_KEY, fresh);
  return fresh;
}

export function makeMandateIntent(now: Date = new Date()): MandateIntent {
  const expireAt = new Date(now.getTime() + 365 * 24 * 60 * 60 * 1000);
  return {
    max_amount_paise: 100000,
    frequency: "monthly",
    expire_at: expireAt.toISOString(),
  };
}

export function makeFlowTimestamps(now: Date = new Date()): { flowStartedAt: string } {
  return { flowStartedAt: now.toISOString() };
}

export function writeChallengeContext(context: ChallengeContext): void {
  writeStorage(CHALLENGE_CONTEXT_KEY, JSON.stringify(context));
}

export function readChallengeContext(): ChallengeContext | null {
  const raw = readStorage(CHALLENGE_CONTEXT_KEY);
  if (!raw) {
    return null;
  }
  try {
    const parsed = JSON.parse(raw) as Partial<ChallengeContext>;
    if (
      typeof parsed.risk_session_id === "string" &&
      typeof parsed.score === "number" &&
      typeof parsed.engine_version === "string" &&
      typeof parsed.assessment_id === "string"
    ) {
      return {
        risk_session_id: parsed.risk_session_id,
        score: parsed.score,
        engine_version: parsed.engine_version,
        assessment_id: parsed.assessment_id,
      };
    }
    return null;
  } catch {
    return null;
  }
}

export function clearChallengeContext(): void {
  if (typeof sessionStorage === "undefined") {
    return;
  }
  try {
    sessionStorage.removeItem(CHALLENGE_CONTEXT_KEY);
  } catch {
    // Best-effort only.
  }
}

export function startCheckout(): CheckoutState {
  return { kind: "idle" };
}

export function sessionCreated(session: RiskSession): CheckoutState {
  return { kind: "ready", session };
}

export function telemetryRecorded(state: CheckoutState, session: RiskSession): CheckoutState {
  if (state.kind !== "ready" || !state.session) {
    return {
      kind: "error_session",
      session: state.session,
      message: "Risk check was requested out of order.",
    };
  }
  return { kind: "checking", session };
}

export function precheckResolved(
  state: CheckoutState,
  assessment: RiskAssessment
): CheckoutState {
  if (state.kind !== "checking" || !state.session) {
    return {
      kind: "error_session",
      session: state.session,
      message: "Risk check was requested out of order.",
    };
  }
  if (assessment.risk_session_id !== state.session.risk_session_id) {
    return {
      kind: "error_session",
      session: state.session,
      message: "Risk check answer did not match this checkout.",
    };
  }
  switch (assessment.decision) {
    case "ALLOW":
      return { kind: "allowed", session: state.session, assessment };
    case "CHALLENGE":
      return { kind: "challenged", session: state.session, assessment };
    case "BLOCK":
      return { kind: "blocked", session: state.session, assessment };
    default:
      return {
        kind: "error_session",
        session: state.session,
        message: "Risk check returned an unknown decision.",
      };
  }
}

export function precheckFailed(state: CheckoutState, error: unknown): CheckoutState {
  if (error instanceof ApiError && error.kind === "unavailable") {
    return {
      kind: "error_precheck_unavailable",
      session: state.session,
      message: "Risk pre-check is unavailable.",
    };
  }
  if (error instanceof ApiError && (error.kind === "network" || error.kind === "timeout")) {
    return {
      kind: "error_network",
      session: state.session,
      message: error.kind === "timeout" ? "Request timed out." : "Network error.",
    };
  }
  if (error instanceof ApiError) {
    return {
      kind: "error_session",
      session: state.session,
      message: `Checkout session problem (HTTP ${error.status}).`,
    };
  }
  return {
    kind: "error_network",
    session: state.session,
    message: "Network error.",
  };
}

export function reset(): CheckoutState {
  clearChallengeContext();
  return { kind: "idle" };
}
