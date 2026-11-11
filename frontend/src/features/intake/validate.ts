import type { CreateRunInput, IntakeErrors, IntakeValues } from "./types";

export const GOAL_MIN = 10;
export const GOAL_MAX = 1000;

export type ValidationResult =
  | { ok: true; input: CreateRunInput }
  | { ok: false; errors: IntakeErrors };

/** `today` is injectable (yyyy-mm-dd, UTC) so the check is deterministic. */
export function validateIntake(
  values: IntakeValues,
  today: string = new Date().toISOString().slice(0, 10),
): ValidationResult {
  const errors: IntakeErrors = {};

  const goal = values.goal.trim();
  if (goal.length < GOAL_MIN) errors.goal = `Describe what to buy (at least ${GOAL_MIN} characters).`;
  else if (goal.length > GOAL_MAX) errors.goal = `Keep it under ${GOAL_MAX} characters.`;

  const maxSpend = Number(values.max_spend);
  if (values.max_spend.trim() === "" || !Number.isFinite(maxSpend) || maxSpend <= 0) {
    errors.max_spend = "Enter a budget greater than zero.";
  }

  if (values.deliver_by && values.deliver_by < today) {
    errors.deliver_by = "Delivery date cannot be in the past.";
  }

  if (Object.keys(errors).length > 0) return { ok: false, errors };

  return {
    ok: true,
    input: {
      goal,
      max_spend: Math.round(maxSpend * 100) / 100,
      currency: values.currency,
      ...(values.deliver_by ? { deliver_by: values.deliver_by } : {}),
    },
  };
}
