# Project Architecture

## System overview

```mermaid
flowchart LR
    subgraph Client["Browser"]
        UI["Next.js 15 / React 19 / Tailwind<br/>dashboard, runs, intake, run detail"]
    end

    subgraph Backend["FastAPI backend - single process"]
        API["API routes<br/>auth, runs, approvals,<br/>webhooks, counterparty"]
        AUTH["Auth<br/>signed session cookie"]
        RS["RunService / RunController"]
        ORC["LangGraph orchestrator<br/>router, guards, agents, executor"]
        CON["ConnectorRegistry"]
        PAY["PaymentService<br/>PayPal client, ledger"]
        CPD["Counterparty desk<br/>outreach + reply tokens"]
        LLMG["LLM gateway<br/>primary / light / fallback"]
        AUD["Audit writer"]
        subgraph Workers["Background workers - asyncio tasks"]
            SWP["Deadline sweeper"]
            REC["Payment reconciler"]
            MONW["Monitor worker"]
        end
    end

    subgraph Data["Persistence"]
        DB[("SQL database<br/>SQLite or Postgres")]
        CK[("LangGraph checkpoints")]
    end

    subgraph External["External services"]
        PP["PayPal Sandbox<br/>OAuth2, Orders, Webhooks"]
        GEM["Gemini / Groq / OpenAI-compatible"]
        C3["Channel3 product API"]
        SMTP["SMTP email"]
        TW["Twilio SMS / WhatsApp"]
        AG["External agents - HTTPS"]
    end

    UI -- "REST + SSE /runs/{id}/events" --> API
    UI -- "redirect to approve" --> PP
    API --> AUTH
    API --> RS --> ORC
    ORC --> CON --> C3
    CON --> SBX["In-app sandbox<br/>contractors and agents"]
    ORC --> PAY --> PP
    ORC --> LLMG --> GEM
    ORC --> CPD
    CPD --> SMTP & TW & AG
    ORC --> AUD --> DB
    RS --> DB
    PAY --> DB
    ORC --> CK

    PP -- "signed webhooks /webhooks/paypal" --> API
    TW -- "/webhooks/twilio" --> API
    SMTP -. "inbound email /webhooks/email-inbound" .-> API
    AG -- "/a2a/callback/{token}" --> API
    API --> MONW --> ORC
    SWP --> MONW
    REC --> PP
    REC --> DB
```

## Request lifecycle

```mermaid
sequenceDiagram
    participant U as User (UI)
    participant A as FastAPI
    participant G as LangGraph run
    participant P as PayPal Sandbox
    participant W as Workers

    U->>A: POST /runs (goal, max_spend, deadline, domain)
    A->>G: start run in background
    A-->>U: 201 run_id
    U->>A: GET /runs/{id}/events (SSE)
    G-->>U: progress events: intent, discovery, evaluation
    G-->>U: interrupt: approval request (exact terms)
    U->>A: POST /runs/{id}/approval
    A->>G: resume with HMAC-signed approval
    G->>P: create order (PayPal-Request-Id)
    G-->>U: approval_url
    U->>P: payer approves
    P-->>U: redirect to /runs/{id}/payment-return
    U->>A: POST /runs/{id}/payer-return
    A->>G: resume, then settle
    G->>P: capture on Verifier PASS, or void on FAIL
    P-->>A: webhook (signature verified)
    A->>W: monitor event
    W->>G: adapt / remedy / close
    W->>P: reconcile stuck payments
```

## Repository layout

```
PayPal-AI/
├─ frontend/                 Next.js app
│  └─ src/
│     ├─ app/                routes: (marketing) landing, (app) dashboard, runs, runs/new, runs/[runId]
│     ├─ features/           approvals, candidates, chat, intake, milestones, payment, plan, runs
│     ├─ hooks/              data hooks (useRun, useChat, useApproval, ...)
│     └─ lib/                api client, SSE client/parser, PayPal approval redirect
├─ backend/
│  ├─ app/
│  │  ├─ api/                FastAPI app, routes, bootstrap (composition root)
│  │  ├─ auth/               sandbox sessions, external JWT auth
│  │  ├─ runs/               RunService, run views
│  │  ├─ orchestrator/       graph, router, guards, agents, executor, prompts
│  │  ├─ domains/            shopping, contractor, A2A plug-ins
│  │  ├─ connectors/         Channel3, sandbox, test mocks, registry
│  │  ├─ counterparty/       outreach desk, transports, reply tokens, SSRF guard
│  │  ├─ payments/           PayPal client, payment service, reconciliation
│  │  ├─ monitor/            event processor, timers, webhook verification, remedy
│  │  ├─ workers/            monitor, reconciler, sweeper
│  │  ├─ llm/                provider gateway and deadlines
│  │  ├─ audit/              audit events, signed approvals
│  │  ├─ db/                 models, Alembic migrations, session
│  │  ├─ sandbox/            in-app demo counterparties
│  │  └─ schemas/            Pydantic contracts
│  └─ tests/                 unit, integration, adversarial, eval suites
├─ infra/docker-compose.yml  Postgres 16
├─ docs/TEST_SCENARIOS.md
└─ start.bat / stop.bat
```

## Data model

```mermaid
erDiagram
    runs ||--o{ run_messages : "chat"
    runs ||--o| run_counterparties : "named counterparty"
    runs ||--o{ payments : "orders"
    runs ||--o{ milestones : "contractor plan"
    runs ||--o{ monitor_events : "post-purchase events"
    runs ||--o{ audit_logs : "decisions"
    payments ||--o{ payment_events : "state changes"
    payments ||--o{ payment_reconciliations : "sync with PayPal"
    connector_texts }o--|| runs : "untrusted listing/review text"
```

## Key architecture points

- **Composition root.** `backend/app/api/bootstrap.py` builds the connectors, LLM providers, PayPal client, graphs (one per domain) and workers, and installs them on `app.state`. Startup fails fast and names any missing required setting.
- **Single deployable backend.** The API, the LangGraph runtime and the background workers (deadline sweeper, payment reconciler, monitor) run in one FastAPI process as asyncio tasks, and stop gracefully on shutdown.
- **Durable runs.** LangGraph checkpoints (SQLite or Postgres) let a run pause for days (approval, payer, counterparty reply) and resume after a restart. Business data lives in SQL tables managed by Alembic migrations.
- **Live updates over SSE.** The UI streams `GET /runs/{id}/events`, and user actions (messages, approvals, cancel, confirm delivery) are plain REST calls that resume the graph.
- **Layering.** Routes stay thin and call `RunService`. Agents never touch HTTP or the database directly. Connectors, payments, counterparty transports and LLM providers sit behind small protocols, so tests swap in fakes.
- **Domain plug-ins.** Shopping, contractor and A2A share one engine. Each plug-in supplies a plan template, required fields, verification spec, monitor spec and payment terms.
- **Payment safety:**
  - Authorize → verify → capture or void.
  - Approvals are HMAC-signed over the exact terms and consumed once.
  - Every PayPal mutation is idempotent (`PayPal-Request-Id`).
  - Webhook signatures are verified against `PAYPAL_WEBHOOK_ID`.
  - A reconciler keeps the local ledger in line with PayPal.
- **Security boundaries:**
  - Every query is scoped to the authenticated principal.
  - CORS allows exact origins only.
  - Secrets come from environment variables.
  - Untrusted text (listings, reviews) is stored separately in `connector_texts` and never reaches control prompts.
  - Outbound counterparty calls pass an SSRF guard (`counterparty/netguard.py`).
  - Inbound Twilio and email webhooks are signature- or secret-checked.
- **LLM resilience.** One gateway serves several providers: the primary (Gemini by default), an optional light model for cheap calls, and an automatic fallback on overload, error or quota exhaustion. A per-run LLM time budget skips optional LLM steps instead of failing.
- **Auditability.** Every decision, constraint revision, approval and payment event is written to `audit_logs` in the same transaction as the change.
