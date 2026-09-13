import type { CheckoutState } from "../../lib/risk-session-state";
import { truncateId } from "../../lib/masking";

interface DecisionBannerProps {
  state: CheckoutState;
  onRetry: () => void;
  onStartOver: () => void;
  onContinueToHandoff: () => void;
  showHandoff: boolean;
}

export default function DecisionBanner({
  state,
  onRetry,
  onStartOver,
  onContinueToHandoff,
  showHandoff,
}: DecisionBannerProps) {
  if (state.kind === "idle" || state.kind === "ready" || state.kind === "checking") {
    return null;
  }

  if (state.kind === "allowed") {
    const sessionId = state.session?.risk_session_id;
    return (
      <div role="status" className="rounded border border-green-300 bg-green-50 p-4">
        <p className="font-semibold text-green-800">Risk check passed</p>
        <button
          type="button"
          onClick={onContinueToHandoff}
          className="mt-2 rounded bg-green-700 px-4 py-2 text-white disabled:opacity-50"
        >
          Continue to UPI app (simulated)
        </button>
        {showHandoff && (
          <div className="mt-3 rounded bg-white p-3 text-sm text-slate-600">
            <p>Simulated hand-off ready for session {sessionId ? truncateId(sessionId) : "—"}.</p>
            <p className="demo-note">
              Demo: hand-off to Razorpay is wired by the demo simulator (not part of A7).
            </p>
          </div>
        )}
      </div>
    );
  }

  if (state.kind === "blocked") {
    return (
      <div role="alert" className="rounded border border-red-300 bg-red-50 p-4">
        <p className="font-semibold text-red-800">Registration blocked</p>
        <p className="text-sm text-red-700">
          This registration cannot continue. Please contact support.
        </p>
        <button
          type="button"
          onClick={onStartOver}
          className="mt-2 rounded border border-red-400 px-4 py-2 text-red-800"
        >
          Start over
        </button>
      </div>
    );
  }

  if (state.kind === "error_precheck_unavailable") {
    return (
      <div role="alert" className="rounded border border-amber-300 bg-amber-50 p-4">
        <p className="font-semibold text-amber-800">
          Risk pre-check is unavailable — you cannot continue yet
        </p>
        <button
          type="button"
          onClick={onRetry}
          className="mt-2 rounded bg-amber-600 px-4 py-2 text-white"
        >
          Retry
        </button>
      </div>
    );
  }

  return (
    <div role="alert" className="rounded border border-red-300 bg-red-50 p-4">
      <p className="font-semibold text-red-800">Something went wrong</p>
      <p className="text-sm text-red-700">{state.message ?? "Please try again."}</p>
      <div className="mt-2 flex gap-2">
        <button
          type="button"
          onClick={onRetry}
          className="rounded bg-red-700 px-4 py-2 text-white"
        >
          Retry
        </button>
        <button
          type="button"
          onClick={onStartOver}
          className="rounded border border-red-400 px-4 py-2 text-red-800"
        >
          Start over
        </button>
      </div>
    </div>
  );
}
