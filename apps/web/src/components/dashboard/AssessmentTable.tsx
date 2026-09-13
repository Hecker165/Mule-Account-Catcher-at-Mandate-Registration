import type { DashboardAssessmentItem } from "../../lib/dashboard-api";
import { maskVpa, truncateId } from "../../lib/dashboard-api";
import ReasonChips from "./ReasonChips";
import { ActionBadge, DecisionBadge, DemoBadge, LatencyBadge } from "./StatusBadges";

interface AssessmentTableProps {
  items: DashboardAssessmentItem[];
  selectedId: string | null;
  onSelect: (assessmentId: string) => void;
}

export default function AssessmentTable({ items, selectedId, onSelect }: AssessmentTableProps) {
  if (items.length === 0) {
    return <p className="text-sm text-slate-500">No assessments yet</p>;
  }
  return (
    <table className="w-full border-collapse text-sm">
      <thead>
        <tr className="text-left text-xs text-slate-500">
          <th className="border-b p-2">Assessed</th>
          <th className="border-b p-2">Decision</th>
          <th className="border-b p-2">Score</th>
          <th className="border-b p-2">Stage</th>
          <th className="border-b p-2">VPA</th>
          <th className="border-b p-2">Token</th>
          <th className="border-b p-2">Assessment</th>
          <th className="border-b p-2">Latency</th>
          <th className="border-b p-2">Revoke</th>
          <th className="border-b p-2">Reasons</th>
        </tr>
      </thead>
      <tbody>
        {items.map((item) => {
          const assessmentId = item.assessment_id ?? "";
          const selected = assessmentId !== "" && assessmentId === selectedId;
          return (
            <tr
              key={assessmentId}
              onClick={() => onSelect(assessmentId)}
              className={selected ? "cursor-pointer bg-slate-100" : "cursor-pointer hover:bg-slate-50"}
            >
              <td className="border-b p-2">{new Date(item.assessed_at).toLocaleString()}</td>
              <td className="border-b p-2">
                <DecisionBadge decision={item.decision} />
              </td>
              <td className="border-b p-2">{item.score}</td>
              <td className="border-b p-2 text-xs text-slate-600">{item.stage}</td>
              <td className="border-b p-2 font-mono text-xs">
                {maskVpa(item.vpa_handle) ?? "—"}
                {item.is_demo_event && (
                  <span className="ml-1">
                    <DemoBadge />
                  </span>
                )}
              </td>
              <td className="border-b p-2 font-mono text-xs">
                {item.token_id ? truncateId(item.token_id) : "—"}
              </td>
              <td className="border-b p-2 font-mono text-xs">{truncateId(assessmentId)}</td>
              <td className="border-b p-2">
                <LatencyBadge ms={item.evaluation_latency_ms} />
              </td>
              <td className="border-b p-2">
                <ActionBadge status={item.action_status} />
              </td>
              <td className="border-b p-2">
                <ReasonChips reasons={item.reasons} />
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}
