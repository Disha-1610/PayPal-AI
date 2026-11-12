# Aisling — Lifecycle-Driven Autonomous Goal Agent

> An AI system that owns a user's goal from the moment it is requested until it is **successfully completed** or the user is told, clearly, **why it cannot proceed** — paying through **PayPal** only with explicit, cryptographically bound authorization.

Built for the PayPal AI Hackathon (PayPal Sandbox + Google Gemini + LangGraph + Channel3).

---

## The idea

Most autonomous-agent demos automate **one operation in one domain**: buy a product, pay a contractor, or settle between two agents. Real goals are bigger than a single operation. They have a **lifecycle**: understand, search, compare, decide, act, verify, pay, monitor, and deal with whatever changes along the way.

Aisling is a **reusable execution framework**, not a bundle of domain-specific bots. An orchestrator picks the capabilities a goal needs and manages its full lifecycle. Its defining capability is not just doing tasks autonomously. It is **adapting the workflow when circumstances change while preserving the user's original intent, constraints and payment authorization.**

### The lifecycle (not a fixed pipeline)

| # | Stage | What happens |
|---|-------|--------------|
| 1 | **Understand** | Extract objectives, budget, deadline, preferences and restrictions. Hard constraints and soft preferences are kept separate. |
| 2 | **Plan** | Break the goal into tasks and dependencies; choose agents, tools and services. |
| 3 | **Discover** | Query authorized platforms and gather price, reviews, stock and delivery data. |
| 4 | **Evaluate** | Score credibility, apply hard filters, rank, and record rejection reasons. |
| 5 | **Decide** | Pick the best option that satisfies every hard constraint. |
| 6 | **Execute** | Prepare the action against the external platform, service or counterparty. |
| 7 | **Verify** | Independently re-check the outcome against the approved terms. |
| 8 | **Adapt & replan** | On failure or unavailability, diagnose, widen or rewrite the search, switch source, renegotiate. |
| 9 | **Interact** | Ask the user whenever a constraint must change or an action exceeds existing authorization. |
| 10 | **Transact** | Authorize and settle through PayPal. |
| 11 | **Monitor & close** | Track delays and post-execution issues; close only when completion conditions are met. |

Stages can be **revisited at any time** — a verification failure goes back to discovery, a user's changed constraint re-runs evaluation, a missed deadline triggers a replan.

### Example: running shoes

> *"Find good-quality running shoes, size 7, under ₹5,000, delivered before November 15. You can search Amazon and Flipkart."*

The agent extracts size 7 / ₹5,000 / Nov 15 / permitted platforms, discovers and evaluates candidates, filters anything that breaks a hard constraint, verifies price, size, delivery and return terms again before checkout, obtains approval, then pays via PayPal and tracks delivery.

**When nothing fits** (A: ₹4,500 but arrives Nov 18; B: ₹5,500 on time; C: ₹4,800 but no size 7), the agent does **not** stop and does **not** silently relax anything. It:

1. Widens discovery (query rewrites, other sources, wider category).
2. If still nothing, explains **which** constraints cannot be met.
3. Asks the user what to change (e.g. raise the budget to ₹5,500).
4. Updates **only that constraint**, keeps the rest, and re-runs discovery and evaluation.

### Same engine, different domains

| Domain | Lifecycle through the same framework |
|--------|--------------------------------------|
| **Shopping** | discover → evaluate → verify → PayPal authorize → capture → monitor delivery |
| **Contractor management** | find contractor → evaluate → negotiate scope & milestones → verify deliverables → release milestone payments → handle delays / incomplete work |
| **Agent-to-agent commerce** | discover agents → verify identity & capability → negotiate → agree → validate authorization → settle via PayPal → verify delivery → close |

Domain behaviour lives in small adapters (`backend/app/domains`, `backend/app/connectors`); the orchestrator and lifecycle are shared.

---

## How PayPal is used (central to the design)

- **Authorize → verify → capture.** Funds are held with a PayPal *authorization*. They are **captured only after the Verifier passes**; on failure the authorization is **voided**. Contractor runs release payments per **milestone**.
- **Human-in-the-loop approvals bound to exact terms.** An approval is an HMAC-SHA256-signed token over a `terms_hash` (payee, currency, amount ceiling, expiry) plus run, session, nonce and TTL. Altered terms, replays or expired approvals are rejected **before** any payment call. An approval is valid only for the exact item / price / seller / delivery it was granted for.
- **OAuth2 client-credentials** for outbound PayPal REST calls, with token caching and refresh.
- **Idempotent mutations:** every mutating call carries a `PayPal-Request-Id`; status is checked before any retry.
- **Signed webhooks:** inbound events are verified with PayPal's `verify-webhook-signature` against `PAYPAL_WEBHOOK_ID`. Invalid signatures are rejected with 401 before any business logic runs.
- **Reconciliation worker & ledger** keep local payment state consistent with PayPal; users can cancel, and the system voids or refunds accordingly.

## How AI is used

- **Model:** Google **Gemini** (`gemini-3.5-flash` by default, configurable) via `google-genai`, behind a provider gateway (`backend/app/llm`).
- **Orchestration:** **LangGraph** state graph with durable checkpoints (SQLite / Postgres) and `interrupt()` for human-in-the-loop pauses.
- **Agents** (prompts in `backend/app/orchestrator/prompts`):
  - **Intent**: full extraction once, then constraint *diffs* only.
  - **Coordinator**: writes and edits the plan; replans when a deadline is at risk; decides widen-search vs. ask-user.
  - **Discovery**: connector calls, query rewriting, adaptive widening.
  - **Evaluator**: credibility scoring, ranking, diagnosis of why options fail.
  - **Verifier**: field-level match for shopping; generator–critic loop for contractor/A2A deliverables.
  - **Negotiator**: counter-offers within user constraints for contractor/A2A.
- **Routing is code; the LLM decides only ambiguous branches.** Deterministic guards sit on every path into execution and payment.

### Safety properties

- Agents never call each other; all transitions go through the router / coordinator.
- **Constraint changes are source-checked:** a hard constraint can only be changed by the *user*, never by an agent or by untrusted text.
- **Untrusted content** (reviews, listings, counter-offers) never reaches the coordinator prompt.
- Iteration caps and a **no-progress hash** stop runaway loops, ending in a clear report of *why* the system could not proceed.

---

## Architecture

```
Next.js UI ──REST + SSE──▶ FastAPI ──▶ LangGraph orchestrator
                                         ├─ Intent → Guard
                                         ├─ Coordinator (plan / replan)
                                         ├─ Discovery ⇄ Evaluator ⇄ Adapt   (search loop)
                                         ├─ Negotiator (contractor / A2A)
                                         ├─ Executor → Approval guard → PayPal authorize
                                         ├─ Verifier → capture | void → re-discover
                                         └─ Monitor (webhooks + timers) → Coordinator
Connectors: Channel3 (live retail), sandbox contractor marketplace, A2A counterparty transport
Persistence: SQLAlchemy/Alembic (SQLite or Postgres), audit log, payment ledger
Workers: monitor, reconciler, sweeper
```

```
backend/app/
  api/            FastAPI routes: runs, approvals, auth, webhooks
  orchestrator/   graph, router, guards, executor, agents, prompts
  domains/        shopping, contractor, A2A adapters
  connectors/     Channel3 + sandbox counterparties
  payments/       PayPal client, ledger, reconciliation
  monitor/        webhooks, timers, remedy
  workers/        monitor, reconciler, sweeper
  audit/          events and signed approvals
frontend/src/     Next.js 15 / React 19 / Tailwind UI with live event stream
```

## Tools used

| Tool | Role |
|------|------|
| **PayPal Sandbox** (REST Orders/Payments, OAuth2, Webhooks) | Authorization, capture, void/refund, webhook events |
| **Google Gemini** | Intent extraction, planning, scoring, verification, negotiation |
| **LangGraph** | Stateful, resumable agent graph with human interrupts |
| **Channel3** | Live multi-retailer product data for shopping runs |
| FastAPI, SQLAlchemy, Alembic, Next.js, Tailwind | Backend, persistence, UI |

Note: Channel3 is the only live retail source. It aggregates many retailers, so "search other stores" is served through it. Amazon/Flipkart connectors are test mocks. Contractor and A2A runs use in-app sandbox counterparties (`SANDBOX_COUNTERPARTIES_ENABLED`).

---

## Run it

**Prerequisites:** Python 3.11+, Node 18+, a PayPal Sandbox REST app and sandbox business account, a Gemini API key, a Channel3 API key.

```bash
# 1. Backend
cd backend
python -m venv .venv
.venv\Scripts\activate            # Windows  (source .venv/bin/activate on macOS/Linux)
pip install -r requirements.txt
cp .env.example .env              # then fill in the values below
alembic upgrade head
python -m uvicorn app.api.main:app --reload --port 8000

# 2. Frontend (new terminal)
cd frontend
npm install
npm run dev                       # http://localhost:3000
```

On Windows, `start.bat` launches both and opens the browser; `stop.bat` stops them.

**Required `.env` values** (`backend/.env.example` lists all):

| Variable | Purpose |
|----------|---------|
| `DATABASE_URL` | `sqlite:///./dev.sqlite` works for local runs; Postgres supported |
| `LLM_API_KEY` | Gemini API key |
| `CHANNEL3_API_KEY` | Product data |
| `PAYPAL_CLIENT_ID` / `PAYPAL_CLIENT_SECRET` | PayPal Sandbox REST app |
| `PAYPAL_PAYEE_MERCHANT_ID` | Sandbox business account receiving funds |
| `PAYPAL_WEBHOOK_ID` | Webhook registered on the app, pointing to `/webhooks/paypal` (use a tunnel such as ngrok locally) |
| `APPROVAL_SIGNING_KEY`, `SESSION_SIGNING_KEY` | `python -c "import secrets; print(secrets.token_hex(32))"` |
| `AUTH_SANDBOX_ENABLED` | `true` for anonymous local sign-in |
| `SANDBOX_COUNTERPARTIES_ENABLED` | `true` to enable contractor / A2A demos |

### Try it

1. Open `http://localhost:3000` and submit: *"Black running shoes, size 7, under 6000 rupees"*.
2. Watch the live event stream: understand → discover → evaluate → verify → approval request.
3. Approve with a PayPal sandbox buyer account; funds are authorized, verified, then captured.
4. Force the adaptive path: *"Nike Air Zoom Pegasus 41, size 9, under 500 rupees"*. The agent explains the budget cannot be met and proposes a relaxation; reply to change **only** the budget and the run resumes.

More prompts and expected outcomes are in [`docs/TEST_SCENARIOS.md`](docs/TEST_SCENARIOS.md).

### Tests and checks

```bash
cd backend && python -m pytest
cd frontend && npm run type-check && npm run build
```

## License

MIT — see [`LICENSE`](LICENSE).
