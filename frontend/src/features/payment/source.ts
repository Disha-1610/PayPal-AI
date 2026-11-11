import { notifyPayerReturn } from "@/lib/api";

export function reportPayerReturn(runId: string, signal: AbortSignal): Promise<void> {
  return notifyPayerReturn(runId, signal);
}
