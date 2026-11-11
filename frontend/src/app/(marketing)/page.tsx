import { Hero } from "@/features/landing/Hero";
import { Assurance, Categories, FinalCta, HowItWorks } from "@/features/landing/Sections";

export default function LandingPage() {
  return (
    <main>
      <Hero />
      <Categories />
      <HowItWorks />
      <Assurance />
      <FinalCta />
    </main>
  );
}
