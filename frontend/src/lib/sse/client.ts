import { API_BASE_URL, getAuthHeaders } from "@/lib/api";
import { SSEParser, type SSEMessage } from "./parser";

export type SSEConnectionState = "connecting" | "open" | "reconnecting" | "closed";

export interface SSEOptions {
  /** Path relative to the API base URL, e.g. `/runs/abc/events`. */
  path: string;
  onMessage: (message: SSEMessage) => void;
  onState?: (state: SSEConnectionState, error?: string) => void;
}

const INITIAL_BACKOFF_MS = 1_000;
const MAX_BACKOFF_MS = 15_000;
/** Statuses where retrying cannot help (auth, missing resource, bad request). */
const FATAL_STATUSES = new Set([400, 401, 403, 404]);

class FatalStreamError extends Error {}

function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const timer = setTimeout(resolve, ms);
    signal.addEventListener(
      "abort",
      () => {
        clearTimeout(timer);
        resolve();
      },
      { once: true },
    );
  });
}

/**
 * Fetch-based SSE client (EventSource cannot send an Authorization header).
 * Reconnects with jittered exponential backoff and resumes via `Last-Event-ID`.
 * Returns a `close` function that stops the stream.
 */
export function connectSSE({ path, onMessage, onState }: SSEOptions): () => void {
  const controller = new AbortController();
  const { signal } = controller;

  async function run(): Promise<void> {
    let lastEventId: string | undefined;
    let backoff = INITIAL_BACKOFF_MS;
    let first = true;

    while (!signal.aborted) {
      onState?.(first ? "connecting" : "reconnecting");
      first = false;
      try {
        const headers: Record<string, string> = {
          Accept: "text/event-stream",
          ...(await getAuthHeaders()),
        };
        if (lastEventId) headers["Last-Event-ID"] = lastEventId;

        const response = await fetch(`${API_BASE_URL}${path}`, {
          headers,
          credentials: "include",
          signal,
        });
        if (!response.ok || !response.body) {
          if (FATAL_STATUSES.has(response.status)) {
            throw new FatalStreamError(`stream rejected (${response.status})`);
          }
          throw new Error(`stream failed (${response.status})`);
        }

        onState?.("open");
        backoff = INITIAL_BACKOFF_MS;
        const parser = new SSEParser();
        const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          for (const message of parser.push(value)) {
            if (message.id) lastEventId = message.id;
            onMessage(message);
          }
        }
      } catch (err) {
        if (signal.aborted) return;
        if (err instanceof FatalStreamError) {
          onState?.("closed", err.message);
          return;
        }
      }
      if (signal.aborted) return;
      onState?.("reconnecting");
      await sleep(backoff * (0.5 + Math.random() / 2), signal);
      backoff = Math.min(backoff * 2, MAX_BACKOFF_MS);
    }
  }

  void run();
  return () => {
    controller.abort();
    onState?.("closed");
  };
}
