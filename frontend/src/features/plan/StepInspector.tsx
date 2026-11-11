import { MousePointerClick } from "lucide-react";
import { Badge, type BadgeTone } from "@/components/Badge";
import { EmptyState } from "@/components/EmptyState";
import { formatDateTime } from "@/lib/utils";
import type { PlanStep, StepStatus } from "@/features/runs/types";

const TONE: Record<StepStatus, BadgeTone> = {
  pending: "neutral",
  running: "info",
  done: "success",
  failed: "danger",
  skipped: "neutral",
};

export function StepInspector({ step }: { step: PlanStep | null }) {
  if (!step) {
    return (
      <EmptyState
        icon={MousePointerClick}
        title="Select a step"
        description="Choose a plan step to inspect its output."
      />
    );
  }

  const outputs = Object.entries(step.output ?? {});

  return (
    <div className="space-y-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h2 className="text-base font-semibold text-slate-900">{step.title}</h2>
          <p className="text-sm capitalize text-slate-500">{step.agent} agent</p>
        </div>
        <Badge tone={TONE[step.status]} className="capitalize">
          {step.status}
        </Badge>
      </div>
      {step.started_at ? (
        <p className="text-xs text-slate-500">Started {formatDateTime(step.started_at)}</p>
      ) : null}
      {step.summary ? <p className="text-sm text-slate-700">{step.summary}</p> : null}
      {outputs.length > 0 ? (
        <dl className="divide-y divide-slate-100 rounded-lg border border-slate-200">
          {outputs.map(([key, value]) => (
            <div key={key} className="flex justify-between gap-4 px-3 py-2 text-sm">
              <dt className="text-slate-500">{key}</dt>
              <dd className="break-all text-right font-medium text-slate-900">{value}</dd>
            </div>
          ))}
        </dl>
      ) : null}
    </div>
  );
}
