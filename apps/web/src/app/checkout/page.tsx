"use client";

import { useEffect, useReducer, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense } from "react";
import {
  createRiskSession,
  recordTelemetry,
  requestPrecheck,
} from "../../lib/api-client";
import {
  MERCHANT_NAMESPACE,
  makeCheckoutOrderRef,
  makeDeviceFingerprint,
  makeFlowTimestamps,
  makeMandateIntent,
  precheckFailed,
  precheckResolved,
  reset,
  sessionCreated,
  startCheckout,
  telemetryRecorded,
  writeChallengeContext,
  type CheckoutState,
} from "../../lib/risk-session-state";
import { truncateId } from "../../lib/masking";
import MandateSummary from "../../components/checkout/MandateSummary";
import DecisionBanner from "../../components/checkout/DecisionBanner";

type Action =
  | { type: "created"; session: NonNullable<CheckoutState["session"]> }
  | { type: "telemetry"; session: NonNullable<CheckoutState["session"]> }
  | {
      type: "resolved";
      assessment: NonNullable<CheckoutState["assessment"]>;
    }
  | { type: "failed"; error: unknown }
  | { type: "retry" }
  | { type: "restart" };

function reducer(state: CheckoutState, action: Action): CheckoutState {
  switch (action.type) {
    case "created":
      return sessionCreated(action.session);
    case "telemetry":
      return telemetryRecorded(state, action.session);
    case "resolved":
      return precheckResolved(state, action.assessment);
    case "failed":
      return precheckFailed(state, action.error);
    case "retry":
      return state.session
        ? { kind: "checking", session: state.session }
        : { kind: "error_session", message: "No checkout session to retry." };
    case "restart":
      return reset();
  }
}

export default function CheckoutPage() {
  return (
    <Suspense>
      <CheckoutContent />
    </Suspense>
  );
}

function CheckoutContent() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const merchantNamespace = searchParams.get("merchant") || MERCHANT_NAMESPACE;
  const [state, dispatch] = useReducer(reducer, undefined, startCheckout);
  const [customerReference, setCustomerReference] = useState("");
  const [orderRef, setOrderRef] = useState("");
  const [flowStartedAt, setFlowStartedAt] = useState("");
  const [deviceFingerprint, setDeviceFingerprint] = useState("");
  const [showHandoff, setShowHandoff] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setOrderRef(makeCheckoutOrderRef());
    setFlowStartedAt(makeFlowTimestamps().flowStartedAt);
    setDeviceFingerprint(makeDeviceFingerprint());
  }, []);

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    if (busy || state.kind !== "idle" || !customerReference.trim()) {
      return;
    }
    setBusy(true);
    try {
      const { session } = await createRiskSession({
        merchant_namespace: merchantNamespace,
        checkout_order_ref: orderRef,
        customer_reference: customerReference.trim(),
        device_fingerprint: deviceFingerprint,
        flow_started_at: flowStartedAt,
        mandate_intent: makeMandateIntent(),
      });
      dispatch({ type: "created", session });
    } catch (error) {
      dispatch({ type: "failed", error });
    } finally {
      setBusy(false);
    }
  }

  async function runTelemetryAndPrecheck(sessionId: string) {
    setBusy(true);
    try {
      const updated = await recordTelemetry(sessionId, {
        device_fingerprint: deviceFingerprint,
        flow_completed_at: new Date().toISOString(),
        client_user_agent: null,
        client_ip: null,
        is_vpn_claimed: null,
      });
      dispatch({ type: "telemetry", session: updated });
      const assessment = await requestPrecheck(sessionId);
      dispatch({ type: "resolved", assessment });
      if (assessment.decision === "CHALLENGE") {
        writeChallengeContext({
          risk_session_id: sessionId,
          score: assessment.score,
          engine_version: assessment.engine_version,
          assessment_id: assessment.assessment_id ?? "",
        });
        router.push(`/challenge?session=${sessionId}`);
      }
    } catch (error) {
      dispatch({ type: "failed", error });
    } finally {
      setBusy(false);
    }
  }

  function handleContinue() {
    if (busy || state.kind !== "ready" || !state.session?.risk_session_id) {
      return;
    }
    void runTelemetryAndPrecheck(state.session.risk_session_id);
  }

  async function handleRetry() {
    if (busy || !state.session?.risk_session_id) {
      return;
    }
    const sessionId = state.session.risk_session_id;
    dispatch({ type: "retry" });
    setBusy(true);
    try {
      const assessment = await requestPrecheck(sessionId);
      dispatch({ type: "resolved", assessment });
      if (assessment.decision === "CHALLENGE") {
        writeChallengeContext({
          risk_session_id: sessionId,
          score: assessment.score,
          engine_version: assessment.engine_version,
          assessment_id: assessment.assessment_id ?? "",
        });
        router.push(`/challenge?session=${sessionId}`);
      }
    } catch (error) {
      dispatch({ type: "failed", error });
    } finally {
      setBusy(false);
    }
  }

  function handleStartOver() {
    dispatch({ type: "restart" });
    setCustomerReference("");
    setShowHandoff(false);
    setOrderRef(makeCheckoutOrderRef());
    setFlowStartedAt(makeFlowTimestamps().flowStartedAt);
  }

  return (
    <main className="mx-auto max-w-xl space-y-4 p-6">
      <h1 className="text-xl font-bold">Checkout (demo)</h1>
      <MandateSummary
        intent={makeMandateIntent()}
        orderRef={orderRef === "" ? null : orderRef}
      />

      {state.kind === "idle" && (
        <form onSubmit={handleSubmit} className="space-y-3 rounded border border-slate-200 bg-white p-4">
          <label htmlFor="customer-reference" className="block text-sm text-slate-700">
            Customer reference (demo)
          </label>
          <input
            id="customer-reference"
            value={customerReference}
            onChange={(event) => setCustomerReference(event.target.value)}
            required
            maxLength={256}
            className="block w-full rounded border border-slate-300 px-2 py-1"
          />
          <button
            type="submit"
            disabled={busy || customerReference.trim() === ""}
            className="rounded bg-slate-800 px-4 py-2 text-white disabled:opacity-50"
          >
            Save details
          </button>
        </form>
      )}

      {state.kind === "ready" && (
        <div className="rounded border border-slate-200 bg-white p-4">
          <p className="text-sm text-slate-700">Details saved — continue to risk check</p>
          {state.session?.risk_session_id && (
            <p className="demo-note">Session {truncateId(state.session.risk_session_id)}</p>
          )}
          <button
            type="button"
            onClick={handleContinue}
            disabled={busy}
            className="mt-2 rounded bg-slate-800 px-4 py-2 text-white disabled:opacity-50"
          >
            Continue
          </button>
        </div>
      )}

      {state.kind === "checking" && (
        <p role="status" className="text-sm text-slate-600">
          Checking registration signals…
        </p>
      )}

      <DecisionBanner
        state={state}
        onRetry={handleRetry}
        onStartOver={handleStartOver}
        onContinueToHandoff={() => setShowHandoff(true)}
        showHandoff={showHandoff}
      />
    </main>
  );
}
