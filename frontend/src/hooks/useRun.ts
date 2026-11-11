"use client";

import { useCallback, useEffect, useState } from "react";
import { ApiError } from "@/lib/api";
import { fetchRun } from "@/features/runs/source";
import type { Run } from "@/features/runs/types";

export type UseRun =
  | { status: "loading" }
  | { status: "not_found" }
  | { status: "error"; message: string; reload: () => void }
  | { status: "success"; run: Run };

/** Loads one run by id; live updates are layered on top by `useRunState`. */
export function useRun(runId: string): UseRun {
  const [state, setState] = useState<UseRun>({ status: "loading" });
  const [reloadToken, setReloadToken] = useState(0);
  const reload = useCallback(() => setReloadToken((n) => n + 1), []);

  useEffect(() => {
    const controller = new AbortController();
    setState({ status: "loading" });
    fetchRun(runId, controller.signal)
      .then((run) => setState({ status: "success", run }))
      .catch((err: unknown) => {
        if (controller.signal.aborted) return;
        if (err instanceof ApiError && err.status === 404) {
          setState({ status: "not_found" });
        } else {
          const message = err instanceof ApiError ? err.message : "Could not load run";
          setState({ status: "error", message, reload });
        }
      });
    return () => controller.abort();
  }, [runId, reloadToken, reload]);

  return state;
}
