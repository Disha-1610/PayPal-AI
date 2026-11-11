import Link from "next/link";
import type { ReactNode } from "react";
import { ShoppingBag } from "lucide-react";

export default function MarketingLayout({ children }: { children: ReactNode }) {
  return (
    <div className="min-h-screen bg-white">
      <header className="sticky top-0 z-30 border-b border-slate-200 bg-white/90 backdrop-blur">
        <div className="mx-auto flex h-16 max-w-6xl items-center justify-between px-4">
          <Link href="/" className="flex items-center gap-2 text-lg font-bold text-paypal-blue">
            <ShoppingBag className="h-5 w-5" aria-hidden />
            Aisling
          </Link>
          <nav aria-label="Primary" className="flex items-center gap-1 text-sm text-slate-700">
            <a href="#categories" className="hidden rounded px-3 py-1.5 hover:bg-slate-100 sm:block">Categories</a>
            <a href="#how-it-works" className="hidden rounded px-3 py-1.5 hover:bg-slate-100 sm:block">How it works</a>
            <Link href="/runs" className="rounded px-3 py-1.5 hover:bg-slate-100">My orders</Link>
            <Link href="/dashboard" className="ml-2 rounded-full bg-paypal-blue px-4 py-1.5 font-semibold text-white hover:bg-paypal-darkblue">
              My account
            </Link>
          </nav>
        </div>
      </header>
      {children}
      <footer className="border-t border-slate-200 py-8 text-center text-sm text-slate-500">
        © {new Date().getFullYear()} Aisling. Payments processed securely by PayPal.
      </footer>
    </div>
  );
}
