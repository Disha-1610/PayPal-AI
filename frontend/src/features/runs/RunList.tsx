import Link from "next/link";
import { Inbox } from "lucide-react";
import { Card } from "@/components/Card";
import { EmptyState } from "@/components/EmptyState";
import { formatDay } from "@/features/dashboard/stats";
import { formatDateTime, formatMoney } from "@/lib/utils";
import { RunStatusBadge } from "./RunStatusBadge";
import type { Run } from "./types";

function DeliveryLine({ run }: { run: Run }) {
  if (!run.promised_by && !run.expected_by) return null;
  const delayed = run.expected_by && run.promised_by && Date.parse(run.expected_by) > Date.parse(run.promised_by);
  if (delayed) {
    return (
      <p className="mt-1 text-sm font-medium text-amber-700">
        Delayed: promised {formatDay(run.promised_by)}, now expected {formatDay(run.expected_by)}
      </p>
    );
  }
  return <p className="mt-1 text-sm text-slate-500">Delivery by {formatDay(run.expected_by ?? run.promised_by)}</p>;
}

export function RunList({ runs }: { runs: readonly Run[] }) {
  if (runs.length === 0) {
    return (
      <Card>
        <EmptyState
          icon={Inbox}
          title="No runs yet"
          description="Runs started by the agent will appear here."
        />
      </Card>
    );
  }

  return (
    <ul className="space-y-3">
      {runs.map((run) => {
        const done = run.steps.filter((s) => s.status === "done").length;
        return (
          <li key={run.id}>
            <Link
              href={`/runs/${encodeURIComponent(run.id)}`}
              className="block rounded-xl focus:outline-none focus-visible:ring-2 focus-visible:ring-paypal-lightblue"
            >
              <Card className="px-5 py-4 transition hover:border-paypal-lightblue">
                <div className="flex items-start justify-between gap-4">
                  <p className="font-medium text-slate-900">{run.goal}</p>
                  <RunStatusBadge status={run.status} />
                </div>
                <p className="mt-2 text-sm text-slate-500">
                  {run.payment ? `Paid ${formatMoney(run.payment.amount, run.payment.currency)}` : `Budget ${formatMoney(run.budget_usd, run.currency)}`}
                  {" · "}
                  {done}/{run.steps.length} steps · Updated {formatDateTime(run.updated_at)}
                </p>
                <DeliveryLine run={run} />
              </Card>
            </Link>
          </li>
        );
      })}
    </ul>
  );
}
