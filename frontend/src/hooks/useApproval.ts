"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, submitApproval } from "@/lib/api";
import type { ApprovalRequest, ApprovalState } from "@/features/approvals/types";
import type { ApprovalChoice } from "@/types/api";

const INITIAL: ApprovalState = { phase: "idle", decision: null, error: null };

/**
 * Submits a HITL decision with an optimistic update. A 409 means the pending terms
 * changed or were already resolved ("stale"): the caller should refetch the run.
 */
export function useApproval(request: ApprovalRequest) {
  const [state, setState] = useState<ApprovalState>(INITIAL);
  const inFlight = useRef<AbortController | null>(null);

  useEffect(() => () => inFlight.current?.abort(), []);

  const submit = useCallback(
    async (decision: ApprovalChoice): Promise<void> => {
      if (inFlight.current) return;
      const controller = new AbortController();
      inFlight.current = controller;
      setState({ phase: "submitting", decision, error: null });
      try {
        await submitApproval(
          request.run_id,
          { decision, terms_hash: request.terms_hash },
          controller.signal,
        );
        setState({ phase: "resolved", decision, error: null });
      } catch (err) {
        if (controller.signal.aborted) return;
        if (err instanceof ApiError && err.status === 409) {
          setState({
            phase: "stale",
            decision: null,
            error: "These terms changed or were already resolved. Reload to see the latest.",
          });
        } else {
          const message = err instanceof ApiError ? err.message : "Could not submit decision";
          setState({ phase: "error", decision: null, error: message });
        }
      } finally {
        inFlight.current = null;
      }
    },
    [request.run_id, request.terms_hash],
  );

  return { state, submit };
}
