import { PaymentReturnView } from "@/features/payment/PaymentReturnView";

/**
 * PayPal return_url target. The backend should set `cancel_url` to this same path with
 * `?cancelled=1`. Query params from PayPal (token, PayerID) are deliberately ignored:
 * payment state is decided server-side, never by this redirect.
 */
export default async function PaymentReturnPage({
  params,
  searchParams,
}: {
  params: Promise<{ runId: string }>;
  searchParams: Promise<{ cancelled?: string }>;
}) {
  const [{ runId }, { cancelled }] = await Promise.all([params, searchParams]);

  return (
    <div className="mx-auto max-w-2xl">
      <PaymentReturnView runId={runId} cancelled={cancelled === "1"} />
    </div>
  );
}
