"use client";

import { useEffect, useRef } from "react";
import Link from "next/link";
import { Loader2, SearchX } from "lucide-react";
import { Card, CardBody } from "@/components/Card";
import { EmptyState } from "@/components/EmptyState";
import { ErrorState } from "@/components/ErrorState";
import { useRun } from "@/hooks/useRun";
import { useRunState } from "@/hooks/useRunState";
import type { Run } from "@/features/runs/types";
import { OutcomeSummary } from "./OutcomeSummary";
import { PaymentStatusCard } from "./PaymentStatusCard";
import { reportPayerReturn } from "./source";

function ReturnContent({ initialRun, cancelled }: { initialRun: Run; cancelled: boolean }) {
  const { run } = useRunState(initialRun);
  const reported = useRef(false);

  // Resume the paused run once. The outcome still comes from webhooks/run state.
  useEffect(() => {
    if (cancelled || reported.current) return;
    reported.current = true;
    const controller = new AbortController();
    reportPayerReturn(run.id, controller.signal).catch(() => {
      // Non-fatal: the backend also learns the result from PayPal webhooks.
    });
    return () => controller.abort();
  }, [cancelled, run.id]);

  const finished = !["awaiting_payment", "running"].includes(run.status);

  return (
    <div className="space-y-4">
      <Card>
        <CardBody className="flex items-start gap-3">
          {cancelled ? null : finished ? null : (
            <Loader2 className="mt-0.5 h-5 w-5 shrink-0 animate-spin text-sky-600" aria-hidden />
          )}
          <div>
            <h1 className="text-sm font-semibold text-slate-900">
              {cancelled ? "Payment not approved" : "Confirming your payment"}
            </h1>
            <p className="mt-0.5 text-sm text-slate-600">
              {cancelled
                ? "You left PayPal without approving. No payment was made; you can approve again below."
                : "We're confirming with PayPal. This can take a moment, and the status below is the source of truth."}
            </p>
          </div>
        </CardBody>
      </Card>
      {run.payment ? (
        <PaymentStatusCard payment={run.payment} awaitingPayer={run.status === "awaiting_payment"} />
      ) : null}
      <OutcomeSummary run={run} />
      <Link
        href={`/runs/${encodeURIComponent(run.id)}`}
        className="inline-block text-sm text-paypal-lightblue hover:underline"
      >
        Back to run
      </Link>
    </div>
  );
}

export function PaymentReturnView({ runId, cancelled }: { runId: string; cancelled: boolean }) {
  const state = useRun(runId);

  switch (state.status) {
    case "loading":
      return (
        <div role="status" className="flex justify-center py-12 text-slate-500">
          <Loader2 className="h-6 w-6 animate-spin" aria-label="Loading payment" />
        </div>
      );
    case "not_found":
      return (
        <Card>
          <EmptyState icon={SearchX} title="Run not found" />
        </Card>
      );
    case "error":
      return (
        <Card>
          <ErrorState message={state.message} onRetry={state.reload} />
        </Card>
      );
    case "success":
      return <ReturnContent initialRun={state.run} cancelled={cancelled} />;
  }
}
