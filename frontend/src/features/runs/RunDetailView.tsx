"use client";

import { useState } from "react";
import { Card, CardBody, CardHeader } from "@/components/Card";
import { ChatPanel } from "@/features/chat/ChatPanel";
import { ConnectionBadge } from "@/components/ConnectionBadge";
import { useRunState } from "@/hooks/useRunState";
import { ApprovalCard } from "@/features/approvals/ApprovalCard";
import { CandidatesPanel } from "@/features/candidates/CandidatesPanel";
import { PlanTimeline } from "@/features/plan/PlanTimeline";
import { StepInspector } from "@/features/plan/StepInspector";
import { RunSummary } from "./RunSummary";
import type { Run } from "./types";

/** Live streaming is opt-in until the backend exposes `/runs/{id}/events`. */
const LIVE = process.env.NEXT_PUBLIC_ENABLE_SSE === "true";

/** Run data is seeded by the page and kept current by SSE; selection is local UI state. */
export function RunDetailView({ run: initialRun }: { run: Run }) {
  const { run, connection } = useRunState(initialRun, { enabled: LIVE });
  const [selectedId, setSelectedId] = useState<string | null>(
    run.steps.find((s) => s.status === "running")?.id ?? run.steps[0]?.id ?? null,
  );
  const selected = run.steps.find((s) => s.id === selectedId) ?? null;

  return (
    <div className="space-y-6">
      {LIVE ? (
        <div className="flex justify-end">
          <ConnectionBadge state={connection} />
        </div>
      ) : null}
      <RunSummary run={run} />
      {run.status === "awaiting_approval" && run.pending_approval ? (
        <ApprovalCard key={run.pending_approval.terms_hash} request={run.pending_approval} />
      ) : null}
      <ChatPanel runId={run.id} status={run.status} messages={run.messages ?? []} />
      {run.candidates && run.candidates.length > 0 ? (
        <CandidatesPanel candidates={run.candidates} />
      ) : null}
      <div className="grid gap-6 lg:grid-cols-5">
        <Card className="lg:col-span-2">
          <CardHeader>
            <h2 className="text-sm font-semibold text-slate-900">Plan</h2>
          </CardHeader>
          <CardBody>
            <PlanTimeline steps={run.steps} selectedId={selectedId} onSelect={setSelectedId} />
          </CardBody>
        </Card>
        <Card className="lg:col-span-3">
          <CardHeader>
            <h2 className="text-sm font-semibold text-slate-900">Step inspector</h2>
          </CardHeader>
          <CardBody>
            <StepInspector step={selected} />
          </CardBody>
        </Card>
      </div>
    </div>
  );
}
