import { Badge, type BadgeTone } from "@/components/Badge";
import { Card, CardBody, CardHeader } from "@/components/Card";
import { formatMoney } from "@/lib/utils";
import type { Milestone, MilestoneStatus } from "./types";

const STATUS: Record<MilestoneStatus, { label: string; tone: BadgeTone }> = {
  proposed: { label: "Proposed", tone: "neutral" },
  pending: { label: "Not started", tone: "neutral" },
  order_placed: { label: "Payment held", tone: "info" },
  settled: { label: "Verified and paid", tone: "success" },
  voided: { label: "Failed verification, not charged", tone: "danger" },
  expired: { label: "Expired, not charged", tone: "warning" },
};

/** A contractor plan: each milestone is approved, paid, verified and captured on its own, in order. */
export function MilestonesPanel({ milestones }: { milestones: Milestone[] }) {
  const total = milestones.reduce((sum, m) => sum + m.amount, 0);
  const currency = milestones[0]?.currency ?? "USD";
  const paid = milestones.filter((m) => m.status === "settled").length;

  return (
    <Card>
      <CardHeader>
        <h2 className="text-sm font-semibold text-slate-900">Milestones</h2>
        <p className="mt-0.5 text-xs text-slate-500">
          {paid} of {milestones.length} paid · {formatMoney(total, currency)} in total
        </p>
      </CardHeader>
      <CardBody>
        <ol className="divide-y divide-slate-100">
          {milestones.map((m) => {
            const status = STATUS[m.status] ?? { label: m.status, tone: "neutral" as BadgeTone };
            return (
              <li key={m.sequence} className="flex flex-wrap items-center justify-between gap-2 py-2">
                <div className="min-w-0">
                  <p className="text-sm font-medium text-slate-900">
                    {m.sequence}. {m.description}
                  </p>
                  <p className="text-xs text-slate-500">
                    {formatMoney(m.amount, m.currency)} · due {m.due_by}
                  </p>
                </div>
                <Badge tone={status.tone}>{status.label}</Badge>
              </li>
            );
          })}
        </ol>
      </CardBody>
    </Card>
  );
}
