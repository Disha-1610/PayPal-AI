import { formatUsd } from "@/lib/utils";
import type { ApprovalRequest } from "./types";

const date = new Intl.DateTimeFormat("en-US", { dateStyle: "medium", timeZone: "UTC" });

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between gap-4 px-3 py-2 text-sm">
      <dt className="text-slate-500">{label}</dt>
      <dd className="text-right font-medium text-slate-900">{value}</dd>
    </div>
  );
}

export function ApprovalTerms({ request }: { request: ApprovalRequest }) {
  const delivery = new Date(request.delivery_by);
  return (
    <dl className="divide-y divide-slate-100 rounded-lg border border-slate-200">
      <Row label="Item" value={request.item} />
      <Row label="Price" value={formatUsd(request.price_usd)} />
      <Row label="Seller" value={request.seller} />
      <Row
        label="Delivery by"
        value={Number.isNaN(delivery.getTime()) ? "—" : date.format(delivery)}
      />
      <Row label="Terms hash" value={`${request.terms_hash.slice(0, 12)}…`} />
    </dl>
  );
}
