"use client";

import { useState } from "react";
import { Card, CardBody, CardHeader } from "@/components/Card";
import { ChatPanel } from "@/features/chat/ChatPanel";
import { ConnectionBadge } from "@/components/ConnectionBadge";
import { useChat } from "@/hooks/useChat";
import { useRunState } from "@/hooks/useRunState";
import { ApprovalCard } from "@/features/approvals/ApprovalCard";
import { CandidatesPanel } from "@/features/candidates/CandidatesPanel";
import { MilestonesPanel } from "@/features/milestones/MilestonesPanel";
import { OutcomeSummary } from "@/features/payment/OutcomeSummary";
import { PaymentStatusCard } from "@/features/payment/PaymentStatusCard";
import { PlanTimeline } from "@/features/plan/PlanTimeline";
import { StepInspector } from "@/features/plan/StepInspector";
import { RunSummary } from "./RunSummary";
import type { Run } from "./types";

/** Run data is seeded by the page and kept current by SSE; selection is local UI state. */
export function RunDetailView({ run: initialRun }: { run: Run }) {
  const { run, connection } = useRunState(initialRun);
  const [selectedId, setSelectedId] = useState<string | null>(
    run.steps.find((s) => s.status === "running")?.id ?? run.steps[0]?.id ?? null,
  );
  const selected = run.steps.find((s) => s.id === selectedId) ?? null;
  const chat = useChat(run.id, run.messages ?? []);
  const asking = run.status === "awaiting_input";
  const canChangeRequirements = asking || run.can_revise === true;

  function focusComposer() {
    const input = document.getElementById("chat-input");
    document.getElementById("conversation")?.scrollIntoView({ behavior: "smooth", block: "center" });
    input?.focus();
  }

  return (
    <div className="space-y-6">
      <div className="flex justify-end">
        <ConnectionBadge state={connection} />
      </div>
      <RunSummary run={run} />
      <OutcomeSummary run={run} />
      {run.payment ? (
        <PaymentStatusCard payment={run.payment} awaitingPayer={run.status === "awaiting_payment"} />
      ) : null}
      {run.status === "awaiting_approval" && run.pending_approval ? (
        <ApprovalCard key={run.pending_approval.terms_hash} request={run.pending_approval} currency={run.currency} />
      ) : null}
      {run.milestones && run.milestones.length > 0 ? <MilestonesPanel milestones={run.milestones} /> : null}
      <ChatPanel status={run.status} chat={chat} canRevise={run.can_revise === true} />
      {run.candidates && run.candidates.length > 0 ? (
        <CandidatesPanel
          candidates={run.candidates}
          currency={run.currency}
          // The agent resolves the pick from the exact title, the same way a typed reply is read.
          onChoose={canChangeRequirements ? (c) => chat.send(c.title) : undefined}
          onChangeRequirements={canChangeRequirements ? focusComposer : undefined}
        />
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
