import Link from "next/link";
import { ArrowLeft } from "lucide-react";
import { RunDetailLoader } from "@/features/runs/RunDetailLoader";

export default async function RunDetailPage({ params }: { params: Promise<{ runId: string }> }) {
  const { runId } = await params;

  return (
    <div className="space-y-4">
      <Link
        href="/runs"
        className="inline-flex items-center gap-1 text-sm text-paypal-lightblue hover:underline"
      >
        <ArrowLeft className="h-4 w-4" aria-hidden /> All runs
      </Link>
      <RunDetailLoader runId={runId} />
    </div>
  );
}
