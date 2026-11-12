"use client";

import { useState, type ChangeEvent, type FormEvent, type ReactNode } from "react";
import { useSearchParams } from "next/navigation";
import { AlertTriangle, Loader2, Send } from "lucide-react";
import { Card, CardBody, CardHeader } from "@/components/Card";
import { useCreateRun } from "@/hooks/useCreateRun";
import { cn } from "@/lib/utils";
import {
  CURRENCIES,
  DOMAIN_COPY,
  DOMAINS,
  SANDBOX_COUNTERPARTIES,
  type Currency,
  type CounterpartyMode,
  type Domain,
  type IntakeErrors,
  type IntakeValues,
} from "./types";
import { GOAL_MAX, validateIntake } from "./validate";

const INITIAL: IntakeValues = {
  domain: "shopping",
  goal: "",
  max_spend: "",
  currency: "USD",
  deliver_by: "",
  cp_mode: "sandbox",
  cp_name: "",
  cp_email: "",
  cp_phone: "",
  cp_whatsapp: false,
  cp_website: "",
  cp_endpoint: "",
};

const INPUT =
  "w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 focus:border-paypal-lightblue focus:outline-none focus:ring-1 focus:ring-paypal-lightblue";

function Field({
  id,
  label,
  hint,
  error,
  children,
}: {
  id: string;
  label: string;
  hint?: string;
  error?: string;
  children: ReactNode;
}) {
  return (
    <div className="space-y-1">
      <label htmlFor={id} className="block text-sm font-medium text-slate-900">
        {label}
      </label>
      {children}
      {error ? (
        <p id={`${id}-error`} role="alert" className="text-xs text-red-700">
          {error}
        </p>
      ) : hint ? (
        <p className="text-xs text-slate-500">{hint}</p>
      ) : null}
    </div>
  );
}

function initialValues(params: URLSearchParams): IntakeValues {
  const domain = params.get("domain");
  return {
    ...INITIAL,
    domain: DOMAINS.find((d) => d === domain) ?? INITIAL.domain,
    goal: (params.get("goal") ?? "").slice(0, GOAL_MAX),
  };
}

export function IntakeForm() {
  const searchParams = useSearchParams();
  const [values, setValues] = useState<IntakeValues>(() => initialValues(searchParams));
  const [errors, setErrors] = useState<IntakeErrors>({});
  const { submit, submitting, error } = useCreateRun();
  const copy = DOMAIN_COPY[values.domain];

  function set<K extends keyof IntakeValues>(key: K, value: IntakeValues[K]) {
    setValues((prev) => ({ ...prev, [key]: value }));
    setErrors((prev) => ({
      ...prev,
      [key]: undefined,
      ...(key === "cp_email" || key === "cp_phone" ? { cp_contact: undefined } : {}),
    }));
  }

  function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const result = validateIntake(values);
    if (!result.ok) {
      setErrors(result.errors);
      return;
    }
    void submit(result.input);
  }

  return (
    <Card>
      <CardHeader>
        <h1 className="text-lg font-semibold text-slate-900">What are you looking for?</h1>
        <p className="mt-0.5 text-sm text-slate-500">
          Describe it and set your budget. We&apos;ll find and compare offers, and ask for your
          approval before any payment.
        </p>
      </CardHeader>
      <CardBody>
        <form onSubmit={onSubmit} noValidate className="space-y-5">
          <Field
            id="domain"
            label="Task type"
            hint={
              values.domain === "shopping"
                ? undefined
                : "Payment is processed securely by PayPal after you approve."
            }
          >
            <select
              id="domain"
              value={values.domain}
              onChange={(e) => set("domain", e.target.value as Domain)}
              className={INPUT}
            >
              {DOMAINS.map((d) => (
                <option key={d} value={d}>
                  {DOMAIN_COPY[d].label}
                </option>
              ))}
            </select>
          </Field>

          <Field
            id="goal"
            label={`${copy.goalLabel} (mention in details)`}
            hint={`${values.goal.length}/${GOAL_MAX}`}
            error={errors.goal}
          >
            <textarea
              id="goal"
              rows={4}
              maxLength={GOAL_MAX}
              value={values.goal}
              onChange={(e) => set("goal", e.target.value)}
              aria-invalid={Boolean(errors.goal)}
              aria-describedby={errors.goal ? "goal-error" : undefined}
              placeholder={copy.placeholder}
              className={INPUT}
            />
          </Field>

          <div className="grid gap-5 sm:grid-cols-3">
            <div className="sm:col-span-2">
              <Field
                id="max_spend"
                label="Maximum spend"
                hint="The agent can never exceed this."
                error={errors.max_spend}
              >
                <input
                  id="max_spend"
                  type="number"
                  inputMode="decimal"
                  min="0"
                  step="0.01"
                  value={values.max_spend}
                  onChange={(e) => set("max_spend", e.target.value)}
                  aria-invalid={Boolean(errors.max_spend)}
                  aria-describedby={errors.max_spend ? "max_spend-error" : undefined}
                  className={INPUT}
                />
              </Field>
            </div>
            <Field id="currency" label="Currency">
              <select
                id="currency"
                value={values.currency}
                onChange={(e) => set("currency", e.target.value as Currency)}
                className={INPUT}
              >
                {CURRENCIES.map((c) => (
                  <option key={c} value={c}>
                    {c}
                  </option>
                ))}
              </select>
            </Field>
          </div>

          <Field
            id="deliver_by"
            label={copy.deadlineLabel}
            error={errors.deliver_by}
          >
            <input
              id="deliver_by"
              type="date"
              value={values.deliver_by}
              onChange={(e) => set("deliver_by", e.target.value)}
              aria-invalid={Boolean(errors.deliver_by)}
              aria-describedby={errors.deliver_by ? "deliver_by-error" : undefined}
              className={cn(INPUT, "sm:w-56")}
            />
          </Field>

          {values.domain !== "shopping" ? (
            <CounterpartyFields values={values} errors={errors} set={set} />
          ) : null}

          {error ? (
            <p role="alert" className="flex items-start gap-2 text-sm text-red-700">
              <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
              {error}
            </p>
          ) : null}

          <button
            type="submit"
            disabled={submitting}
            className="inline-flex items-center gap-2 rounded-lg bg-paypal-blue px-4 py-2 text-sm font-medium text-white hover:bg-paypal-darkblue disabled:cursor-not-allowed disabled:opacity-50"
          >
            {submitting ? (
              <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
            ) : (
              <Send className="h-4 w-4" aria-hidden />
            )}
            Find offers
          </button>
        </form>
      </CardBody>
    </Card>
  );
}

type SetField = <K extends keyof IntakeValues>(key: K, value: IntakeValues[K]) => void;
type TextKey = "cp_name" | "cp_email" | "cp_phone" | "cp_website" | "cp_endpoint";

const MODE_COPY: Record<CounterpartyMode, string> = {
  sandbox: "Built-in demo businesses (sandbox)",
  named: "Someone I name",
};

/** Contractor / agent-to-agent: built-in sandbox businesses, or only the counterparty the user names here. */
function CounterpartyFields({ values, errors, set }: { values: IntakeValues; errors: IntakeErrors; set: SetField }) {
  const agent = values.domain === "a2a";
  const names = SANDBOX_COUNTERPARTIES[agent ? "a2a" : "contractor"];

  function pickSandbox(name: string) {
    if (values.goal.toLowerCase().includes(name.toLowerCase())) return;
    const goal = values.goal.trim();
    set("goal", `${goal}${goal && !/[.!?]$/.test(goal) ? "." : ""}${goal ? " " : ""}Use ${name}.`.slice(0, GOAL_MAX));
  }

  return (
    <fieldset className="space-y-4 rounded-lg border border-slate-200 p-4">
      <legend className="px-1 text-sm font-semibold text-slate-900">
        {agent ? "Agent to negotiate with" : "Contractor to contact"}
      </legend>
      <div role="radiogroup" aria-label="Counterparty source" className="flex flex-wrap gap-4">
        {(Object.keys(MODE_COPY) as CounterpartyMode[]).map((mode) => (
          <label key={mode} className="flex items-center gap-2 text-sm text-slate-700">
            <input
              type="radio"
              name="cp_mode"
              value={mode}
              checked={values.cp_mode === mode}
              onChange={() => set("cp_mode", mode)}
              className="h-4 w-4 border-slate-300"
            />
            {MODE_COPY[mode]}
          </label>
        ))}
      </div>
      {values.cp_mode === "sandbox" ? (
        <div className="space-y-2">
          <p className="text-xs text-slate-500">
            I search the server&apos;s built-in demo {agent ? "agents" : "contractors"} and pay through PayPal
            Sandbox. Pick one to add it to the description, or leave it to me to compare them.
          </p>
          <div className="flex flex-wrap gap-2">
            {names.map((name) => (
              <button
                key={name}
                type="button"
                onClick={() => pickSandbox(name)}
                className="rounded-full border border-slate-300 px-3 py-1 text-xs text-slate-700 hover:border-paypal-lightblue hover:text-paypal-blue"
              >
                {name}
              </button>
            ))}
          </div>
        </div>
      ) : (
        <NamedCounterpartyFields values={values} errors={errors} set={set} agent={agent} />
      )}
    </fieldset>
  );
}

function NamedCounterpartyFields({
  values,
  errors,
  set,
  agent,
}: {
  values: IntakeValues;
  errors: IntakeErrors;
  set: SetField;
  agent: boolean;
}) {
  const text = (key: TextKey) => ({
    id: key,
    value: values[key],
    onChange: (e: ChangeEvent<HTMLInputElement>) => set(key, e.target.value),
    "aria-invalid": Boolean(errors[key]),
    "aria-describedby": errors[key] ? `${key}-error` : undefined,
    className: INPUT,
  });

  return (
    <>
      <p className="text-xs text-slate-500">
        {agent
          ? "I send the request to this agent's endpoint and wait for its quote. Your budget is never shared."
          : "I ask this contractor for a quote and wait for their reply, which can take hours or days. If they don't answer, I'll ask you what to do. Your budget is never shared."}
      </p>
      <Field id="cp_name" label={agent ? "Agent name" : "Contractor name"} error={errors.cp_name}>
        <input {...text("cp_name")} maxLength={120} autoComplete="off" />
      </Field>
      {agent ? (
        <Field id="cp_endpoint" label="Agent endpoint (https)" error={errors.cp_endpoint}>
          <input {...text("cp_endpoint")} type="url" inputMode="url" placeholder="https://agent.example.com/rpc" />
        </Field>
      ) : (
        <>
          <div className="grid gap-4 sm:grid-cols-2">
            <Field id="cp_email" label="Email" error={errors.cp_email}>
              <input {...text("cp_email")} type="email" inputMode="email" autoComplete="off" />
            </Field>
            <Field id="cp_phone" label="Phone" hint="International format, e.g. +14155550123" error={errors.cp_phone}>
              <input {...text("cp_phone")} type="tel" inputMode="tel" autoComplete="off" />
            </Field>
          </div>
          <label className="flex items-center gap-2 text-sm text-slate-700">
            <input
              type="checkbox"
              checked={values.cp_whatsapp}
              onChange={(e) => set("cp_whatsapp", e.target.checked)}
              className="h-4 w-4 rounded border-slate-300"
            />
            Message the phone on WhatsApp instead of SMS
          </label>
          {errors.cp_contact ? (
            <p id="cp_contact-error" role="alert" className="text-xs text-red-700">
              {errors.cp_contact}
            </p>
          ) : null}
        </>
      )}
      <Field id="cp_website" label="Website or portfolio (optional)" error={errors.cp_website}>
        <input {...text("cp_website")} type="url" inputMode="url" placeholder="https://" />
      </Field>
    </>
  );
}
