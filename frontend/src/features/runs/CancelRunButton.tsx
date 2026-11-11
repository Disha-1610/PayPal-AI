"use client";

import { Loader2 } from "lucide-react";
import { useCancelRun } from "@/hooks/useCancelRun";
import { ACTIVE_STATUSES, type Run } from "./types";

const BUTTON = "rounded-lg border px-3 py-1.5 text-sm font-medium disabled:cursor-not-allowed disabled:opacity-50";

/** Cancel with confirmation. Only offered while the run can still be cancelled. */
export function CancelRunButton({ run }: { run: Pick<Run, "id" | "status"> }) {
  const { phase, error, ask, keep, confirm } = useCancelRun(run.id);
  if (!ACTIVE_STATUSES.includes(run.status) || phase === "done") return null;

  return (
    <div className="flex flex-col items-end gap-1">
      {phase === "confirming" || phase === "cancelling" ? (
        <div className="flex items-center gap-2" role="group" aria-label="Confirm cancel">
          <span className="text-sm text-slate-700">Cancel this run? Held funds will be released.</span>
          <button
            type="button"
            disabled={phase === "cancelling"}
            onClick={() => void confirm()}
            className={`${BUTTON} inline-flex items-center gap-2 border-red-300 bg-red-600 text-white hover:bg-red-700`}
          >
            {phase === "cancelling" ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden /> : null}
            Yes, cancel
          </button>
          <button
            type="button"
            disabled={phase === "cancelling"}
            onClick={keep}
            className={`${BUTTON} border-slate-300 bg-white text-slate-700 hover:bg-slate-50`}
          >
            Keep running
          </button>
        </div>
      ) : (
        <button type="button" onClick={ask} className={`${BUTTON} border-slate-300 bg-white text-slate-700 hover:bg-slate-50`}>
          Cancel run
        </button>
      )}
      {error ? (
        <p role="alert" className="text-xs text-red-600">
          {error}
        </p>
      ) : null}
    </div>
  );
}
