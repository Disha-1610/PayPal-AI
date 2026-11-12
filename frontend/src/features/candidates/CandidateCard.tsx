import { Star } from "lucide-react";
import { Badge } from "@/components/Badge";
import { Card } from "@/components/Card";
import { cn, formatMoney } from "@/lib/utils";
import type { Candidate } from "./types";

interface CandidateCardProps {
  candidate: Candidate;
  currency: string;
  rank?: number;
  selected?: boolean;
  /** Disables selecting further candidates once the compare limit is hit. */
  selectDisabled?: boolean;
  onToggleCompare?: (id: string) => void;
  /** Shown while the agent waits for the user's pick; the pick is sent as a chat reply. */
  onChoose?: (candidate: Candidate) => void;
}

export function CandidateCard({
  candidate,
  currency,
  rank,
  selected = false,
  selectDisabled = false,
  onToggleCompare,
  onChoose,
}: CandidateCardProps) {
  const warned = candidate.verdict === "warning";
  return (
    <Card className={cn("px-4 py-3", selected && "border-paypal-lightblue ring-1 ring-sky-200")}>
      <div className="flex items-start gap-3">
        {rank !== undefined ? (
          <span className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-slate-100 text-xs font-semibold text-slate-700">
            {rank}
          </span>
        ) : null}
        <div className="min-w-0 flex-1">
          {candidate.product_url ? (
            <a
              href={candidate.product_url}
              target="_blank"
              rel="noopener noreferrer"
              className="block truncate text-sm font-medium text-slate-900 hover:text-paypal-lightblue hover:underline"
            >
              {candidate.title}
            </a>
          ) : (
            <p className="truncate text-sm font-medium text-slate-900">{candidate.title}</p>
          )}
          <p className="text-xs text-slate-500">{candidate.seller}</p>
          <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-sm text-slate-700">
            <span className="font-semibold">{formatMoney(candidate.price_usd, currency)}</span>
            <span className="inline-flex items-center gap-1">
              <Star className="h-3.5 w-3.5 fill-amber-400 text-amber-400" aria-hidden />
              {candidate.rating.toFixed(1)} ({candidate.review_count})
            </span>
            {!candidate.in_stock ? <Badge tone="danger">Out of stock</Badge> : null}
          </div>
          <div
            className="mt-2 h-1.5 overflow-hidden rounded-full bg-slate-100"
            role="meter"
            aria-label="Evaluator score"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={candidate.score}
          >
            <div
              className={cn("h-full rounded-full", warned ? "bg-amber-300" : candidate.score >= 70 ? "bg-emerald-500" : "bg-amber-400")}
              style={{ width: `${Math.max(0, Math.min(100, candidate.score))}%` }}
            />
          </div>
          {rank === 1 && !warned ? <Badge tone="success">Top pick</Badge> : null}
          {warned && candidate.warning_reason ? (
            <p className="mt-2 text-xs text-amber-700">Warning: {candidate.warning_reason}</p>
          ) : null}
        </div>
        <div className="flex flex-col items-end gap-2">
          <Badge tone={warned ? "warning" : "success"}>{warned ? "Warning" : `Score ${candidate.score}`}</Badge>
          {onChoose ? (
            <button
              type="button"
              onClick={() => onChoose(candidate)}
              className="rounded-md border border-paypal-lightblue px-2 py-0.5 text-xs font-medium text-paypal-lightblue hover:bg-sky-50"
            >
              Choose
            </button>
          ) : null}
          {!warned && onToggleCompare ? (
            <label className="flex items-center gap-1.5 text-xs text-slate-600">
              <input
                type="checkbox"
                checked={selected}
                disabled={selectDisabled && !selected}
                onChange={() => onToggleCompare(candidate.id)}
                className="h-3.5 w-3.5"
              />
              Compare
            </label>
          ) : null}
        </div>
      </div>
    </Card>
  );
}
