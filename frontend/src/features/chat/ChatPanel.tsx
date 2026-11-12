"use client";

import { Card, CardHeader } from "@/components/Card";
import { useChat } from "@/hooks/useChat";
import { ACTIVE_STATUSES, type RunStatus } from "@/features/runs/types";
import { Composer } from "./Composer";
import { MessageList } from "./MessageList";
import type { ChatMessage } from "./types";

const WAITING_PLACEHOLDER: Partial<Record<RunStatus, string>> = {
  running: "The agent is working…",
  awaiting_approval: "Approve or reject the purchase above.",
  awaiting_payment: "Waiting for you to pay at PayPal.",
};

interface ChatPanelProps {
  runId: string;
  status: RunStatus;
  messages: readonly ChatMessage[];
}

export function ChatPanel({ runId, status, messages: serverMessages }: ChatPanelProps) {
  const { messages, send, retry } = useChat(runId, serverMessages);
  // The agent reads replies only while it is paused on a question; at any other point the server refuses them.
  const asking = status === "awaiting_input";
  const hint = asking
    ? "The agent is waiting for your answer."
    : ACTIVE_STATUSES.includes(status)
      ? "You can reply when the agent asks a question."
      : "This run has ended.";

  return (
    <Card>
      <CardHeader>
        <h2 className="text-sm font-semibold text-slate-900">Conversation</h2>
        <p className="mt-0.5 text-xs text-slate-500">{hint}</p>
      </CardHeader>
      <MessageList messages={messages} onRetry={retry} />
      <Composer
        onSend={send}
        disabled={!asking}
        placeholder={asking ? "Reply to the agent…" : WAITING_PLACEHOLDER[status] ?? "This run has ended."}
      />
    </Card>
  );
}
