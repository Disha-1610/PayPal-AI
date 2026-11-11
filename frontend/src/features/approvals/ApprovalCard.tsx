"use client";

import { AlertTriangle, CheckCircle2, Loader2, XCircle } from "lucide-react";
import { Card, CardBody, CardHeader } from "@/components/Card";
import { useApproval } from "@/hooks/useApproval";
import { cn } from "@/lib/utils";
import { ApprovalTerms } from "./ApprovalTerms";
import type { ApprovalRequest } from "./types";

export function ApprovalCard({ request }: { request: ApprovalRequest }) {
  const { state, submit } = useApproval(request);
  const submitting = state.phase === "submitting";
  const locked = submitting || state.phase === "resolved" || state.phase === "stale";

  return (
    <Card className="border-amber-200">
      <CardHeader>
        <h2 className="text-sm font-semibold text-slate-900">Approval required</h2>
        <p className="mt-0.5 text-xs text-slate-500">
          Your decision applies only to these exact terms.
        </p>
      </CardHeader>
      <CardBody className="space-y-4">
        <ApprovalTerms request={request} />

        {state.phase === "resolved" ? (
          <p role="status" className="flex items-center gap-2 text-sm text-slate-700">
            {state.decision === "approve" ? (
              <CheckCircle2 className="h-4 w-4 text-emerald-600" aria-hidden />
            ) : (
              <XCircle className="h-4 w-4 text-red-600" aria-hidden />
            )}
            {state.decision === "approve" ? "Approved" : "Rejected"}
          </p>
        ) : (
          <div className="flex gap-3">
            <button
              type="button"
              disabled={locked}
              onClick={() => void submit("approve")}
              className={cn(
                "inline-flex items-center gap-2 rounded-lg bg-paypal-blue px-4 py-2 text-sm font-medium text-white",
                "hover:bg-paypal-darkblue disabled:cursor-not-allowed disabled:opacity-50",
              )}
            >
              {submitting && state.decision === "approve" ? (
                <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
              ) : null}
              Approve
            </button>
            <button
              type="button"
              disabled={locked}
              onClick={() => void submit("reject")}
              className="inline-flex items-center gap-2 rounded-lg border border-slate-300 bg-white px-4 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {submitting && state.decision === "reject" ? (
                <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
              ) : null}
              Reject
            </button>
          </div>
        )}

        {state.error ? (
          <p role="alert" className="flex items-start gap-2 text-sm text-red-700">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
            {state.error}
          </p>
        ) : null}
      </CardBody>
    </Card>
  );
}
