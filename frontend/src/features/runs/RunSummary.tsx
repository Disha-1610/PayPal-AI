import { Card, CardBody } from "@/components/Card";
import { formatDateTime, formatUsd } from "@/lib/utils";
import { CancelRunButton } from "./CancelRunButton";
import { RunStatusBadge } from "./RunStatusBadge";
import type { Run } from "./types";

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-xs uppercase tracking-wide text-slate-500">{label}</dt>
      <dd className="mt-0.5 text-sm font-medium text-slate-900">{value}</dd>
    </div>
  );
}

export function RunSummary({ run }: { run: Run }) {
  return (
    <Card>
      <CardBody>
        <div className="flex items-start justify-between gap-4">
          <h1 className="text-lg font-semibold text-slate-900">{run.goal}</h1>
          <RunStatusBadge status={run.status} />
        </div>
        <div className="mt-3 flex justify-end">
          <CancelRunButton run={run} />
        </div>
        <dl className="mt-4 grid grid-cols-2 gap-4 sm:grid-cols-4">
          <Stat label="Budget" value={formatUsd(run.budget_usd)} />
          <Stat label="Iteration" value={`${run.iteration} / ${run.max_iterations}`} />
          <Stat label="Started" value={formatDateTime(run.created_at)} />
          <Stat label="Updated" value={formatDateTime(run.updated_at)} />
        </dl>
      </CardBody>
    </Card>
  );
}
