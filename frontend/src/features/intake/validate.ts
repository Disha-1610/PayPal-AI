import {
  DOMAIN_COPY,
  type CounterpartyChannel,
  type CounterpartyInput,
  type CreateRunInput,
  type IntakeErrors,
  type IntakeValues,
} from "./types";

export const GOAL_MIN = 10;
export const GOAL_MAX = 1000;

const EMAIL = /^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,24}$/;
const PHONE = /^\+[1-9]\d{7,14}$/;

/** Strip spaces, dots, dashes and brackets: "+1 (415) 555-0123" -> "+14155550123". */
export function normalizePhone(raw: string): string {
  return raw.replace(/[\s().-]/g, "");
}

function isHttpsUrl(raw: string): boolean {
  try {
    const url = new URL(raw);
    return url.protocol === "https:" && url.hostname.includes(".") && !url.username && !url.password;
  } catch {
    return false;
  }
}

/** The counterparty for a contractor / agent-to-agent run; problems are added to `errors`. */
function validateCounterparty(values: IntakeValues, errors: IntakeErrors): CounterpartyInput {
  const name = values.cp_name.trim();
  const email = values.cp_email.trim();
  const phone = normalizePhone(values.cp_phone.trim());
  const website = values.cp_website.trim();
  const endpoint = values.cp_endpoint.trim();

  if (!name) errors.cp_name = "Name who the agent should negotiate with.";
  if (email && !EMAIL.test(email)) errors.cp_email = "Enter a valid email address.";
  if (phone && !PHONE.test(phone)) errors.cp_phone = "Use international format, e.g. +14155550123.";
  if (website && !isHttpsUrl(website)) errors.cp_website = "Use a full https:// address.";
  if (values.domain === "a2a") {
    if (!endpoint) errors.cp_endpoint = "The agent's https endpoint is required so I can reach it.";
    else if (!isHttpsUrl(endpoint)) errors.cp_endpoint = "Use the agent's full https:// address.";
  }
  if (values.domain === "contractor" && !email && !phone) {
    errors.cp_contact = "Give an email or a phone number so I can contact the contractor.";
  }

  const useEmail = values.domain === "contractor" && email;
  const usePhone = values.domain === "contractor" && phone;
  const channels: CounterpartyChannel[] = [];
  if (useEmail) channels.push("email");
  if (usePhone) channels.push(values.cp_whatsapp ? "whatsapp" : "sms");
  return {
    name,
    ...(useEmail ? { email } : {}),
    ...(usePhone ? { phone } : {}),
    ...(website ? { website } : {}),
    ...(values.domain === "a2a" && endpoint ? { agent_endpoint: endpoint } : {}),
    ...(channels.length > 0 ? { channels } : {}),
  };
}

export type ValidationResult =
  | { ok: true; input: CreateRunInput }
  | { ok: false; errors: IntakeErrors };

/** `today` is injectable (yyyy-mm-dd, UTC) so the check is deterministic. */
export function validateIntake(
  values: IntakeValues,
  today: string = new Date().toISOString().slice(0, 10),
): ValidationResult {
  const errors: IntakeErrors = {};

  const goal = values.goal.trim();
  if (goal.length < GOAL_MIN) errors.goal = `Describe the task (at least ${GOAL_MIN} characters).`;
  else if (goal.length > GOAL_MAX) errors.goal = `Keep it under ${GOAL_MAX} characters.`;

  const maxSpend = Number(values.max_spend);
  if (values.max_spend.trim() === "" || !Number.isFinite(maxSpend) || maxSpend <= 0) {
    errors.max_spend = "Enter a budget greater than zero.";
  }

  if (values.deliver_by && values.deliver_by < today) {
    errors.deliver_by = "Delivery date cannot be in the past.";
  } else if (!values.deliver_by && DOMAIN_COPY[values.domain].deadlineRequired) {
    errors.deliver_by = "Set the date the work must be completed by.";
  }

  const counterparty =
    values.domain === "shopping" || values.cp_mode === "sandbox" ? undefined : validateCounterparty(values, errors);

  if (Object.keys(errors).length > 0) return { ok: false, errors };

  return {
    ok: true,
    input: {
      domain: values.domain,
      goal,
      max_spend: Math.round(maxSpend * 100) / 100,
      currency: values.currency,
      ...(values.deliver_by ? { deliver_by: values.deliver_by } : {}),
      ...(counterparty ? { counterparty } : {}),
    },
  };
}
