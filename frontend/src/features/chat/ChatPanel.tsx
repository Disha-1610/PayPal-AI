"use client";

import { Card, CardHeader } from "@/components/Card";
import { useChat } from "@/hooks/useChat";
import { ACTIVE_STATUSES, type RunStatus } from "@/features/runs/types";
import { Composer } from "./Composer";
import { MessageList } from "./MessageList";
import type { ChatMessage } from "./types";

interface ChatPanelProps {
  runId: string;
  status: RunStatus;
  messages: readonly ChatMessage[];
}

export function ChatPanel({ runId, status, messages: serverMessages }: ChatPanelProps) {
  const { messages, send, retry } = useChat(runId, serverMessages);
  const active = ACTIVE_STATUSES.includes(status);

  return (
    <Card>
      <CardHeader>
        <h2 className="text-sm font-semibold text-slate-900">Conversation</h2>
        <p className="mt-0.5 text-xs text-slate-500">
          {status === "awaiting_input"
            ? "The agent is waiting for your answer."
            : "Answer questions or change what you want. Hard-constraint changes are re-checked."}
        </p>
      </CardHeader>
      <MessageList messages={messages} onRetry={retry} />
      <Composer
        onSend={send}
        disabled={!active}
        placeholder={active ? "Reply to the agent…" : "This run has ended."}
      />
    </Card>
  );
}
