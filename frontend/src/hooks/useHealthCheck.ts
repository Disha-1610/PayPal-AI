"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError, getHealth } from "@/lib/api";
import type { HealthResponse } from "@/types/api";

export type HealthState =
  | { status: "loading" }
  | { status: "ready"; data: HealthResponse }
  | { status: "unauthorized"; message: string }
  | { status: "error"; message: string; timedOut: boolean };

function toErrorState(error: unknown): HealthState {
  if (error instanceof ApiError) {
    if (error.isUnauthorized) {
      return { status: "unauthorized", message: "Your session has expired. Please sign in again." };
    }
    return {
      status: "error",
      message: error.status === 0 ? error.message : `Backend error (${error.status}): ${error.message}`,
      timedOut: error.status === 0 && error.message === "request timed out",
    };
  }
  return { status: "error", message: "Unexpected error while contacting the backend.", timedOut: false };
}

/** Calls GET /health (Bearer token + cookie attached by the API client). */
export function useHealthCheck(): { state: HealthState; retry: () => void } {
  const [state, setState] = useState<HealthState>({ status: "loading" });
  const [attempt, setAttempt] = useState(0);
  const controllerRef = useRef<AbortController | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    controllerRef.current = controller;
    setState({ status: "loading" });

    getHealth(controller.signal)
      .then((data) => {
        setState(
          data.status === "ok"
            ? { status: "ready", data }
            : { status: "error", message: `Backend reported status "${data.status}".`, timedOut: false },
        );
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        setState(toErrorState(error));
      });

    return () => controller.abort();
  }, [attempt]);

  const retry = useCallback(() => setAttempt((n) => n + 1), []);

  return { state, retry };
}
