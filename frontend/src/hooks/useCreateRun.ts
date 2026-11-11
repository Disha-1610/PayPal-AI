"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { ApiError, createRun } from "@/lib/api";
import type { CreateRunInput } from "@/features/intake/types";

export interface UseCreateRun {
  submit: (input: CreateRunInput) => Promise<void>;
  submitting: boolean;
  error: string | null;
}

/** Creates a run and navigates to it. One idempotency key per hook instance. */
export function useCreateRun(): UseCreateRun {
  const router = useRouter();
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const keyRef = useRef<string>(crypto.randomUUID());
  const controllerRef = useRef<AbortController | null>(null);

  useEffect(() => () => controllerRef.current?.abort(), []);

  const submit = useCallback(
    async (input: CreateRunInput): Promise<void> => {
      if (controllerRef.current) return;
      const controller = new AbortController();
      controllerRef.current = controller;
      setSubmitting(true);
      setError(null);
      try {
        const { run_id: runId } = await createRun(input, keyRef.current, controller.signal);
        router.push(`/runs/${encodeURIComponent(runId)}`);
      } catch (err) {
        if (controller.signal.aborted) return;
        setError(err instanceof ApiError ? err.message : "Could not start the task");
        setSubmitting(false);
      } finally {
        controllerRef.current = null;
      }
    },
    [router],
  );

  return { submit, submitting, error };
}
