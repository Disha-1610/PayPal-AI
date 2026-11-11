import { CheckCircle2, Circle, Loader2, MinusCircle, XCircle, type LucideIcon } from "lucide-react";
import { cn } from "@/lib/utils";
import type { PlanStep, StepStatus } from "@/features/runs/types";

const STATUS_ICON: Record<StepStatus, { icon: LucideIcon; className: string; label: string }> = {
  pending: { icon: Circle, className: "text-slate-300", label: "Pending" },
  running: { icon: Loader2, className: "animate-spin text-sky-600", label: "Running" },
  done: { icon: CheckCircle2, className: "text-emerald-600", label: "Done" },
  failed: { icon: XCircle, className: "text-red-600", label: "Failed" },
  skipped: { icon: MinusCircle, className: "text-slate-400", label: "Skipped" },
};

interface PlanTimelineProps {
  steps: readonly PlanStep[];
  selectedId: string | null;
  onSelect: (stepId: string) => void;
}

export function PlanTimeline({ steps, selectedId, onSelect }: PlanTimelineProps) {
  return (
    <ol className="space-y-1">
      {steps.map((step) => {
        const { icon: Icon, className, label } = STATUS_ICON[step.status];
        const selected = step.id === selectedId;
        return (
          <li key={step.id}>
            <button
              type="button"
              onClick={() => onSelect(step.id)}
              aria-current={selected ? "step" : undefined}
              className={cn(
                "flex w-full items-center gap-3 rounded-lg px-3 py-2.5 text-left transition",
                "focus:outline-none focus-visible:ring-2 focus-visible:ring-paypal-lightblue",
                selected ? "bg-sky-50 ring-1 ring-sky-200" : "hover:bg-slate-50",
              )}
            >
              <Icon className={cn("h-5 w-5 shrink-0", className)} aria-label={label} />
              <span className="min-w-0 flex-1">
                <span className="block truncate text-sm font-medium text-slate-900">
                  {step.title}
                </span>
                <span className="block text-xs capitalize text-slate-500">{step.agent}</span>
              </span>
            </button>
          </li>
        );
      })}
    </ol>
  );
}
