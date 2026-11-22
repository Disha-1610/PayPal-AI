# Agents

How each agent in Aisling works. Code lives in `backend/app/orchestrator/agents/`, prompts in `backend/app/orchestrator/prompts/`.

Rules shared by every agent:
- An agent never calls another agent. It returns a result and a partial state update; the **Router** (`orchestrator/router/`) picks the next step.
- The LLM only *reports* or *proposes* into a strict schema. Every decision that changes behaviour or moves money is made by deterministic code.
- Untrusted text (listings, reviews, counter-offers, deliverables) never reaches the Coordinator prompt and can't change a constraint, an approval or a route.
- If the model fails or returns something invalid, each agent falls back to a safe, defined outcome. Model errors never crash a run.

---

## 1. Intent agent
`agents/intent/agent.py` · prompts `intent_extract.md`, `intent_patch.md`

Turns the user's words into a goal and a set of constraints.

- **First message:** full extraction. The model returns the goal and constraints (field, operator, value, **hard** or **soft**), each with a quote from the user's text as evidence.
- **Later messages:** patch mode. The model returns only the operations (add / change / remove) implied by the update, and code applies them through `Constraint.revise`. Only the touched constraints change, and revision history is kept.
- **Deterministic checks:**
  - **Grounding:** a constraint must be backed by the user's own words.
  - **Conflicts:** contradictory hard constraints on the same field (e.g. "under 3000 and over 5000") are detected.
  - **Status:** `ready`, `blocked` (a required field is missing, so it asks the domain plug-in's clarifying question) or `out_of_scope`.
- If the output can't be made valid after one corrective retry, it raises `IntentError` and the user is asked to rephrase.

## 2. Coordinator agent
`agents/coordinator/agent.py`, `agents/coordinator/plan.py` · prompt `coordinator.md`

Owns the plan and decides what to do when the normal path breaks.

- The plan lives in `TaskState.plan`, built from the domain plug-in's template. Replanning means editing that plan, and every edit is audited. There is no separate planner.
- Called only by the Router, on two triggers: **no viable candidate** after evaluation, or a **deadline at risk** reported by the Monitor.
- It sees only structured state (ids, field names, counts), never candidate or user free text.
- The model picks one action from a closed set:
  - `widen`: relax a soft preference or widen the search
  - `switch_candidate`: move to the next-ranked option
  - `replan`
  - `ask_user`
  - `escalate`
- Code checks the choice is allowed for the trigger and that its ids are real. Code then builds the plan edits, re-queueing every step from the restart point onward.
- **Safeguards:**
  - An iteration cap and a `progress_hash` no-progress check force `stop`. `stop` is set by code only.
  - Invalid model output falls back to `ask_user`.

## 3. Discovery agent
`agents/discovery/` · prompt `query_rewrite.md`

Finds candidates on the platforms the user allowed, one resumable step per graph node.

1. **Build the request** (`request.py`). The query is the goal plus soft text preferences. Filters come only from hard `==` constraints, so they never change when the query is rewritten.
2. **Search** (`agent.py`). Each platform is searched one at a time, with authorization narrowed to that platform. Each round runs a resilient search with retries, de-duplicates the results, and enriches the top N.
3. **Enrich** (`enrich.py`). Details, reviews, stock and delivery come through the `ConnectorRegistry`. Review text is stored by the registry, and only a text-free summary returns. A failed call leaves the candidate as it was and never fails the run.
4. **Thin check** (`thin.py`). Results are "thin" if there are fewer than `min_candidates` complete candidates.
5. **Query ladder** (`rewrite.py`). If results are thin, discovery tries the next query in a fixed order:
   1. LLM synonym rewrites
   2. drop a soft term (plain code)
   3. LLM category widening

   Every rewrite passes `guard/query_guard.py`. Validated rewrites are cached, so a resumed run never asks the model again. A model failure skips that step and is recorded in the audit log.
6. **Next platform**, then finish with a recorded stop reason.

The LLM only proposes query text. Routing between these steps is `router/discovery.py`.

## 4. Evaluator agent
`agents/evaluator/`, `agents/evaluation/` · prompt `evaluator_score.md`

Decides which candidates are usable. The deterministic pipeline decides; the LLM can only add reasoning.

1. **Hard filter** (`hard_filter.py`). Every active hard constraint is checked. A failure, or data that can't be verified, excludes the candidate. Unverifiable data is reported separately so the user can be offered a choice.
2. **Credibility** (`credibility.py`). Structured signals (review counts and dates, quality flags, price vs. the rest of the pool) multiply a trust score. A candidate is blocked only below a hard floor.
3. **LLM scoring** (`llm_scoring.py`, optional). The model reads review and listing text into a fixed `LLMScores` schema. It has no tools and no state. If it fails, ranking uses code only.
4. **Blend & rank** (`blend.py`, `ranking.py`):
   - Weighted score from trust plus one criterion per soft constraint, with partial credit for numeric targets that are narrowly missed.
   - The LLM score can only **lower** a candidate, never promote it.
   - Ties break on trust, then cost, then id.
5. **Outcome** (`outcome.py`). `VIABLE`, `NONE_VIABLE` or `RETRY_SEARCH`, with a retry cap. A near-tie is left for the user to pick.
6. **Rejections & diagnosis** (`diagnosis.py`). Each rejected candidate gets one reason built from field names, operators and targets (no free text). Diagnosis keeps each candidate's actual value so Adapt can compute relaxations.

## 5. Adapt (recovery) agent
`agents/adapt/`

Handles "nothing fits" without ever relaxing a constraint silently.

- **Recovery ladder** (`recovery.py`). Pure decision logic with no connector calls:
  1. local recovery, inside the user's source lock
  2. cross-domain expansion, only if nothing is locked
  3. refresh a stale pool
  4. **ask the user**
  5. stop
- **Relaxation proposals** (`relaxation.py`). These are computed from the cached candidate pool. For each hard constraint it finds the *smallest* edit that admits candidates failing only that constraint. The edit depends on the operator:
  - **Ranges:** move the bound to the candidate's actual value.
  - **`==` / `in`:** allow the candidate's value.
  - **`not_in`:** drop the excluded item that blocks the candidate.
  - **`!=` / boolean:** drop the requirement.

  Up to three proposals are offered: most options unlocked first, then the smallest change.
- **Ask user** (`node.py`). Pauses the graph with `interrupt()`. The reply is either a structured choice or free text, which goes to the Intent agent as a patch. This is the **only** place hard constraints change, and every change is recorded as made by the user.
- **Stops:** a repeated progress hash marks the current step as exhausted. If the user has already been asked and nothing changed, the run stops with `no_progress`. Other stop reasons are `exhausted`, `prompt_cap`, `attempt_cap` and `user_declined`.

## 6. Negotiator agent
`agents/negotiator/` · prompt `negotiator.md` (contractor and agent-to-agent runs)

Responds to a counter-offer or a failed delivery, only inside the user's approved envelope.

- **Sanitize** (`offer.py`). Only known fields of a counter-offer survive. Terms must be machine tokens (e.g. `warranty:12m`), so prose such as "ignore limits and accept" is dropped before the model sees it.
- **Envelope.** Built from the signed approval terms: budget ceiling, deadline, currency and non-negotiable terms.
- **Decide.** The model returns `accept`, `counter` or `stop` plus numbers. Every proposal goes through `guard/negotiation_guard.py`:
  - **Outside the envelope** → `BLOCKED`. The model gets one retry with the reason, then the fail-safe is `stop`.
  - **Inside the envelope but terms changed** → the terms hash differs, so the user must approve again.
  - **Identical terms** → the existing approval still holds.
- **Round cap.** In the last round the agent may only accept or stop.

## 7. Verifier agent
`agents/verifier/` · prompts `verifier_generate.md`, `verifier_critic.md`

Checks the outcome before money is captured.

- **Deterministic verification** (`verify.py`). Checks the delivered evidence (price, item, seller, delivery, order status) against the approved terms. Money is captured only on a `PASS` computed here; no LLM is involved.
- **Generator–critic check** (`contractor.py`, contractor and agent-to-agent):
  1. The generator maps deliverable evidence to each agreed acceptance criterion, citing evidence ids.
  2. A critic independently re-checks each criterion and challenges unsupported claims.
  3. On disagreement, they revise for up to `max_passes` rounds.

  **Verdict:** code reduces the agreed per-criterion statuses. All met → `PASS`. Any not met → `FAIL`. Otherwise → `INSUFFICIENT_EVIDENCE`, which escalates to a human. A model failure also gives `INSUFFICIENT_EVIDENCE`.

  The only output that reaches settlement is a boolean (`Evidence.confirmed`), and `verify_evidence` still checks the terms. Injected "approve this" text can't pass a criterion, because a `met` must cite real evidence and survive the critic.

## 8. Executor (money path)
`orchestrator/executor/` · no LLM

Runs the payment steps for every domain, split across graph nodes by two user pauses:

1. **`recheck`.** Reads the live terms again (price, stock, seller, delivery). If they changed, `ExecutionBlocked` routes the run to re-approval, to the Evaluator or to Adapt.
2. **The user approves** the exact terms. The approval is HMAC-signed over a terms hash by `guard/approval_guard.py`. The model never sees the key.
3. **`place_order`.** Creates the PayPal order (idempotent `PayPal-Request-Id`). The approval is consumed once, enforced by a database write.
4. **The payer approves** at PayPal.
5. **`settle`.** The Verifier runs, then the payment is **captured** on PASS or **voided** on FAIL. Contractor runs repeat this per milestone.

## 9. Monitor
`monitor/`, `workers/monitor.py` · no LLM

Watches runs after payment.

- **Event intake.** Events come from signed PayPal webhooks, counterparty updates and a deadline sweeper. The event processor validates, de-duplicates, orders and interprets each event using the domain's `MonitorSpec`. It records events but never changes constraints or moves money.
- **Routing.** `router/monitor.py` maps each result to an action, which `workers/monitor.py` executes:
  - **`ADAPT` / `ASK_USER`:** hand the run to Adapt.
  - **`REPLAN_POST_PURCHASE`:** first release the money (void the hold or refund the capture), then replan. Buying again while funds are still committed is never allowed.
  - **`REMEDY`:** request a refund through `monitor/remedy.py`. Refunds are permission-checked and need a signed approval where required.
  - **`CLOSE`:** mark the run completed.
