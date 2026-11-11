"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { postMessage } from "@/features/chat/source";
import type { ChatMessage, DisplayMessage } from "@/features/chat/types";

export interface UseChat {
  messages: readonly DisplayMessage[];
  send: (text: string) => void;
  retry: (clientId: string) => void;
}

/**
 * Merges server-streamed messages with messages sent from this client. Sent messages
 * appear immediately ("sending"), flip to "sent"/"failed", and are dropped from the
 * local list in favour of the server copy once it arrives (matched by id or client_id).
 */
export function useChat(runId: string, serverMessages: readonly ChatMessage[]): UseChat {
  const [local, setLocal] = useState<readonly DisplayMessage[]>([]);
  const controllerRef = useRef<AbortController | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    controllerRef.current = controller;
    return () => controller.abort();
  }, []);

  const patch = useCallback((clientId: string, next: Partial<DisplayMessage>) => {
    setLocal((prev) => prev.map((m) => (m.client_id === clientId ? { ...m, ...next } : m)));
  }, []);

  const deliver = useCallback(
    (clientId: string, text: string) => {
      const signal = controllerRef.current?.signal;
      if (!signal) return;
      patch(clientId, { delivery: "sending" });
      postMessage(runId, { text, client_id: clientId }, signal)
        .then((message) => patch(clientId, { ...message, client_id: clientId, delivery: "sent" }))
        .catch(() => {
          if (!signal.aborted) patch(clientId, { delivery: "failed" });
        });
    },
    [runId, patch],
  );

  const send = useCallback(
    (raw: string) => {
      const text = raw.trim();
      if (!text) return;
      const clientId = crypto.randomUUID();
      setLocal((prev) => [
        ...prev,
        {
          id: clientId,
          client_id: clientId,
          role: "user",
          text,
          created_at: new Date().toISOString(),
          delivery: "sending",
        },
      ]);
      deliver(clientId, text);
    },
    [deliver],
  );

  const retry = useCallback(
    (clientId: string) => {
      const failed = local.find((m) => m.client_id === clientId && m.delivery === "failed");
      if (failed) deliver(clientId, failed.text);
    },
    [local, deliver],
  );

  const messages = useMemo(() => {
    const ids = new Set(serverMessages.map((m) => m.id));
    const clientIds = new Set(serverMessages.flatMap((m) => (m.client_id ? [m.client_id] : [])));
    const pending = local.filter(
      (m) => !ids.has(m.id) && !(m.client_id && clientIds.has(m.client_id)),
    );
    return [...serverMessages, ...pending];
  }, [serverMessages, local]);

  return { messages, send, retry };
}
