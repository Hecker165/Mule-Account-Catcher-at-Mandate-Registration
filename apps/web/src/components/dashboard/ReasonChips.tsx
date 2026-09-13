import type { components } from "../../lib/api-types";

type Reason = components["schemas"]["DashboardReasonItem"];

interface ReasonChipsProps {
  reasons: Reason[] | undefined;
}

export default function ReasonChips({ reasons }: ReasonChipsProps) {
  if (!reasons || reasons.length === 0) {
    return <span className="text-xs text-slate-400">No triggered rules</span>;
  }
  return (
    <ul className="flex flex-wrap gap-1">
      {reasons.map((reason) => (
        <li
          key={reason.rule_id}
          title={reason.reason_text ?? reason.rule_id}
          className="rounded-full bg-slate-100 px-2 py-0.5 text-xs text-slate-700"
        >
          {reason.reason_code ?? reason.rule_id} +{reason.points}
          {reason.reason_text && (
            <span className="ml-1 text-slate-500">{reason.reason_text}</span>
          )}
        </li>
      ))}
    </ul>
  );
}
