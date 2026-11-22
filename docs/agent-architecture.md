# Agent Architecture

## Components

```mermaid
flowchart LR
    UI["Next.js UI"] -- "REST + SSE" --> API["FastAPI"]
    API --> RC["RunController<br/>LangGraph runtime"]
    RC --> G["Task graph<br/>one per domain"]

    subgraph Agents["Agents - return results, never call each other"]
        INT["Intent"]
        COO["Coordinator"]
        DIS["Discovery"]
        EVA["Evaluator"]
        ADA["Adapt"]
        NEG["Negotiator"]
        VER["Verifier"]
    end

    subgraph Control["Deterministic control"]
        ROU["Router<br/>orchestrator/router"]
        GUA["Guards<br/>query / constraint / graph /<br/>negotiation / approval"]
        EXE["Executor<br/>recheck / place_order / settle"]
    end

    G --> ROU
    ROU --> Agents
    Agents --> ROU
    ROU --> GUA --> EXE

    DIS --> REG["ConnectorRegistry"]
    REG --> C3["Channel3"]
    REG --> SBX["Sandbox contractors / agents"]
    NEG --> DESK["Counterparty desk<br/>email / SMS / WhatsApp / HTTPS"]
    EXE --> PP["PayPal Sandbox<br/>authorize / capture / void"]
    VER --> EXE

    INT & COO & DIS & EVA & NEG & VER -.-> LLM["LLM gateway<br/>Gemini + fallback"]

    PP -- "signed webhooks" --> MON["Monitor + workers<br/>sweeper / reconciler"]
    MON -- "monitor_intake" --> G
    G --> CK[("Checkpoints<br/>SQLite / Postgres")]
    G --> AUD[("Audit log + payment ledger")]
```

## Task graph

Node names match `backend/app/orchestrator/graph/builder.py`. Rounded nodes pause the graph with `interrupt()` and wait for the user, the payer or a counterparty.

```mermaid
flowchart TD
    START((start)) --> intent

    intent -->|ready| dstart
    intent -->|missing / conflicting| ask_user
    intent -->|out of scope| stop

    subgraph DISCOVERY["Discovery loop"]
        dstart["discovery_start"] --> dsearch["discovery_search"]
        dsearch --> dadapt["discovery_adapt<br/>query ladder"]
        dsearch --> dadv["discovery_advance<br/>next platform"]
        dadapt --> dsearch
        dadv --> dsearch
        dsearch --> dfin["discovery_finish"]
    end

    dstart -->|named counterparty| request_quote
    request_quote --> await_quote([await_quote])
    await_quote -->|quote received| guard_evaluate
    request_quote & await_quote -->|no reply / timeout| followup([counterparty_followup])
    followup --> dstart

    dfin --> guard_evaluate{{guard_evaluate}} --> evaluate
    evaluate -->|viable| select
    evaluate -->|near-tie / only warned options| choose([choose_candidate])
    evaluate -->|retry search| dstart
    evaluate -->|none viable| coordinator

    coordinator -->|switch_candidate| switch["switch_candidate"]
    coordinator -->|widen / replan / ask| adapt
    adapt -->|retry| dstart
    adapt -->|propose relaxations| ask_user([ask_user])
    ask_user -->|constraints changed| dstart

    choose -->|picked| guard_execute
    choose -->|requirements changed| dstart
    select --> guard_execute{{guard_execute}}
    switch --> guard_execute

    guard_execute -->|shopping / A2A| recheck
    guard_execute -->|contractor| propose_plan --> recheck
    recheck -->|terms still hold| request_approval
    recheck -->|terms changed| evaluate
    recheck -->|unavailable| adapt

    request_approval --> await_approval([await_approval<br/>HMAC-signed terms])
    await_approval -->|approved| guard_pay{{guard_pay}}
    await_approval -->|plan approved| guard_plan{{guard_plan}}
    await_approval -->|declined| decline["decline_candidate"] --> choose

    guard_pay --> place_order
    guard_pay -->|milestone| milestone_pay
    guard_plan --> milestone_create --> milestone_next
    milestone_next -->|next milestone| request_approval
    milestone_next -->|all paid| END1((end))

    place_order --> await_payer([await_payer<br/>PayPal approval])
    milestone_pay --> await_payer
    await_payer --> settle["settle<br/>Verifier: capture or void"]

    settle -->|PASS| END2((end))
    settle -->|milestone captured| milestone_next
    settle -->|FAIL: re-discover| dstart
    settle -->|FAIL: renegotiate| guard_negotiate{{guard_negotiate}}
    settle -->|problem| adapt

    guard_negotiate --> negotiate
    negotiate -->|counter| send_counter --> await_cp([await_counterparty])
    await_cp --> guard_negotiate
    negotiate -->|accept: new terms| request_approval

    monitor_intake["monitor_intake<br/>webhooks / timers"] --> adapt
    revise_intake["revise_intake<br/>user message on a stopped run"] --> revise --> dstart

    stop["stop_report"] --> END3((end))
    guard_evaluate & guard_execute & guard_pay & guard_plan & guard_negotiate -.->|guard fails| stop
    coordinator & adapt & negotiate -.->|cap / no progress / declined| stop
```

## Key architecture rules

- **Agents never call each other.** Each node runs one agent or executor step and returns a partial `TaskState` update. Every conditional edge is a function in `orchestrator/router/`.
- **Routing is code.** The LLM only fills strict schemas: extracted intent, query rewrites, scores, a coordinator choice from a closed set, negotiation moves, and verification findings. It never picks a graph edge directly.
- **Guards before every side effect.** `guard_evaluate`, `guard_execute`, `guard_pay`, `guard_plan` and `guard_negotiate` are deterministic checks. Any failure ends at `stop_report` with a reason.
- **Two pauses bracket the money path.** `await_approval` (the user approves the exact terms, which are HMAC-signed) and `await_payer` (the payer approves at PayPal). The approval is valid only for its terms hash, so any change to price, seller, delivery or milestones needs a fresh approval.
- **Verification before capture.** `settle` runs the Verifier. Funds are captured on PASS and voided on FAIL. Contractor runs pay per milestone.
- **Hard constraints change only by the user**, through `ask_user`, `choose_candidate` or `revise`. Agents and untrusted text cannot change them.
- **Untrusted text is isolated.** Listings, reviews, counter-offers and deliverables never reach the Coordinator prompt. Counter-offers are sanitized to machine tokens before the Negotiator sees them.
- **Bounded loops.** Every loop has a cap: iteration cap, discovery ladder, Adapt prompt cap (2), negotiation rounds and verifier passes. A repeated `progress_hash` means no progress and stops the run with an explanation.
- **Durable and resumable.** LangGraph checkpoints (SQLite or Postgres) persist state at every node. Nothing with side effects runs before an `interrupt()`, because LangGraph re-runs that node when it resumes.
- **One engine, three domains.** Shopping, contractor and A2A use the same graph. Domain differences live in plug-ins (`backend/app/domains`): plan template, verification spec, monitor spec and terms. The contractor graph adds the milestone branch, and contractor/A2A runs add the quote and negotiation branches.
- **Post-purchase events re-enter the graph.** The Monitor worker turns webhook and timer events into `monitor_intake` → Adapt. Funds are released (void or refund) before any replan.
