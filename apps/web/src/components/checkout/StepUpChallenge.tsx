"use client";

import { useState } from "react";
import type { components } from "../../lib/api-types";
import { truncateId } from "../../lib/masking";

type MandateIntent = components["schemas"]["MandateIntent"];

interface StepUpChallengeProps {
  riskSessionId: string;
  intent?: MandateIntent;
  onSuccess: () => void;
  onCancel: () => void;
}

function makeDemoCode(): string {
  const digits = crypto.randomUUID().replace(/\D/g, "") + "0123456789";
  return digits.slice(0, 6);
}

export default function StepUpChallenge({
  riskSessionId,
  intent,
  onSuccess,
  onCancel,
}: StepUpChallengeProps) {
  const [demoCode] = useState(makeDemoCode);
  const [confirmed, setConfirmed] = useState(false);
  const [codeInput, setCodeInput] = useState("");
  const [attempts, setAttempts] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [completed, setCompleted] = useState(false);

  if (attempts >= 3 && !completed) {
    return (
      <div className="rounded border border-red-300 bg-red-50 p-4">
        <p className="font-semibold text-red-800">Verification failed — return to checkout</p>
        <a href="/checkout" className="mt-2 inline-block text-sm text-red-700 underline">
          Return to checkout
        </a>
      </div>
    );
  }

  if (completed) {
    return (
      <div className="rounded border border-green-300 bg-green-50 p-4">
        <p className="font-semibold text-green-800">Verification complete.</p>
        <p className="text-sm text-slate-600">
          In the live demo this is where you would be handed to your UPI app.
        </p>
        <p className="text-sm text-slate-600">Session {truncateId(riskSessionId)}.</p>
        <p className="demo-note">
          Demo: hand-off to Razorpay is wired by the demo simulator (not part of A7).
        </p>
      </div>
    );
  }

  const codeReady = /^\d{6}$/.test(codeInput);
  const canVerify = confirmed && codeReady;

  function handleVerify() {
    if (!canVerify) {
      return;
    }
    if (codeInput !== demoCode) {
      setAttempts((count) => count + 1);
      setError("The verification code does not match");
      return;
    }
    setError(null);
    setCompleted(true);
    onSuccess();
  }

  return (
    <div className="space-y-4 rounded border border-slate-200 bg-white p-4">
      <p className="text-sm text-slate-700">
        Additional verification is required before we can send you to your UPI app.
      </p>
      <div aria-label="Demo verification code" className="rounded border border-dashed p-3 text-center">
        <p className="demo-note">Demo verification code — displayed here because no SMS provider is configured</p>
        <p data-testid="demo-code" className="text-2xl font-mono tracking-widest">
          {demoCode}
        </p>
      </div>
      <label className="flex items-start gap-2 text-sm text-slate-700">
        <input
          type="checkbox"
          checked={confirmed}
          onChange={(event) => setConfirmed(event.target.checked)}
        />
        I confirm the mandate details above are correct
      </label>
      <div>
        <label htmlFor="demo-code-input" className="text-sm text-slate-700">
          Enter the 6-digit demo code
        </label>
        <input
          id="demo-code-input"
          inputMode="numeric"
          value={codeInput}
          onChange={(event) => setCodeInput(event.target.value)}
          className="mt-1 block w-40 rounded border border-slate-300 px-2 py-1"
        />
      </div>
      {error && (
        <p role="alert" className="text-sm text-red-700">
          {error} (attempt {attempts} of 3)
        </p>
      )}
      <div className="flex gap-2">
        <button
          type="button"
          onClick={handleVerify}
          disabled={!canVerify}
          className="rounded bg-slate-800 px-4 py-2 text-white disabled:opacity-50"
        >
          Verify and continue
        </button>
        <button
          type="button"
          onClick={onCancel}
          className="rounded border border-slate-300 px-4 py-2 text-slate-700"
        >
          Cancel
        </button>
      </div>
      <p className="text-sm text-slate-500">
        Mandate: {intent?.frequency ?? "—"} · session {truncateId(riskSessionId)}
      </p>
    </div>
  );
}
