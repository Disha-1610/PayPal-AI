import { Badge, type BadgeTone } from "@/components/Badge";
import type { SSEConnectionState } from "@/lib/sse";

const CONFIG: Record<SSEConnectionState, { label: string; tone: BadgeTone }> = {
  connecting: { label: "Connecting", tone: "info" },
  open: { label: "Live", tone: "success" },
  reconnecting: { label: "Reconnecting", tone: "warning" },
  closed: { label: "Offline", tone: "neutral" },
};

export function ConnectionBadge({ state }: { state: SSEConnectionState }) {
  const { label, tone } = CONFIG[state];
  return <Badge tone={tone}>{label}</Badge>;
}
