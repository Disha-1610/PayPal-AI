import Link from "next/link";
import { notFound } from "next/navigation";
import { ArrowLeft } from "lucide-react";
import { RunDetailView } from "@/features/runs/RunDetailView";
import { getMockRun } from "@/features/runs/mock";

export default async function RunDetailPage({ params }: { params: Promise<{ runId: string }> }) {
  const { runId } = await params;
  const run = getMockRun(runId);
  if (!run) notFound();

  return (
    <div className="space-y-4">
      <Link
        href="/runs"
        className="inline-flex items-center gap-1 text-sm text-paypal-lightblue hover:underline"
      >
        <ArrowLeft className="h-4 w-4" aria-hidden /> All runs
      </Link>
      <RunDetailView run={run} />
    </div>
  );
}
