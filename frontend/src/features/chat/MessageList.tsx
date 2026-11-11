"use client";

import { useEffect, useRef } from "react";
import { MessagesSquare } from "lucide-react";
import { EmptyState } from "@/components/EmptyState";
import { MessageBubble } from "./MessageBubble";
import type { DisplayMessage } from "./types";

interface MessageListProps {
  messages: readonly DisplayMessage[];
  onRetry: (clientId: string) => void;
}

export function MessageList({ messages, onRetry }: MessageListProps) {
  const containerRef = useRef<HTMLDivElement>(null);

  // Scroll the list itself (not the page) to the newest message.
  useEffect(() => {
    const el = containerRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages.length]);

  return (
    <div
      ref={containerRef}
      role="log"
      aria-live="polite"
      aria-label="Conversation"
      className="max-h-96 min-h-48 space-y-4 overflow-y-auto px-5 py-4"
    >
      {messages.length === 0 ? (
        <EmptyState
          icon={MessagesSquare}
          title="No messages yet"
          description="Agent questions and your replies appear here."
        />
      ) : (
        messages.map((m) => <MessageBubble key={m.id} message={m} onRetry={onRetry} />)
      )}
    </div>
  );
}
