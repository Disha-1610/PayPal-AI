"use client";

import { Card, CardHeader } from "@/components/Card";
import type { UseChat } from "@/hooks/useChat";
import { ACTIVE_STATUSES, type RunStatus } from "@/features/runs/types";
import { Composer } from "./Composer";
import { MessageList } from "./MessageList";

const WAITING_PLACEHOLDER: Partial<Record<RunStatus, string>> = {
  running: "The agent is working…",
  awaiting_approval: "Approve or reject the purchase above.",
  awaiting_payment: "Waiting for you to pay at PayPal.",
};

interface ChatPanelProps {
  status: RunStatus;
  chat: UseChat;
  /** The run stopped before any money moved: a message changes the requirements and runs it again. */
  canRevise?: boolean;
}

export function ChatPanel({ status, chat, canRevise = false }: ChatPanelProps) {
  const { messages, send, retry } = chat;
  // The agent reads replies while it is paused on a question, or when a stopped run may be re-opened with new requirements.
  const asking = status === "awaiting_input";
  const canReply = asking || canRevise;
  const hint = asking
    ? "The agent is waiting for your answer."
    : canRevise
      ? "The run stopped. Describe what to change and it will run again."
      : ACTIVE_STATUSES.includes(status)
        ? "You can reply when the agent asks a question."
        : "This run has ended.";

  return (
    <Card id="conversation">
      <CardHeader>
        <h2 className="text-sm font-semibold text-slate-900">Conversation</h2>
        <p className="mt-0.5 text-xs text-slate-500">{hint}</p>
      </CardHeader>
      <MessageList messages={messages} onRetry={retry} />
      <Composer
        onSend={send}
        disabled={!canReply}
        placeholder={
          asking
            ? "Reply to the agent…"
            : canRevise
              ? "Change the requirements, e.g. raise the price ceiling…"
              : WAITING_PLACEHOLDER[status] ?? "This run has ended."
        }
      />
    </Card>
  );
}
