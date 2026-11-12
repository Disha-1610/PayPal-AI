export interface Candidate {
  id: string;
  title: string;
  seller: string;
  price_usd: number;
  /** 0–5 */
  rating: number;
  review_count: number;
  /** ISO date promised by the seller. */
  delivery_by: string;
  in_stock: boolean;
  /** Listing page on the store the product was found at. */
  product_url?: string;
  /** Evaluator score, 0–100. */
  score: number;
  verdict: "viable" | "rejected";
  /** Set when `verdict` is "rejected". */
  rejection_reason?: string;
}
