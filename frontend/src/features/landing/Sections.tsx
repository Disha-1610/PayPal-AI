import Link from "next/link";
import {
  ArrowRight,
  Armchair,
  Dumbbell,
  Headphones,
  Lock,
  Shirt,
  Tag,
  Wrench,
  type LucideIcon,
} from "lucide-react";

interface Category {
  icon: LucideIcon;
  name: string;
  query: string;
  domain?: "contractor";
}

const CATEGORIES: Category[] = [
  { icon: Headphones, name: "Electronics", query: "" },
  { icon: Armchair, name: "Home & Office", query: "" },
  { icon: Shirt, name: "Fashion", query: "" },
  { icon: Dumbbell, name: "Sports & Outdoors", query: "" },
  { icon: Wrench, name: "Services & Freelancers", query: "", domain: "contractor" },
];

function categoryHref({ name, domain }: Category): string {
  const params = new URLSearchParams();
  if (domain) params.set("domain", domain);
  else params.set("goal", name);
  return `/runs/new?${params.toString()}`;
}

export function Categories() {
  return (
    <section id="categories" className="mx-auto max-w-6xl px-4 py-14">
      <h2 className="text-2xl font-bold text-slate-900">Shop by category</h2>
      <ul className="mt-6 grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-5">
        {CATEGORIES.map((category) => (
          <li key={category.name}>
            <Link
              href={categoryHref(category)}
              className="flex h-full flex-col items-center gap-3 rounded-2xl border border-slate-200 bg-white p-6 text-center transition hover:-translate-y-0.5 hover:border-paypal-lightblue hover:shadow-lg"
            >
              <span className="flex h-14 w-14 items-center justify-center rounded-full bg-sky-50 text-paypal-blue">
                <category.icon className="h-7 w-7" aria-hidden />
              </span>
              <span className="text-sm font-semibold text-slate-900">{category.name}</span>
            </Link>
          </li>
        ))}
      </ul>
    </section>
  );
}

const STEPS = [
  { title: "Tell us what you need", body: "Describe the item, your budget and when you need it." },
  { title: "We find the best offers", body: "Aisling compares sellers and negotiates the price for you." },
  { title: "You choose and pay", body: "Review the offer, then check out securely with PayPal." },
];

export function HowItWorks() {
  return (
    <section id="how-it-works" className="bg-slate-50 py-14">
      <div className="mx-auto max-w-6xl px-4">
        <h2 className="text-2xl font-bold text-slate-900">How it works</h2>
        <ol className="mt-6 grid gap-6 md:grid-cols-3">
          {STEPS.map((step, i) => (
            <li key={step.title} className="flex gap-4">
              <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-paypal-blue text-sm font-semibold text-white">
                {i + 1}
              </span>
              <div>
                <h3 className="font-semibold text-slate-900">{step.title}</h3>
                <p className="mt-1 text-sm text-slate-600">{step.body}</p>
              </div>
            </li>
          ))}
        </ol>
      </div>
    </section>
  );
}

export function Assurance() {
  const items = [
    { icon: Lock, title: "Pay securely with PayPal", body: "Your payment details are never shared with sellers." },
    { icon: Tag, title: "You approve every purchase", body: "Nothing is charged until you confirm the final price." },
  ];
  return (
    <section className="mx-auto grid max-w-6xl gap-6 px-4 py-14 sm:grid-cols-2">
      {items.map(({ icon: Icon, title, body }) => (
        <div key={title} className="flex items-start gap-4 rounded-2xl border border-slate-200 p-5">
          <Icon className="mt-0.5 h-6 w-6 shrink-0 text-paypal-blue" aria-hidden />
          <div>
            <h3 className="font-semibold text-slate-900">{title}</h3>
            <p className="mt-1 text-sm text-slate-600">{body}</p>
          </div>
        </div>
      ))}
    </section>
  );
}

export function FinalCta() {
  return (
    <section className="mx-auto max-w-6xl px-4 pb-16">
      <div className="rounded-3xl bg-paypal-blue px-8 py-12 text-center text-white">
        <h2 className="text-2xl font-bold sm:text-3xl">Looking for something specific?</h2>
        <Link
          href="/runs/new"
          className="group mt-6 inline-flex items-center gap-2 rounded-full bg-paypal-yellow px-6 py-3 font-semibold text-slate-900 transition hover:-translate-y-0.5"
        >
          Start searching
          <ArrowRight className="h-4 w-4 transition group-hover:translate-x-1" aria-hidden />
        </Link>
      </div>
    </section>
  );
}
