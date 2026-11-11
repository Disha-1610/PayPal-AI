"use client";

import { useState, type FormEvent, type ReactNode } from "react";
import { AlertTriangle, Loader2, Send } from "lucide-react";
import { Card, CardBody, CardHeader } from "@/components/Card";
import { useCreateRun } from "@/hooks/useCreateRun";
import { cn } from "@/lib/utils";
import {
  CURRENCIES,
  DOMAIN_COPY,
  DOMAINS,
  type Currency,
  type Domain,
  type IntakeErrors,
  type IntakeValues,
} from "./types";
import { GOAL_MAX, validateIntake } from "./validate";

const INITIAL: IntakeValues = { domain: "shopping", goal: "", max_spend: "", currency: "USD", deliver_by: "" };

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

export function IntakeForm() {
  const [values, setValues] = useState<IntakeValues>(INITIAL);
  const [errors, setErrors] = useState<IntakeErrors>({});
  const { submit, submitting, error } = useCreateRun();
  const copy = DOMAIN_COPY[values.domain];

  function set<K extends keyof IntakeValues>(key: K, value: IntakeValues[K]) {
    setValues((prev) => ({ ...prev, [key]: value }));
    setErrors((prev) => ({ ...prev, [key]: undefined }));
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
        <h1 className="text-lg font-semibold text-slate-900">New task</h1>
        <p className="mt-0.5 text-sm text-slate-500">
          Describe what you want. The agent searches, compares, negotiates where it can, and asks
          for your approval before any payment.
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
                : "Contractors and agents are in-app sandbox counterparties; payment runs on PayPal sandbox."
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
            label={copy.goalLabel}
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
            Start task
          </button>
        </form>
      </CardBody>
    </Card>
  );
}
