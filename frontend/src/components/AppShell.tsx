import Link from "next/link";
import type { ReactNode } from "react";
import { ShoppingBag } from "lucide-react";
import { SessionGate } from "./SessionGate";

export function AppShell({ children }: { children: ReactNode }) {
  return (
    <div className="min-h-screen">
      <header className="border-b border-slate-200 bg-paypal-blue text-white">
        <div className="mx-auto flex h-14 max-w-6xl items-center justify-between px-4">
          <Link href="/runs" className="flex items-center gap-2 font-semibold">
            <ShoppingBag className="h-5 w-5 text-paypal-yellow" aria-hidden />
            PayPal AI
          </Link>
          <nav aria-label="Primary" className="text-sm">
            <Link href="/runs" className="rounded px-3 py-1.5 hover:bg-white/10">
              Runs
            </Link>
          </nav>
        </div>
      </header>
      <main className="mx-auto max-w-6xl px-4 py-8">
        <SessionGate>{children}</SessionGate>
      </main>
    </div>
  );
}
