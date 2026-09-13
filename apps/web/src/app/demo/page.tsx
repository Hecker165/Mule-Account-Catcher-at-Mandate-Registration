"use client";

import { useEffect, useState } from "react";
import { fetchAssessments, type DashboardAssessmentItem } from "../../lib/dashboard-api";

interface Counters {
  total: number;
  allow: number;
  challenge: number;
  block: number;
  simulated: number;
  revokeLifecycle: number;
  revokeSucceeded: number;
  avgLatencyMs: number | null;
  p95LatencyMs: number | null;
}

const SCENARIOS: { name: string; expected: string }[] = [
  {
    name: "Legitimate flow",
    expected: "ALLOW with no triggered rules; mandate proceeds to the UPI app.",
  },
  {
    name: "Bot burst",
    expected: "Device velocity rule triggers; pre-check answers CHALLENGE before any redirect.",
  },
  {
    name: "NPCI-risk rejection",
    expected: "REJECTION_AUDIT assessment scores 100 (BLOCK) with the NPCI reason; no revoke is queued.",
  },
  {
    name: "Confirmed high-risk block",
    expected: "POST_CONFIRMATION BLOCK queues exactly one revoke request plus outbox message.",
  },
  {
    name: "Duplicate webhook",
    expected: "Replay returns the original assessment with an idempotent-replay marker; no rescore.",
  },
  {
    name: "Worker failure and recovery",
    expected: "First attempt RETRYING with backoff, then SUCCEEDED; same idempotency key twice.",
  },
  {
    name: "Shared demo merchant velocity",
    expected: "One device across 3+ simulated merchants triggers the demo velocity rule.",
  },
];

function computeCounters(rows: DashboardAssessmentItem[]): Counters {
  const latencies = rows.map((row) => row.evaluation_latency_ms).sort((a, b) => a - b);
  const sum = latencies.reduce((total, value) => total + value, 0);
  return {
    total: rows.length,
    allow: rows.filter((row) => row.decision === "ALLOW").length,
    challenge: rows.filter((row) => row.decision === "CHALLENGE").length,
    block: rows.filter((row) => row.decision === "BLOCK").length,
    simulated: rows.filter((row) => row.is_demo_event === true).length,
    revokeLifecycle: rows.filter((row) => (row.action_status ?? null) !== null).length,
    revokeSucceeded: rows.filter((row) => row.action_status === "SUCCEEDED").length,
    avgLatencyMs: latencies.length > 0 ? Math.round(sum / latencies.length) : null,
    p95LatencyMs:
      latencies.length > 0 ? latencies[Math.min(latencies.length - 1, Math.floor(latencies.length * 0.95))] : null,
  };
}

export default function DemoPage() {
  const [counters, setCounters] = useState<Counters | null>(null);
  const [controlsInstalled, setControlsInstalled] = useState<boolean | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchAssessments({ limit: 100 })
      .then((data) => {
        if (!cancelled) {
          setCounters(computeCounters(data.items));
        }
      })
      .catch(() => {
        if (!cancelled) {
          setCounters(null);
        }
      });
    fetch("/api/v1/demo/scenarios", { cache: "no-store" })
      .then((response) => {
        if (!cancelled) {
          setControlsInstalled(response.ok);
        }
      })
      .catch(() => {
        if (!cancelled) {
          setControlsInstalled(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <main className="mx-auto max-w-3xl space-y-4 p-6">
      <h1 className="text-xl font-bold">Demo suite (demo)</h1>
      <nav className="flex gap-4 text-sm">
        <a href="/checkout" className="text-slate-800 underline">
          Drive a scenario manually
        </a>
        <a href="/dashboard" className="text-slate-800 underline">
          Back to dashboard
        </a>
      </nav>

      <section aria-label="Scenario checklist" className="rounded border p-4">
        <h2 className="text-sm font-semibold">Demonstration scenarios</h2>
        <ol className="mt-2 list-decimal space-y-2 pl-5 text-sm text-slate-700">
          {SCENARIOS.map((scenario) => (
            <li key={scenario.name}>
              <span className="font-medium">{scenario.name}:</span> {scenario.expected}
            </li>
          ))}
        </ol>
      </section>

      <section aria-label="Live counters" className="rounded border p-4">
        <h2 className="text-sm font-semibold">Live counters (last 100 assessments)</h2>
        {counters === null ? (
          <p className="text-sm text-slate-500">Counters unavailable — API unreachable.</p>
        ) : (
          <dl className="mt-2 grid grid-cols-2 gap-1 text-sm text-slate-700">
            <dt>Total</dt>
            <dd>{counters.total}</dd>
            <dt>ALLOW / CHALLENGE / BLOCK</dt>
            <dd>
              {counters.allow} / {counters.challenge} / {counters.block}
            </dd>
            <dt>Simulated rows</dt>
            <dd>{counters.simulated}</dd>
            <dt>Avg / p95 latency</dt>
            <dd>
              {counters.avgLatencyMs ?? "—"} ms / {counters.p95LatencyMs ?? "—"} ms
            </dd>
            <dt>Revoke lifecycle (SUCCEEDED)</dt>
            <dd>
              {counters.revokeLifecycle} ({counters.revokeSucceeded})
            </dd>
          </dl>
        )}
      </section>

      <section aria-label="Demo controls" className="rounded border p-4">
        <h2 className="text-sm font-semibold">Automated demo controls</h2>
        {controlsInstalled === null ? (
          <p className="text-sm text-slate-500">Checking for demo controls…</p>
        ) : controlsInstalled ? (
          <p className="text-sm text-slate-700">
            Automated demo controls are installed (see the A9 simulator package).
          </p>
        ) : (
          <p className="text-sm text-slate-500">
            Automated demo controls are not installed yet — provided by the A9 simulator
            package.
          </p>
        )}
      </section>

      <footer className="demo-note border-t pt-2">
        Demo build: data is simulated and local-only. Run controls arrive with the A9
        simulator package.
      </footer>
    </main>
  );
}
