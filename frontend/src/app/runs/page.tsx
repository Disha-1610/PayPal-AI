import Link from "next/link";
import { Plus } from "lucide-react";
import { RunsView } from "@/features/runs/RunsView";

export default function RunsPage() {
  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold text-slate-900">Runs</h1>
        <Link
          href="/runs/new"
          className="inline-flex items-center gap-1.5 rounded-lg bg-paypal-blue px-3 py-2 text-sm font-medium text-white hover:bg-paypal-darkblue"
        >
          <Plus className="h-4 w-4" aria-hidden /> New task
        </Link>
      </div>
      <RunsView />
    </div>
  );
}
