/**
 * Payer approval uses PayPal's hosted redirect (the backend returns `approval_url`),
 * so no PayPal SDK or client credentials exist in the browser. The redirect and the
 * return page never decide payment state: the backend's webhooks/API status do.
 */

/** Returns the URL only if it is https on paypal.com or a subdomain; otherwise null. */
export function safePayPalUrl(raw: string | null | undefined): string | null {
  if (!raw) return null;
  try {
    const url = new URL(raw);
    const host = url.hostname.toLowerCase();
    const isPayPal = host === "paypal.com" || host.endsWith(".paypal.com");
    return url.protocol === "https:" && isPayPal ? url.toString() : null;
  } catch {
    return null;
  }
}
