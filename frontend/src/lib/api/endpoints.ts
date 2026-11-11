import type {
  ApprovalDecision,
  ApprovalResponse,
  PayPalWebhookEvent,
  PayPalWebhookHeaders,
  WebhookResponse,
} from "@/types/api";
import type { ChatMessage, SendMessageInput } from "@/features/chat/types";
import type { CreateRunInput, CreateRunResponse } from "@/features/intake/types";
import type { Run, RunPage } from "@/features/runs/types";
import { ApiError, request } from "./client";

export interface ListRunsParams {
  limit?: number;
  cursor?: string | null;
}

function malformed(what: string): ApiError {
  return new ApiError(502, `unexpected ${what} response`);
}

function isRun(value: unknown): value is Run {
  if (typeof value !== "object" || value === null) return false;
  const run = value as Partial<Run>;
  return typeof run.id === "string" && typeof run.status === "string" && Array.isArray(run.steps);
}

/** List runs, newest first, one cursor page at a time. */
export async function listRuns(
  { limit = 20, cursor }: ListRunsParams = {},
  signal?: AbortSignal,
): Promise<RunPage> {
  const query = new URLSearchParams({ limit: String(limit) });
  if (cursor) query.set("cursor", cursor);
  const page = await request<RunPage>(`/runs?${query}`, { signal });
  if (!page || !Array.isArray(page.items) || !page.items.every(isRun)) throw malformed("runs");
  return { items: page.items, next_cursor: page.next_cursor ?? null };
}

/**
 * Start a run. The idempotency key makes a retried/double submit return the same run
 * instead of creating a second task.
 */
export async function createRun(
  input: CreateRunInput,
  idempotencyKey: string,
  signal?: AbortSignal,
): Promise<CreateRunResponse> {
  const response = await request<CreateRunResponse>("/runs", {
    method: "POST",
    body: input,
    headers: { "Idempotency-Key": idempotencyKey },
    signal,
  });
  if (!response || typeof response.run_id !== "string") throw malformed("create run");
  return { run_id: response.run_id };
}

/**
 * Send a user message into a run (answer a question or change a constraint).
 * `client_id` doubles as the idempotency key so retries never duplicate the message.
 */
export async function sendRunMessage(
  runId: string,
  input: SendMessageInput,
  signal?: AbortSignal,
): Promise<ChatMessage> {
  const message = await request<ChatMessage>(`/runs/${encodeURIComponent(runId)}/messages`, {
    method: "POST",
    body: input,
    headers: { "Idempotency-Key": input.client_id },
    signal,
  });
  if (!message || typeof message.id !== "string" || typeof message.text !== "string") {
    throw malformed("message");
  }
  return message;
}

/**
 * Tell the backend the payer came back from PayPal so the paused run can resume.
 * Sends no PayPal params: the backend verifies the order itself, and the outcome
 * arrives via webhooks / run state, never from this call.
 */
export async function notifyPayerReturn(runId: string, signal?: AbortSignal): Promise<void> {
  await request<unknown>(`/runs/${encodeURIComponent(runId)}/payer-return`, {
    method: "POST",
    signal,
  });
}

/** Fetch a single run. 404 means it does not exist or is not visible to the caller. */
export async function getRun(runId: string, signal?: AbortSignal): Promise<Run> {
  const run = await request<Run>(`/runs/${encodeURIComponent(runId)}`, { signal });
  if (!isRun(run)) throw malformed("run");
  return run;
}

/**
 * Submit a HITL approval/rejection. 409 means no pending approval or the terms
 * changed (hash mismatch): refetch and re-present the current terms.
 */
export function submitApproval(
  runId: string,
  decision: ApprovalDecision,
  signal?: AbortSignal,
): Promise<ApprovalResponse> {
  return request<ApprovalResponse>(`/runs/${encodeURIComponent(runId)}/approval`, {
    method: "POST",
    body: decision,
    signal,
  });
}

/**
 * Webhook ingest. PayPal calls this server-to-server; exposed only for local
 * sandbox/testing tools — the browser must never hold real transmission signatures.
 */
export function postPayPalWebhook(
  event: PayPalWebhookEvent,
  headers: PayPalWebhookHeaders,
  signal?: AbortSignal,
): Promise<WebhookResponse> {
  return request<WebhookResponse>("/webhooks/paypal", {
    method: "POST",
    body: event,
    headers: { ...headers },
    signal,
  });
}
