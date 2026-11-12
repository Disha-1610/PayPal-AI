"use client";

import { SearchX } from "lucide-react";
import { Card, CardBody, CardHeader } from "@/components/Card";
import { EmptyState } from "@/components/EmptyState";
import { MAX_COMPARE, useCandidateCompare } from "@/hooks/useCandidateCompare";
import { CandidateCard } from "./CandidateCard";
import { CandidateCompare } from "./CandidateCompare";
import type { Candidate } from "./types";

interface CandidatesPanelProps {
  candidates: readonly Candidate[];
  currency: string;
  /** The agent is waiting for the user's pick: every candidate, warnings included, can be chosen. */
  onChoose?: (candidate: Candidate) => void;
  /** The user may change the requirements (by replying, or by re-opening a stopped run). */
  onChangeRequirements?: () => void;
}

export function CandidatesPanel({ candidates, currency, onChoose, onChangeRequirements }: CandidatesPanelProps) {
  const { selectedIds, toggle, clear, isFull } = useCandidateCompare();

  const viable = candidates.filter((c) => c.verdict === "viable").sort((a, b) => b.score - a.score);
  const warned = candidates.filter((c) => c.verdict === "warning");
  const selected = viable.filter((c) => selectedIds.includes(c.id));

  return (
    <Card>
      <CardHeader>
        <h2 className="text-sm font-semibold text-slate-900">Candidates</h2>
        <p className="mt-0.5 text-xs text-slate-500">
          {viable.length} viable · {warned.length} with warnings · compare up to {MAX_COMPARE}
        </p>
      </CardHeader>
      <CardBody className="space-y-6">
        {candidates.length === 0 ? (
          <EmptyState
            icon={SearchX}
            title="No candidates yet"
            description="Results appear after discovery and evaluation."
          />
        ) : (
          <>
            <ul className="space-y-2">
              {viable.map((c, i) => (
                <li key={c.id}>
                  <CandidateCard
                    candidate={c}
                    currency={currency}
                    rank={i + 1}
                    selected={selectedIds.includes(c.id)}
                    selectDisabled={isFull}
                    onToggleCompare={toggle}
                    onChoose={onChoose}
                  />
                </li>
              ))}
            </ul>

            {selectedIds.length > 0 ? (
              <section aria-label="Comparison" className="space-y-3">
                <div className="flex items-center justify-between">
                  <h3 className="text-sm font-semibold text-slate-900">Comparison</h3>
                  <button
                    type="button"
                    onClick={clear}
                    className="text-xs text-paypal-lightblue hover:underline"
                  >
                    Clear
                  </button>
                </div>
                <CandidateCompare items={selected} currency={currency} />
              </section>
            ) : null}

            {warned.length > 0 ? (
              <details open={viable.length === 0}>
                <summary className="cursor-pointer text-sm font-medium text-slate-700">
                  With warnings ({warned.length})
                </summary>
                <p className="mt-1 text-xs text-slate-500">
                  These do not fully meet your requirements, but you can still choose one.
                </p>
                <ul className="mt-3 space-y-2">
                  {warned.map((c) => (
                    <li key={c.id}>
                      <CandidateCard candidate={c} currency={currency} onChoose={onChoose} />
                    </li>
                  ))}
                </ul>
              </details>
            ) : null}

            {onChangeRequirements ? (
              <div className="flex items-center justify-between rounded-lg bg-slate-50 px-4 py-3 text-xs text-slate-600">
                <span>Not what you are looking for?</span>
                <button
                  type="button"
                  onClick={onChangeRequirements}
                  className="font-medium text-paypal-lightblue hover:underline"
                >
                  Change requirements
                </button>
              </div>
            ) : null}
          </>
        )}
      </CardBody>
    </Card>
  );
}
