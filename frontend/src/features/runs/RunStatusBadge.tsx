import { Badge, type BadgeTone } from "@/components/Badge";
import type { RunStatus } from "./types";

const CONFIG: Record<RunStatus, { label: string; tone: BadgeTone }> = {
  running: { label: "Running", tone: "info" },
  awaiting_input: { label: "Needs your input", tone: "warning" },
  awaiting_counterparty: { label: "Waiting for reply", tone: "info" },
  awaiting_approval: { label: "Awaiting approval", tone: "warning" },
  awaiting_payment: { label: "Awaiting PayPal", tone: "warning" },
  completed: { label: "Completed", tone: "success" },
  failed: { label: "Failed", tone: "danger" },
  stopped: { label: "Stopped", tone: "neutral" },
};

export function RunStatusBadge({ status }: { status: RunStatus }) {
  const { label, tone } = CONFIG[status];
  return <Badge tone={tone}>{label}</Badge>;
}
