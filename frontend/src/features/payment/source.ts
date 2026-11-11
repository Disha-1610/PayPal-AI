import { notifyPayerReturn } from "@/lib/api";

const USE_MOCKS = process.env.NEXT_PUBLIC_USE_MOCKS === "true";

export async function reportPayerReturn(runId: string, signal: AbortSignal): Promise<void> {
  if (USE_MOCKS) return;
  await notifyPayerReturn(runId, signal);
}
