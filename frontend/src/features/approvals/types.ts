import type { ApprovalChoice } from "@/types/api";

/** Exact terms the user is asked to approve; `terms_hash` binds the decision to them. */
export interface ApprovalRequest {
  run_id: string;
  item: string;
  price_usd: number;
  seller: string;
  /** ISO date by which the seller promises delivery. */
  delivery_by: string;
  terms_hash: string;
}

export type ApprovalPhase = "idle" | "submitting" | "resolved" | "stale" | "error";

export interface ApprovalState {
  phase: ApprovalPhase;
  /** Optimistic while submitting; confirmed once resolved. */
  decision: ApprovalChoice | null;
  error: string | null;
}
