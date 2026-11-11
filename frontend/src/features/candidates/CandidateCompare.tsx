import { formatUsd } from "@/lib/utils";
import type { Candidate } from "./types";

const date = new Intl.DateTimeFormat("en-US", { dateStyle: "medium", timeZone: "UTC" });

function formatDate(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "—" : date.format(d);
}

interface Row {
  label: string;
  render: (c: Candidate) => string;
  /** Returns the id(s) of the best candidate for highlighting. */
  best?: (items: readonly Candidate[]) => string;
}

const ROWS: readonly Row[] = [
  {
    label: "Price",
    render: (c) => formatUsd(c.price_usd),
    best: (items) => items.reduce((a, b) => (b.price_usd < a.price_usd ? b : a)).id,
  },
  {
    label: "Rating",
    render: (c) => `${c.rating.toFixed(1)} (${c.review_count})`,
    best: (items) => items.reduce((a, b) => (b.rating > a.rating ? b : a)).id,
  },
  {
    label: "Delivery by",
    render: (c) => formatDate(c.delivery_by),
    best: (items) =>
      items.reduce((a, b) => (Date.parse(b.delivery_by) < Date.parse(a.delivery_by) ? b : a)).id,
  },
  { label: "Seller", render: (c) => c.seller },
  { label: "In stock", render: (c) => (c.in_stock ? "Yes" : "No") },
  {
    label: "Score",
    render: (c) => String(c.score),
    best: (items) => items.reduce((a, b) => (b.score > a.score ? b : a)).id,
  },
];

export function CandidateCompare({ items }: { items: readonly Candidate[] }) {
  if (items.length < 2) {
    return (
      <p className="text-sm text-slate-500">Select at least two candidates to compare them.</p>
    );
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-slate-200 text-left">
            <th className="py-2 pr-4 font-medium text-slate-500">
              <span className="sr-only">Attribute</span>
            </th>
            {items.map((c) => (
              <th key={c.id} className="px-3 py-2 font-medium text-slate-900">
                {c.title}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {ROWS.map((row) => {
            const bestId = row.best?.(items);
            return (
              <tr key={row.label}>
                <th scope="row" className="py-2 pr-4 text-left font-normal text-slate-500">
                  {row.label}
                </th>
                {items.map((c) => (
                  <td
                    key={c.id}
                    className={
                      c.id === bestId
                        ? "px-3 py-2 font-semibold text-emerald-700"
                        : "px-3 py-2 text-slate-900"
                    }
                  >
                    {row.render(c)}
                  </td>
                ))}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
