"use client";

import { useCallback, useEffect, useState } from "react";
import {
  fetchAssessments,
  type DashboardAssessmentItem,
} from "../../lib/dashboard-api";
import AssessmentTable from "../../components/dashboard/AssessmentTable";
import AuditChainPanel from "../../components/dashboard/AuditChainPanel";
import ReasonChips from "../../components/dashboard/ReasonChips";

const POLL_INTERVAL_MS = 2000;

export default function DashboardPage() {
  const [items, setItems] = useState<DashboardAssessmentItem[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [status, setStatus] = useState<string>("Loading…");

  const refresh = useCallback(async () => {
    try {
      const data = await fetchAssessments({ limit: 50 });
      setItems(data.items);
      setStatus(`Updated ${new Date().toLocaleTimeString()}`);
    } catch {
      setStatus("Dashboard updates paused — API unreachable");
    }
  }, []);

  useEffect(() => {
    void refresh();
    const timer = setInterval(() => {
      if (!document.hidden) {
        void refresh();
      }
    }, POLL_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [refresh]);

  const selected = items.find((item) => item.assessment_id === selectedId) ?? null;

  return (
    <main className="mx-auto max-w-5xl space-y-4 p-6">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-bold">Risk dashboard (demo)</h1>
        <button
          type="button"
          onClick={() => void refresh()}
          className="rounded border border-slate-300 px-3 py-1 text-sm"
        >
          Refresh
        </button>
      </div>
      <p role="status" className="text-xs text-slate-500">
        {status}
      </p>
      <AssessmentTable items={items} selectedId={selectedId} onSelect={setSelectedId} />
      {selected && (
        <section aria-label="Assessment drill-down" className="space-y-2 rounded border p-4">
          <h2 className="text-sm font-semibold">Assessment detail</h2>
          <ReasonChips reasons={selected.reasons ?? []} />
          <AuditChainPanel aggregateType="risk_assessment" aggregateId={selected.assessment_id ?? ""} />
        </section>
      )}
      <footer className="demo-note border-t pt-2">
        Demo build: data is simulated and local-only. Dashboard has no authentication and shows
        no raw customer data.
      </footer>
    </main>
  );
}
