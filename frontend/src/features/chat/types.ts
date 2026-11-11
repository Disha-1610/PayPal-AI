import type { AgentName } from "@/features/runs/types";

export type ChatRole = "user" | "agent" | "system";

export interface ChatMessage {
  id: string;
  role: ChatRole;
  /** Set for `agent` messages. */
  agent?: AgentName;
  text: string;
  /** ISO timestamp. */
  created_at: string;
  /** Client-generated id echoed back by the server for user messages (dedupe). */
  client_id?: string;
  /** True when the agent is blocked waiting for the user's answer. */
  is_question?: boolean;
}

export type DeliveryState = "sending" | "failed" | "sent";

export interface DisplayMessage extends ChatMessage {
  /** Only set on messages sent from this client. */
  delivery?: DeliveryState;
}

export interface SendMessageInput {
  text: string;
  client_id: string;
}

export const MESSAGE_MAX = 2000;
