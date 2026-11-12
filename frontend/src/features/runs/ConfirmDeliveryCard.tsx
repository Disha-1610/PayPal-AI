"use client";

import { useEffect, useRef, useState } from "react";
import { Loader2 } from "lucide-react";
import { Card, CardBody, CardHeader } from "@/components/Card";
import { ApiError, confirmDelivery } from "@/lib/api";

/**
 * A counterparty the user named reports its work outside this app, so the user confirms delivery here. Accepting
 * lets the held payment be captured; rejecting voids it (or reopens negotiation). The new state arrives via SSE.
 */
export function ConfirmDeliveryCard({ runId, counterparty }: { runId: string; counterparty: string }) {
  const [sending, setSending] = useState<boolean | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [sent, setSent] = useState(false);
  const inFlight = useRef<AbortController | null>(null);

  useEffect(() => () => inFlight.current?.abort(), []);

  async function answer(accepted: boolean) {
    if (inFlight.current) return;
    const controller = new AbortController();
    inFlight.current = controller;
    setSending(accepted);
    setError(null);
    try {
      await confirmDelivery(runId, accepted, controller.signal);
      setSent(true);
    } catch (err) {
      if (controller.signal.aborted) return;
      setError(err instanceof ApiError ? err.message : "Could not send your answer");
    } finally {
      inFlight.current = null;
      setSending(null);
    }
  }

  return (
    <Card>
      <CardHeader>
        <h2 className="text-sm font-semibold text-slate-900">Did {counterparty} deliver?</h2>
        <p className="mt-0.5 text-xs text-slate-500">
          The payment is held until you confirm. Reject it if the work is missing or not what was agreed.
        </p>
      </CardHeader>
      <CardBody>
        {sent ? (
          <p className="text-sm text-slate-700">Thanks. Your answer was recorded.</p>
        ) : (
          <div className="flex flex-wrap items-center gap-3">
            <button
              type="button"
              disabled={sending !== null}
              onClick={() => void answer(true)}
              className="inline-flex items-center gap-2 rounded-lg bg-paypal-blue px-4 py-2 text-sm font-medium text-white hover:bg-paypal-darkblue disabled:cursor-not-allowed disabled:opacity-50"
            >
              {sending === true ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden /> : null}
              Yes, the work is delivered
            </button>
            <button
              type="button"
              disabled={sending !== null}
              onClick={() => void answer(false)}
              className="inline-flex items-center gap-2 rounded-lg border border-slate-300 px-4 py-2 text-sm font-medium text-slate-800 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {sending === false ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden /> : null}
              No, reject it
            </button>
          </div>
        )}
        {error ? (
          <p role="alert" className="mt-3 text-sm text-red-700">
            {error}
          </p>
        ) : null}
      </CardBody>
    </Card>
  );
}
