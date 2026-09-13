import type { components } from "../../lib/api-types";
import { formatPaise, maskOrderRef } from "../../lib/masking";

type MandateIntent = components["schemas"]["MandateIntent"];

interface MandateSummaryProps {
  intent: MandateIntent;
  orderRef: string | null;
}

export default function MandateSummary({ intent, orderRef }: MandateSummaryProps) {
  return (
    <section aria-label="Mandate summary" className="rounded border border-slate-200 bg-white p-4">
      <h2 className="text-sm font-semibold text-slate-700">Mandate details (demo)</h2>
      <dl className="mt-2 space-y-1 text-sm text-slate-600">
        <div className="flex justify-between">
          <dt>Amount</dt>
          <dd>{intent.max_amount_paise === null ? "—" : formatPaise(intent.max_amount_paise)}</dd>
        </div>
        <div className="flex justify-between">
          <dt>Frequency</dt>
          <dd>{intent.frequency ?? "—"}</dd>
        </div>
        <div className="flex justify-between">
          <dt>Order reference</dt>
          <dd>{orderRef === null ? "—" : maskOrderRef(orderRef)}</dd>
        </div>
      </dl>
      <p className="demo-note mt-2">Demo values — no real mandate is created.</p>
    </section>
  );
}
