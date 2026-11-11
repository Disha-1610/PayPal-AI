"use client";

import { useCallback, useEffect, useState, type ReactNode } from "react";
import { ApiError, ensureSession, resetSession, setSessionRefresher } from "@/lib/api";
import { ErrorState } from "./ErrorState";

type Phase = { kind: "starting" } | { kind: "ready" } | { kind: "error"; message: string };

/** Signs the browser in before any page fetches data, and re-signs-in when a request meets a 401. */
export function SessionGate({ children }: { children: ReactNode }) {
  const [phase, setPhase] = useState<Phase>({ kind: "starting" });

  const start = useCallback(() => {
    setPhase({ kind: "starting" });
    ensureSession().then(
      () => setPhase({ kind: "ready" }),
      (err: unknown) =>
        setPhase({
          kind: "error",
          message: err instanceof ApiError && err.status === 0 ? "Could not reach the server" : "Could not start a session",
        }),
    );
  }, []);

  useEffect(() => {
    setSessionRefresher(() => {
      resetSession();
      return ensureSession();
    });
    start();
    return () => setSessionRefresher(null);
  }, [start]);

  if (phase.kind === "error") return <ErrorState message={phase.message} onRetry={start} />;
  if (phase.kind === "starting") {
    return (
      <p role="status" className="py-12 text-center text-sm text-slate-500">
        Starting…
      </p>
    );
  }
  return <>{children}</>;
}
