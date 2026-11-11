"use client";

import { useEffect, useState } from "react";
import { applyRunEvent, parseRunEvent } from "@/features/runs/events";
import type { Run } from "@/features/runs/types";
import { connectSSE, type SSEConnectionState } from "@/lib/sse";

interface UseRunStateOptions {
  /** When false the hook returns `initialRun` unchanged and opens no stream. */
  enabled?: boolean;
}

export interface UseRunState {
  run: Run;
  connection: SSEConnectionState;
  error: string | null;
}

/**
 * Real-time run state: seeds from `initialRun`, then folds SSE events into it.
 * A `run.snapshot` event (sent on every (re)connect) replaces the whole run.
 */
export function useRunState(initialRun: Run, { enabled = true }: UseRunStateOptions = {}): UseRunState {
  const [run, setRun] = useState<Run>(initialRun);
  const [connection, setConnection] = useState<SSEConnectionState>("closed");
  const [error, setError] = useState<string | null>(null);
  const runId = initialRun.id;

  useEffect(() => {
    if (!enabled) return;
    const close = connectSSE({
      path: `/runs/${encodeURIComponent(runId)}/events`,
      onMessage: (message) => {
        const event = parseRunEvent(message);
        if (!event) {
          console.warn("Ignoring unrecognised run event", message.event);
          return;
        }
        setRun((current) => applyRunEvent(current, event));
      },
      onState: (state, reason) => {
        setConnection(state);
        setError(state === "closed" ? (reason ?? null) : null);
      },
    });
    return close;
  }, [runId, enabled]);

  return { run, connection, error };
}
