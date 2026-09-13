"use client";

import { Suspense, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import StepUpChallenge from "../../components/checkout/StepUpChallenge";
import {
  clearChallengeContext,
  readChallengeContext,
  type ChallengeContext,
} from "../../lib/risk-session-state";
import { isUuid } from "../../lib/masking";

function Fallback() {
  return (
    <main className="mx-auto max-w-xl space-y-4 p-6">
      <h1 className="text-xl font-bold">Step-up verification (demo)</h1>
      <p className="text-sm text-slate-600">No challenge in progress — return to checkout</p>
      <a href="/checkout" className="text-sm text-slate-800 underline">
        Return to checkout
      </a>
    </main>
  );
}

function ChallengeContent() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [context, setContext] = useState<ChallengeContext | null>(null);
  const [loaded, setLoaded] = useState(false);

  const sessionParam = searchParams.get("session");

  useEffect(() => {
    setContext(readChallengeContext());
    setLoaded(true);
  }, []);

  if (!loaded) {
    return (
      <main className="mx-auto max-w-xl p-6">
        <p className="text-sm text-slate-600">Loading…</p>
      </main>
    );
  }

  if (
    !sessionParam ||
    !isUuid(sessionParam) ||
    !context ||
    context.risk_session_id !== sessionParam
  ) {
    return <Fallback />;
  }

  return (
    <main className="mx-auto max-w-xl space-y-4 p-6">
      <h1 className="text-xl font-bold">Step-up verification (demo)</h1>
      <StepUpChallenge
        riskSessionId={context.risk_session_id}
        onSuccess={() => {}}
        onCancel={() => {
          clearChallengeContext();
          router.push("/checkout");
        }}
      />
    </main>
  );
}

export default function ChallengePage() {
  return (
    <Suspense>
      <ChallengeContent />
    </Suspense>
  );
}
