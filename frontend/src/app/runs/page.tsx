import { RunList } from "@/features/runs/RunList";
import { MOCK_RUNS } from "@/features/runs/mock";

export default function RunsPage() {
  return (
    <div className="space-y-6">
      <h1 className="text-xl font-semibold text-slate-900">Runs</h1>
      <RunList runs={MOCK_RUNS} />
    </div>
  );
}
