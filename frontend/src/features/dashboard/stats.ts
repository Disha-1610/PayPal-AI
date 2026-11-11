import type { Run } from "@/features/runs/types";

const DAY_MS = 86_400_000;
const FINISHED: readonly Run["status"][] = ["completed", "failed", "stopped"];

export interface DeliveryWarning {
  run: Run;
  kind: "delayed" | "overdue";
  /** Whole days past the promised date (to the revised date when delayed, to now when overdue). */
  daysLate: number;
}

export interface Spend {
  currency: string;
  amount: number;
}

export function isActive(run: Run): boolean {
  return !FINISHED.includes(run.status);
}

function time(iso: string | undefined): number | null {
  if (!iso) return null;
  const ms = Date.parse(iso);
  return Number.isFinite(ms) ? ms : null;
}

/** The date a run is now expected to arrive: the revised date if there is one, else the promised date. */
export function deliveryDate(run: Run): string | undefined {
  return run.expected_by ?? run.promised_by;
}

/** Runs whose delivery slipped past the promised date, or whose promised date has passed unfinished. */
export function deliveryWarnings(runs: readonly Run[], now: Date): DeliveryWarning[] {
  const warnings: DeliveryWarning[] = [];
  for (const run of runs) {
    const promised = time(run.promised_by);
    if (promised === null || !isActive(run)) continue;
    const expected = time(run.expected_by);
    if (expected !== null && expected > promised) {
      warnings.push({ run, kind: "delayed", daysLate: Math.ceil((expected - promised) / DAY_MS) });
    } else if (now.getTime() > promised) {
      warnings.push({ run, kind: "overdue", daysLate: Math.ceil((now.getTime() - promised) / DAY_MS) });
    }
  }
  return warnings.sort((a, b) => b.daysLate - a.daysLate);
}

/** Paid, still-open runs ordered by when they are expected to arrive (soonest first). */
export function upcomingDeliveries(runs: readonly Run[]): Run[] {
  return runs
    .filter((r) => isActive(r) && r.payment !== undefined && time(deliveryDate(r)) !== null)
    .sort((a, b) => (time(deliveryDate(a)) ?? 0) - (time(deliveryDate(b)) ?? 0));
}

/** Runs waiting on the user: input, approval or PayPal payment. */
export function needsAction(runs: readonly Run[]): Run[] {
  return runs.filter((r) => ["awaiting_input", "awaiting_approval", "awaiting_payment"].includes(r.status));
}

/** Captured payments summed per currency; currencies are never mixed. */
export function totalSpent(runs: readonly Run[]): Spend[] {
  const totals = new Map<string, number>();
  for (const r of runs) {
    if (r.payment?.status === "captured") {
      totals.set(r.payment.currency, (totals.get(r.payment.currency) ?? 0) + r.payment.amount);
    }
  }
  return [...totals].map(([currency, amount]) => ({ currency, amount }));
}

const dateOnly = new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", timeZone: "UTC" });

export function formatDay(iso: string | undefined): string {
  const ms = time(iso);
  return ms === null ? "—" : dateOnly.format(new Date(ms));
}

/** "in 3 days", "today", "2 days ago". */
export function relativeDays(iso: string | undefined, now: Date): string {
  const ms = time(iso);
  if (ms === null) return "";
  const days = Math.ceil((ms - now.getTime()) / DAY_MS);
  if (days === 0) return "today";
  const n = Math.abs(days);
  const unit = n === 1 ? "day" : "days";
  return days > 0 ? `in ${n} ${unit}` : `${n} ${unit} ago`;
}
