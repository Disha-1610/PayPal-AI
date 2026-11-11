export type RunStatus = "running" | "awaiting_approval" | "completed" | "failed" | "stopped";

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
  status: RunStatus;
  created_at: string;
  updated_at: string;
  budget_usd: number;
  iteration: number;
  max_iterations: number;
  steps: PlanStep[];
}
