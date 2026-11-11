import { CheckCircle2, OctagonX, XCircle, type LucideIcon } from "lucide-react";
import { Card, CardBody } from "@/components/Card";
import { formatMoney } from "@/lib/utils";
import type { Run } from "@/features/runs/types";

interface Outcome {
  icon: LucideIcon;
  iconClass: string;
  title: string;
  detail: string;
}

function describe(run: Run): Outcome | null {
  switch (run.status) {
    case "completed": {
      const paid = run.payment?.status === "captured" ? run.payment : null;
      return {
        icon: CheckCircle2,
        iconClass: "text-emerald-600",
        title: "Purchase complete",
        detail: paid
          ? `${formatMoney(paid.amount, paid.currency)} was captured after the order was verified.`
          : "The task finished successfully.",
      };
    }
    case "stopped":
      return {
        icon: OctagonX,
        iconClass: "text-slate-500",
        title: "Task stopped",
        detail: run.stop_reason ?? "The agent stopped before completing the purchase. No charge was made.",
      };
    case "failed":
      return {
        icon: XCircle,
        iconClass: "text-red-600",
        title: "Task failed",
        detail: run.stop_reason ?? "Something went wrong. Check the payment status above.",
      };
    default:
      return null; // run still active
  }
}

/** Final report for a finished run; renders nothing while the run is active. */
export function OutcomeSummary({ run }: { run: Run }) {
  const outcome = describe(run);
  if (!outcome) return null;
  const Icon = outcome.icon;

  return (
    <Card>
      <CardBody className="flex items-start gap-3">
        <Icon className={`mt-0.5 h-5 w-5 shrink-0 ${outcome.iconClass}`} aria-hidden />
        <div>
          <h2 className="text-sm font-semibold text-slate-900">{outcome.title}</h2>
          <p className="mt-0.5 text-sm text-slate-600">{outcome.detail}</p>
        </div>
      </CardBody>
    </Card>
  );
}
