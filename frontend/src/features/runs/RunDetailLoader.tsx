"use client";

import { Loader2, SearchX } from "lucide-react";
import { Card } from "@/components/Card";
import { EmptyState } from "@/components/EmptyState";
import { ErrorState } from "@/components/ErrorState";
import { useRun } from "@/hooks/useRun";
import { RunDetailView } from "./RunDetailView";

export function RunDetailLoader({ runId }: { runId: string }) {
  const state = useRun(runId);

  switch (state.status) {
    case "loading":
      return (
        <div role="status" className="flex justify-center py-12 text-slate-500">
          <Loader2 className="h-6 w-6 animate-spin" aria-label="Loading run" />
        </div>
      );
    case "not_found":
      return (
        <Card>
          <EmptyState
            icon={SearchX}
            title="Run not found"
            description="It may not exist or you may not have access to it."
          />
        </Card>
      );
    case "error":
      return (
        <Card>
          <ErrorState message={state.message} onRetry={state.reload} />
        </Card>
      );
    case "success":
      return <RunDetailView run={state.run} />;
  }
}
