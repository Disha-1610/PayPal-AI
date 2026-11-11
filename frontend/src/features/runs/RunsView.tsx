"use client";

import { Loader2 } from "lucide-react";
import { Card } from "@/components/Card";
import { ErrorState } from "@/components/ErrorState";
import { useRuns } from "@/hooks/useRuns";
import { RunList } from "./RunList";

export function RunsView() {
  const { runs, loading, loadingMore, error, hasMore, loadMore, reload } = useRuns();

  if (loading) {
    return (
      <div role="status" className="flex justify-center py-12 text-slate-500">
        <Loader2 className="h-6 w-6 animate-spin" aria-label="Loading runs" />
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
    <div className="space-y-4">
      {error ? (
        <p role="alert" className="text-sm text-red-700">
          {error}
        </p>
      ) : null}
      <RunList runs={runs} />
      {hasMore ? (
        <div className="flex justify-center">
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
    </div>
  );
}
