/** Mirrors the backend `PaymentStatus` ledger lifecycle. */
export type PaymentStatus = "created" | "authorized" | "captured" | "voided" | "refunded" | "failed";

export interface PaymentInfo {
  status: PaymentStatus;
  amount: number;
  currency: string;
  order_id: string;
  /** PayPal payer-approval link; present while the payer still has to approve. */
  approval_url: string | null;
  /** ISO timestamp of the last ledger change. */
  updated_at: string;
}
