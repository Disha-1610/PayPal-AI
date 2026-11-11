import { Search } from "lucide-react";

const POPULAR = [
  "4K monitor",
  "Standing desk",
  "Running shoes",
  "Noise-cancelling headphones",
  "Office chair",
];

export function Hero() {
  return (
    <section className="bg-gradient-to-b from-sky-50 to-white">
      <div className="mx-auto max-w-3xl px-4 pb-16 pt-16 text-center sm:pt-24">
        <h1 className="text-4xl font-bold tracking-tight text-slate-900 sm:text-5xl">
          Find it. Compare it. Get the best price.
        </h1>
        <p className="mt-4 text-lg text-slate-600">
          Tell Aisling what you&apos;re looking for and we&apos;ll handle the searching and haggling.
        </p>

        <form action="/runs/new" method="get" role="search" className="mt-8">
          <div className="flex overflow-hidden rounded-full border border-slate-300 bg-white shadow-md focus-within:border-paypal-lightblue focus-within:ring-2 focus-within:ring-paypal-lightblue/30">
            <label htmlFor="hero-search" className="sr-only">
              What are you looking for?
            </label>
            <input
              id="hero-search"
              name="goal"
              type="text"
              required
              maxLength={500}
              placeholder="What are you looking for?"
              className="min-w-0 flex-1 bg-transparent px-6 py-4 text-base text-slate-900 placeholder:text-slate-400 focus:outline-none"
            />
            <button
              type="submit"
              aria-label="Search"
              className="m-1.5 inline-flex items-center gap-2 rounded-full bg-paypal-blue px-5 font-semibold text-white transition hover:bg-paypal-darkblue"
            >
              <Search className="h-4 w-4" aria-hidden />
              <span className="hidden sm:inline">Search</span>
            </button>
          </div>
        </form>

        <ul className="mt-5 flex flex-wrap justify-center gap-2 text-sm">
          <li className="py-1 text-slate-500">Popular:</li>
          {POPULAR.map((term) => (
            <li key={term}>
              <a
                href={`/runs/new?goal=${encodeURIComponent(term)}`}
                className="inline-block rounded-full border border-slate-200 bg-white px-3 py-1 text-slate-700 transition hover:border-paypal-lightblue hover:text-paypal-blue"
              >
                {term}
              </a>
            </li>
          ))}
        </ul>
      </div>
    </section>
  );
}
