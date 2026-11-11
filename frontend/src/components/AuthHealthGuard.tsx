"use client";

import type { ReactNode } from "react";

import { useHealthCheck } from "@/hooks/useHealthCheck";

export function AuthHealthGuard({ children }: { children: ReactNode }) {
  const { state, retry } = useHealthCheck();

  if (state.status === "loading") {
    return (
      <div role="status" aria-live="polite">
        Checking backend connection…
      </div>
    );
  }

  if (state.status === "ready") return <>{children}</>;

  return (
    <div role="alert">
      <p>{state.status === "error" && state.timedOut ? "The backend timed out." : state.message}</p>
      <button type="button" onClick={retry}>
        Retry
      </button>
    </div>
  );
}
