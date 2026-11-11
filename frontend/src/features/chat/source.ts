import { sendRunMessage } from "@/lib/api";
import type { ChatMessage, SendMessageInput } from "./types";

const USE_MOCKS = process.env.NEXT_PUBLIC_USE_MOCKS === "true";

export async function postMessage(
  runId: string,
  input: SendMessageInput,
  signal: AbortSignal,
): Promise<ChatMessage> {
  if (!USE_MOCKS) return sendRunMessage(runId, input, signal);
  return {
    id: input.client_id,
    role: "user",
    text: input.text,
    created_at: new Date().toISOString(),
    client_id: input.client_id,
  };
}
