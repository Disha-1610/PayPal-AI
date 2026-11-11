import { RunsView } from "@/features/runs/RunsView";

export default function RunsPage() {
  return (
    <div className="space-y-6">
      <h1 className="text-xl font-semibold text-slate-900">Runs</h1>
      <RunsView />
    </div>
  );
}
