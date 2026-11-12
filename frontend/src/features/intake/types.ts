// PayPal rejects INR orders (CURRENCY_NOT_SUPPORTED), so only currencies it accepts are offered.
export const CURRENCIES = ["USD"] as const;
export type Currency = (typeof CURRENCIES)[number];

export const DOMAINS = ["shopping", "contractor", "a2a"] as const;
export type Domain = (typeof DOMAINS)[number];

/** Copy per task type. Contractor and agent-to-agent tasks need a hard completion deadline. */
export const DOMAIN_COPY: Record<
  Domain,
  { label: string; goalLabel: string; placeholder: string; deadlineLabel: string; deadlineRequired: boolean }
> = {
  shopping: {
    label: "Buy a product",
    goalLabel: "What should I buy?",
    placeholder: "A 27-inch 4K monitor, USB-C preferred, delivered by Friday",
    deadlineLabel: "Deliver by (optional)",
    deadlineRequired: false,
  },
  contractor: {
    label: "Hire a contractor",
    goalLabel: "What work should the contractor do?",
    placeholder: "Redesign our landing page in React, NDA required, remote is fine",
    deadlineLabel: "Work completed by",
    deadlineRequired: true,
  },
  a2a: {
    label: "Negotiate with another agent",
    goalLabel: "What should I negotiate?",
    placeholder: "Label 10k product images with LabelBot, pay at most 450",
    deadlineLabel: "Result delivered by",
    deadlineRequired: true,
  },
};

/** Raw form state; numbers stay strings until validated. */
export interface IntakeValues {
  domain: Domain;
  goal: string;
  max_spend: string;
  currency: Currency;
  /** yyyy-mm-dd or "" when no deadline. */
  deliver_by: string;
  /** Contractor / agent-to-agent: who the agent negotiates with. Ignored for shopping. */
  cp_name: string;
  cp_email: string;
  /** International format, e.g. +14155550123. */
  cp_phone: string;
  /** Reach the phone by WhatsApp instead of SMS. */
  cp_whatsapp: boolean;
  cp_website: string;
  /** The other agent's https endpoint (agent-to-agent only). */
  cp_endpoint: string;
}

export type IntakeErrors = Partial<Record<keyof IntakeValues | "cp_contact", string>>;

export type CounterpartyChannel = "email" | "sms" | "whatsapp";

/** Who a contractor / agent-to-agent run negotiates with. Mirrors the backend `CounterpartyContact`. */
export interface CounterpartyInput {
  name: string;
  email?: string;
  phone?: string;
  website?: string;
  agent_endpoint?: string;
  channels?: CounterpartyChannel[];
}

/** Validated payload for `POST /runs`. The Intent agent extracts constraints from `goal`. */
export interface CreateRunInput {
  domain: Domain;
  goal: string;
  max_spend: number;
  currency: Currency;
  deliver_by?: string;
  counterparty?: CounterpartyInput;
}

export interface CreateRunResponse {
  run_id: string;
}
