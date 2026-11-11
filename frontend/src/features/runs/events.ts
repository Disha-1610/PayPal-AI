import type { SSEMessage } from "@/lib/sse";
import type { ApprovalRequest } from "@/features/approvals/types";
import type { Candidate } from "@/features/candidates/types";
import type { PlanStep, Run, RunStatus } from "./types";

/**
 * Frontend contract for `GET /runs/{run_id}/events` (not yet implemented in the
 * backend). The SSE `event:` field is the discriminator; `data:` is the JSON payload.
 */
export type RunEvent =
  | { type: "run.snapshot"; run: Run }
  | { type: "run.status"; status: RunStatus; iteration: number; updated_at: string }
  | { type: "step.updated"; step: PlanStep }
  | { type: "candidates.updated"; candidates: Candidate[] }
  | { type: "approval.requested"; approval: ApprovalRequest }
  | { type: "approval.resolved" };

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Parses and shape-checks a stream message; returns null for unknown/invalid events. */
export function parseRunEvent(message: SSEMessage): RunEvent | null {
  let payload: unknown;
  try {
    payload = JSON.parse(message.data);
  } catch {
    return null;
  }
  if (!isRecord(payload)) return null;

  switch (message.event) {
    case "run.snapshot":
      return isRecord(payload.run) && Array.isArray(payload.run.steps)
        ? { type: message.event, run: payload.run as unknown as Run }
        : null;
    case "run.status":
      return typeof payload.status === "string" &&
        typeof payload.iteration === "number" &&
        typeof payload.updated_at === "string"
        ? {
            type: message.event,
            status: payload.status as RunStatus,
            iteration: payload.iteration,
            updated_at: payload.updated_at,
          }
        : null;
    case "step.updated":
      return isRecord(payload.step) && typeof payload.step.id === "string"
        ? { type: message.event, step: payload.step as unknown as PlanStep }
        : null;
    case "candidates.updated":
      return Array.isArray(payload.candidates)
        ? { type: message.event, candidates: payload.candidates as Candidate[] }
        : null;
    case "approval.requested":
      return isRecord(payload.approval) && typeof payload.approval.terms_hash === "string"
        ? { type: message.event, approval: payload.approval as unknown as ApprovalRequest }
        : null;
    case "approval.resolved":
      return { type: message.event };
    default:
      return null;
  }
}

/** Pure reducer: applies one stream event to the run. */
export function applyRunEvent(run: Run, event: RunEvent): Run {
  switch (event.type) {
    case "run.snapshot":
      return event.run;
    case "run.status":
      return {
        ...run,
        status: event.status,
        iteration: event.iteration,
        updated_at: event.updated_at,
      };
    case "step.updated": {
      const exists = run.steps.some((s) => s.id === event.step.id);
      const steps = exists
        ? run.steps.map((s) => (s.id === event.step.id ? event.step : s))
        : [...run.steps, event.step];
      return { ...run, steps };
    }
    case "candidates.updated":
      return { ...run, candidates: event.candidates };
    case "approval.requested":
      return { ...run, status: "awaiting_approval", pending_approval: event.approval };
    case "approval.resolved":
      return { ...run, pending_approval: undefined };
  }
}
