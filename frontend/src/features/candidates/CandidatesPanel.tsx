"use client";

import { SearchX } from "lucide-react";
import { Card, CardBody, CardHeader } from "@/components/Card";
import { EmptyState } from "@/components/EmptyState";
import { MAX_COMPARE, useCandidateCompare } from "@/hooks/useCandidateCompare";
import { CandidateCard } from "./CandidateCard";
import { CandidateCompare } from "./CandidateCompare";
import type { Candidate } from "./types";

export function CandidatesPanel({ candidates }: { candidates: readonly Candidate[] }) {
  const { selectedIds, toggle, clear, isFull } = useCandidateCompare();

  const viable = candidates.filter((c) => c.verdict === "viable").sort((a, b) => b.score - a.score);
  const rejected = candidates.filter((c) => c.verdict === "rejected");
  const selected = viable.filter((c) => selectedIds.includes(c.id));

  return (
    <Card>
      <CardHeader>
        <h2 className="text-sm font-semibold text-slate-900">Candidates</h2>
        <p className="mt-0.5 text-xs text-slate-500">
          {viable.length} viable · {rejected.length} rejected · compare up to {MAX_COMPARE}
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
                    rank={i + 1}
                    selected={selectedIds.includes(c.id)}
                    selectDisabled={isFull}
                    onToggleCompare={toggle}
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
                <CandidateCompare items={selected} />
              </section>
            ) : null}

            {rejected.length > 0 ? (
              <details>
                <summary className="cursor-pointer text-sm font-medium text-slate-700">
                  Rejected ({rejected.length})
                </summary>
                <ul className="mt-3 space-y-2">
                  {rejected.map((c) => (
                    <li key={c.id}>
                      <CandidateCard candidate={c} />
                    </li>
                  ))}
                </ul>
              </details>
            ) : null}
          </>
        )}
      </CardBody>
    </Card>
  );
}
