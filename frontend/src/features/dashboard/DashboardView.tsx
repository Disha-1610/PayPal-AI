"use client";

import Link from "next/link";
import { useMemo } from "react";
import { AlertTriangle, Loader2, PlusCircle } from "lucide-react";
import { Card, CardBody, CardHeader } from "@/components/Card";
import { ErrorState } from "@/components/ErrorState";
import { useRuns } from "@/hooks/useRuns";
import { RunList } from "@/features/runs/RunList";
import { RunStatusBadge } from "@/features/runs/RunStatusBadge";
import { formatMoney } from "@/lib/utils";
import { deliveryDate, deliveryWarnings, formatDay, needsAction, relativeDays, totalSpent, upcomingDeliveries } from "./stats";

function RunLink({ id, children }: { id: string; children: React.ReactNode }) {
  return (
    <Link href={`/runs/${encodeURIComponent(id)}`} className="font-medium text-slate-900 hover:underline">
      {children}
    </Link>
  );
}

export function DashboardView() {
  const { runs, loading, loadingMore, error, hasMore, loadMore, reload } = useRuns();
  const now = useMemo(() => new Date(), [runs]);
  const warnings = useMemo(() => deliveryWarnings(runs, now), [runs, now]);
  const waiting = useMemo(() => needsAction(runs), [runs]);
  const upcoming = useMemo(() => upcomingDeliveries(runs), [runs]);
  const spent = useMemo(() => totalSpent(runs), [runs]);

  if (loading) {
    return (
      <div role="status" className="flex justify-center py-12 text-slate-500">
        <Loader2 className="h-6 w-6 animate-spin" aria-label="Loading dashboard" />
      </div>
    );
  }
  if (error && runs.length === 0) {
    return (
      <Card>
        <ErrorState message={error} onRetry={reload} />
      </Card>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold text-slate-900">Dashboard</h1>
        <Link
          href="/runs/new"
          className="inline-flex items-center gap-2 rounded-lg bg-paypal-blue px-4 py-2 text-sm font-medium text-white hover:bg-paypal-darkblue"
        >
          <PlusCircle className="h-4 w-4" aria-hidden /> New run
        </Link>
      </div>

      <Card>
        <CardBody>
          <p className="text-xs uppercase tracking-wide text-slate-500">Total spent</p>
          <p className="mt-1 text-2xl font-semibold text-slate-900">
            {spent.length === 0 ? "No payments yet" : spent.map((s) => formatMoney(s.amount, s.currency)).join(" + ")}
          </p>
          <p className="mt-1 text-sm text-slate-500">
            {runs.length} {runs.length === 1 ? "run" : "runs"} in total
          </p>
        </CardBody>
      </Card>

      {warnings.length > 0 || waiting.length > 0 ? (
        <section aria-labelledby="attention">
          <h2 id="attention" className="mb-3 text-sm font-semibold text-slate-900">Needs your attention</h2>
          <ul className="space-y-2">
            {warnings.map(({ run, kind, daysLate }) => (
              <li key={`w-${run.id}`} className="flex items-start gap-3 rounded-xl border border-amber-300 bg-amber-50 px-4 py-3">
                <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-700" aria-hidden />
                <p className="text-sm text-amber-900">
                  <RunLink id={run.id}>{run.goal}</RunLink>:{" "}
                  {kind === "delayed"
                    ? `promised ${formatDay(run.promised_by)}, now expected ${formatDay(run.expected_by)} (${daysLate} ${daysLate === 1 ? "day" : "days"} late)`
                    : `was due ${formatDay(run.promised_by)} and is ${daysLate} ${daysLate === 1 ? "day" : "days"} overdue`}
                </p>
              </li>
            ))}
            {waiting.map((run) => (
              <li key={`a-${run.id}`} className="flex items-center justify-between gap-3 rounded-xl border border-slate-200 bg-white px-4 py-3">
                <RunLink id={run.id}>{run.goal}</RunLink>
                <RunStatusBadge status={run.status} />
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {upcoming.length > 0 ? (
        <section aria-labelledby="arriving">
          <h2 id="arriving" className="mb-3 text-sm font-semibold text-slate-900">Arriving soon</h2>
          <ul className="space-y-2">
            {upcoming.slice(0, 5).map((run) => (
              <li key={run.id} className="flex items-center justify-between gap-3 rounded-xl border border-slate-200 bg-white px-4 py-3">
                <RunLink id={run.id}>{run.goal}</RunLink>
                <span className="shrink-0 text-sm text-slate-500">
                  {formatDay(deliveryDate(run))} · {relativeDays(deliveryDate(run), now)}
                </span>
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      <section aria-labelledby="all-runs">
        <h2 id="all-runs" className="mb-3 text-sm font-semibold text-slate-900">All runs</h2>
        {error ? (
          <p role="alert" className="mb-2 text-sm text-red-700">{error}</p>
        ) : null}
        <RunList runs={runs} />
        {hasMore ? (
          <div className="mt-4 flex justify-center">
            <button
              type="button"
              onClick={loadMore}
              disabled={loadingMore}
              className="inline-flex items-center gap-2 rounded-lg border border-slate-300 bg-white px-4 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50"
            >
              {loadingMore ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden /> : null}
              Load more
            </button>
          </div>
        ) : null}
      </section>
    </div>
  );
}
