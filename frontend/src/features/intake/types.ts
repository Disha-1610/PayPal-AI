export const CURRENCIES = ["USD", "INR"] as const;
export type Currency = (typeof CURRENCIES)[number];

/** Raw form state; numbers stay strings until validated. */
export interface IntakeValues {
  goal: string;
  max_spend: string;
  currency: Currency;
  /** yyyy-mm-dd or "" when no deadline. */
  deliver_by: string;
}

export type IntakeErrors = Partial<Record<keyof IntakeValues, string>>;

/** Validated payload for `POST /runs`. The Intent agent extracts constraints from `goal`. */
export interface CreateRunInput {
  goal: string;
  max_spend: number;
  currency: Currency;
  deliver_by?: string;
}

export interface CreateRunResponse {
  run_id: string;
}
