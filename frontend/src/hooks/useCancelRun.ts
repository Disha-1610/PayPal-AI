"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, cancelRun } from "@/lib/api";

export type CancelPhase = "idle" | "confirming" | "cancelling" | "done" | "error";

export interface UseCancelRun {
  phase: CancelPhase;
  error: string | null;
  /** First click asks for confirmation; nothing is sent yet. */
  ask: () => void;
  /** Back out of the confirmation. */
  keep: () => void;
  /** Second click: sends the cancel. */
  confirm: () => Promise<void>;
}

/** Cancels a run after an explicit confirmation. The run's new status arrives through the event stream. */
export function useCancelRun(runId: string): UseCancelRun {
  const [phase, setPhase] = useState<CancelPhase>("idle");
  const [error, setError] = useState<string | null>(null);
  const inFlight = useRef<AbortController | null>(null);

  useEffect(() => () => inFlight.current?.abort(), []);

  const ask = useCallback(() => {
    setError(null);
    setPhase("confirming");
  }, []);
  const keep = useCallback(() => setPhase("idle"), []);

  const confirm = useCallback(async (): Promise<void> => {
    if (inFlight.current) return;
    const controller = new AbortController();
    inFlight.current = controller;
    setPhase("cancelling");
    try {
      await cancelRun(runId, controller.signal);
      setPhase("done");
    } catch (err) {
      if (controller.signal.aborted) return;
      setError(err instanceof ApiError ? err.message : "Could not cancel the run");
      setPhase("error");
    } finally {
      inFlight.current = null;
    }
  }, [runId]);

  return { phase, error, ask, keep, confirm };
}
