export type MilestoneStatus = "proposed" | "pending" | "order_placed" | "settled" | "voided" | "expired";

export interface Milestone {
  sequence: number;
  description: string;
  amount: number;
  currency: string;
  /** yyyy-mm-dd (UTC). */
  due_by: string;
  status: MilestoneStatus;
}
