import { AlertTriangle, CheckCircle2, Circle, ExternalLink } from "lucide-react";
import { Badge, type BadgeTone } from "@/components/Badge";
import { Card, CardBody, CardHeader } from "@/components/Card";
import { safePayPalUrl } from "@/lib/paypal/approval";
import { cn, formatDateTime, formatMoney } from "@/lib/utils";
import type { PaymentInfo, PaymentStatus } from "./types";

/** Happy-path milestones; a payment is "at" the rank of its status. */
const PATH: readonly { status: PaymentStatus; label: string }[] = [
  { status: "created", label: "Order created" },
  { status: "authorized", label: "Funds held" },
  { status: "captured", label: "Payment captured" },
];

const RANK: Record<PaymentStatus, number> = {
  created: 0,
  authorized: 1,
  captured: 2,
  refunded: 2,
  voided: 0,
  failed: 0,
};

const OFF_PATH: Partial<Record<PaymentStatus, { label: string; tone: BadgeTone }>> = {
  voided: { label: "Authorization voided — you were not charged", tone: "neutral" },
  refunded: { label: "Refunded", tone: "info" },
  failed: { label: "Payment failed", tone: "danger" },
};

interface PaymentStatusCardProps {
  payment: PaymentInfo;
  /** True while the run is paused waiting for the payer at PayPal. */
  awaitingPayer: boolean;
}

export function PaymentStatusCard({ payment, awaitingPayer }: PaymentStatusCardProps) {
  const rank = RANK[payment.status];
  const offPath = OFF_PATH[payment.status];
  const approvalUrl =
    awaitingPayer && payment.status === "created" ? safePayPalUrl(payment.approval_url) : null;
  const badApprovalUrl =
    awaitingPayer && payment.status === "created" && payment.approval_url && !approvalUrl;

  return (
    <Card>
      <CardHeader>
        <div className="flex items-start justify-between gap-3">
          <div>
            <h2 className="text-sm font-semibold text-slate-900">Payment</h2>
            <p className="mt-0.5 text-xs text-slate-500">
              Order {payment.order_id} · Updated {formatDateTime(payment.updated_at)}
            </p>
          </div>
          <span className="text-sm font-semibold text-slate-900">
            {formatMoney(payment.amount, payment.currency)}
          </span>
        </div>
      </CardHeader>
      <CardBody className="space-y-4">
        <ol className="space-y-2">
          {PATH.map((step, index) => {
            const reached = index <= rank;
            return (
              <li key={step.status} className="flex items-center gap-2 text-sm">
                {reached ? (
                  <CheckCircle2 className="h-4 w-4 text-emerald-600" aria-hidden />
                ) : (
                  <Circle className="h-4 w-4 text-slate-300" aria-hidden />
                )}
                <span className={cn(reached ? "text-slate-900" : "text-slate-400")}>
                  {step.label}
                </span>
              </li>
            );
          })}
        </ol>

        {offPath ? <Badge tone={offPath.tone}>{offPath.label}</Badge> : null}

        {approvalUrl ? (
          <div className="space-y-2">
            <p className="text-sm text-slate-700">
              Approve this payment on PayPal. Funds are only held, and captured after the agent
              verifies your order.
            </p>
            <a
              href={approvalUrl}
              rel="noopener noreferrer"
              className="inline-flex items-center gap-2 rounded-lg bg-paypal-yellow px-4 py-2 text-sm font-semibold text-paypal-darkblue hover:brightness-95"
            >
              Approve on PayPal <ExternalLink className="h-4 w-4" aria-hidden />
            </a>
          </div>
        ) : null}

        {badApprovalUrl ? (
          <p role="alert" className="flex items-start gap-2 text-sm text-red-700">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
            The PayPal approval link looks invalid, so it was blocked. Contact support.
          </p>
        ) : null}
      </CardBody>
    </Card>
  );
}
