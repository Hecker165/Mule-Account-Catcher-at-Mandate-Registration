interface BadgeProps {
  label: string;
  tone: "green" | "amber" | "red" | "slate";
}

function Badge({ label, tone }: BadgeProps) {
  const tones: Record<BadgeProps["tone"], string> = {
    green: "bg-green-100 text-green-800 border-green-300",
    amber: "bg-amber-100 text-amber-800 border-amber-300",
    red: "bg-red-100 text-red-800 border-red-300",
    slate: "bg-slate-100 text-slate-600 border-slate-300",
  };
  return (
    <span className={`inline-block rounded border px-2 py-0.5 text-xs font-semibold ${tones[tone]}`}>
      {label}
    </span>
  );
}

export function DecisionBadge({ decision }: { decision: string }) {
  const tone = decision === "ALLOW" ? "green" : decision === "CHALLENGE" ? "amber" : "red";
  return <Badge label={decision} tone={tone} />;
}

export function ActionBadge({ status }: { status: string | null | undefined }) {
  if (status === null || status === undefined) {
    return <span className="text-slate-400">—</span>;
  }
  const tone =
    status === "SUCCEEDED" ? "green" : status === "FAILED" ? "red" : "amber";
  return <Badge label={status} tone={tone} />;
}

export function DemoBadge() {
  return <Badge label="simulated" tone="slate" />;
}

export function LatencyBadge({ ms }: { ms: number }) {
  return <Badge label={`${ms} ms`} tone="slate" />;
}
