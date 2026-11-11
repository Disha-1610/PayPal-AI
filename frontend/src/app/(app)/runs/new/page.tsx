import { Suspense } from "react";
import Link from "next/link";
import { ArrowLeft } from "lucide-react";
import { IntakeForm } from "@/features/intake/IntakeForm";

export default function NewRunPage() {
  return (
    <div className="mx-auto max-w-2xl space-y-4">
      <Link
        href="/runs"
        className="inline-flex items-center gap-1 text-sm text-paypal-lightblue hover:underline"
      >
        <ArrowLeft className="h-4 w-4" aria-hidden /> My orders
      </Link>
      <Suspense fallback={null}>
        <IntakeForm />
      </Suspense>
    </div>
  );
}
