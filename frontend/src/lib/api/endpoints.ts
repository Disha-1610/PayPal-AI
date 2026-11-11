import type {
  ApprovalDecision,
  ApprovalResponse,
  PayPalWebhookEvent,
  PayPalWebhookHeaders,
  WebhookResponse,
} from "@/types/api";
import { request } from "./client";

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
