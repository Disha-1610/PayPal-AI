import { sendRunMessage } from "@/lib/api";
import type { ChatMessage, SendMessageInput } from "./types";

export function postMessage(
  runId: string,
  input: SendMessageInput,
  signal: AbortSignal,
): Promise<ChatMessage> {
  return sendRunMessage(runId, input, signal);
}
