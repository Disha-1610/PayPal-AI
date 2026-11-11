import Link from "next/link";
import { Inbox } from "lucide-react";
import { Card } from "@/components/Card";
import { EmptyState } from "@/components/EmptyState";
import { formatDateTime, formatUsd } from "@/lib/utils";
import { RunStatusBadge } from "./RunStatusBadge";
import type { Run } from "./types";

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
                  Budget {formatUsd(run.budget_usd)} · {done}/{run.steps.length} steps · Updated{" "}
                  {formatDateTime(run.updated_at)}
                </p>
              </Card>
            </Link>
          </li>
        );
      })}
    </ul>
  );
}
