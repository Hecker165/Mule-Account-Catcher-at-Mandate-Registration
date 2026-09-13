"use client";

import { useEffect, useState } from "react";
import {
  fetchAuditEvents,
  verifyAuditChain,
  type AuditVerificationResponse,
  type DashboardAuditItem,
} from "../../lib/dashboard-api";

interface AuditChainPanelProps {
  aggregateType: string;
  aggregateId: string;
}

export default function AuditChainPanel({ aggregateType, aggregateId }: AuditChainPanelProps) {
  const [events, setEvents] = useState<DashboardAuditItem[] | null>(null);
  const [verification, setVerification] = useState<AuditVerificationResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setEvents(null);
    setVerification(null);
    setError(null);
    Promise.all([
      fetchAuditEvents(aggregateType, aggregateId),
      verifyAuditChain(aggregateType, aggregateId),
    ])
      .then(([chain, result]) => {
        if (!cancelled) {
          setEvents(chain);
          setVerification(result);
        }
      })
      .catch(() => {
        if (!cancelled) {
          setError("Audit chain unavailable.");
        }
      });
    return () => {
      cancelled = true;
    };
  }, [aggregateType, aggregateId]);

  if (error) {
    return <p className="text-sm text-red-700">{error}</p>;
  }
  if (events === null || verification === null) {
    return <p className="text-sm text-slate-500">Loading audit chain…</p>;
  }

  return (
    <div className="space-y-2">
      {verification.valid ? (
        <p className="rounded border border-green-300 bg-green-50 p-2 text-sm font-semibold text-green-800">
          Chain verified · {verification.checked_events} events
        </p>
      ) : (
        <p role="alert" className="rounded border border-red-300 bg-red-50 p-2 text-sm font-semibold text-red-800">
          Chain invalid{verification.first_invalid_sequence !== null ? ` at sequence ${verification.first_invalid_sequence}` : ""}
          {verification.reason ? `: ${verification.reason}` : ""}
        </p>
      )}
      <ol className="space-y-1 text-xs text-slate-600">
        {events.map((event) => (
          <li key={event.audit_event_id} className="rounded border border-slate-200 p-2">
            #{event.sequence_number} {event.event_type} · {event.actor_type}
            {event.actor_id ? `/${event.actor_id}` : ""} ·{" "}
            {new Date(event.occurred_at).toLocaleString()}
          </li>
        ))}
      </ol>
    </div>
  );
}
