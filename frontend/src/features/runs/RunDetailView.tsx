"use client";

import { useState } from "react";
import { Card, CardBody, CardHeader } from "@/components/Card";
import { PlanTimeline } from "@/features/plan/PlanTimeline";
import { StepInspector } from "@/features/plan/StepInspector";
import { RunSummary } from "./RunSummary";
import type { Run } from "./types";

/** Selected-step state is local UI state; run data comes from the parent. */
export function RunDetailView({ run }: { run: Run }) {
  const [selectedId, setSelectedId] = useState<string | null>(
    run.steps.find((s) => s.status === "running")?.id ?? run.steps[0]?.id ?? null,
  );
  const selected = run.steps.find((s) => s.id === selectedId) ?? null;

  return (
    <div className="space-y-6">
      <RunSummary run={run} />
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
