export interface HealthResponse {
  status: string;
  app: string;
  environment: string;
}

export type ApprovalChoice = "approve" | "reject";

export interface ApprovalDecision {
  decision: ApprovalChoice;
  /** SHA-256 hash of the exact terms presented to the user. */
  terms_hash: string;
}

export interface ApprovalResponse {
  run_id: string;
  decision: ApprovalChoice;
  status: "resumed" | "approved" | "completed";
}

export type WebhookStatus = "applied" | "ignored" | "duplicate";

export interface WebhookResponse {
  status: WebhookStatus;
}

/** PayPal webhook event envelope; `resource` is event-type specific. */
export interface PayPalWebhookEvent {
  id: string;
  event_version: string;
  create_time: string;
  resource_type: string;
  event_type: string;
  summary?: string;
  resource: Record<string, unknown>;
}

/** Transmission headers PayPal sends and the backend verifies. */
export interface PayPalWebhookHeaders {
  "paypal-auth-algo": string;
  "paypal-cert-url": string;
  "paypal-transmission-id": string;
  "paypal-transmission-sig": string;
  "paypal-transmission-time": string;
}
