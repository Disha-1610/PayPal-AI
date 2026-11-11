import type { ApprovalRequest } from "@/features/approvals/types";
import type { PaymentInfo } from "@/features/payment/types";
import type { ChatMessage } from "@/features/chat/types";
import type { Candidate } from "@/features/candidates/types";
import type { Milestone } from "@/features/milestones/types";

export type RunStatus =
  | "running"
  | "awaiting_input"
  | "awaiting_approval"
  | "awaiting_payment"
  | "completed"
  | "failed"
  | "stopped";

/** Statuses in which the user can still send messages (constraint changes, answers). */
export const ACTIVE_STATUSES: readonly RunStatus[] = [
  "running",
  "awaiting_input",
  "awaiting_approval",
  "awaiting_payment",
];

export type StepStatus = "pending" | "running" | "done" | "failed" | "skipped";

export type AgentName =
  | "coordinator"
  | "intent"
  | "discovery"
  | "evaluator"
  | "verifier"
  | "executor"
  | "payment"
  | "monitor";

export interface PlanStep {
  id: string;
  title: string;
  agent: AgentName;
  status: StepStatus;
  started_at?: string;
  /** Human-readable outcome of the step. */
  summary?: string;
  /** Structured, display-only key/value outputs for the step inspector. */
  output?: Record<string, string>;
}

export interface Run {
  id: string;
  goal: string;
  /** Task type: "shopping", "contractor" or "a2a". */
  domain?: string;
  status: RunStatus;
  created_at: string;
  updated_at: string;
  budget_usd: number;
  /** ISO 4217 code every amount on the run (budget, prices, approval) is in. */
  currency: string;
  iteration: number;
  max_iterations: number;
  /** Present only while `status` is "awaiting_approval". */
  pending_approval?: ApprovalRequest;
  candidates?: Candidate[];
  /** Contractor runs: the milestone plan and each milestone's progress. */
  milestones?: Milestone[];
  messages?: ChatMessage[];
  payment?: PaymentInfo;
  /** Why the run ended early; set when `status` is "stopped" or "failed". */
  stop_reason?: string;
  steps: PlanStep[];
  /** ISO delivery date the user set or approved. */
  promised_by?: string;
  /** ISO date the counterparty now says delivery will happen; present only after a revision. */
  expected_by?: string;
}

export interface RunPage {
  items: Run[];
  /** Opaque cursor for the next page; null when exhausted. */
  next_cursor: string | null;
}
