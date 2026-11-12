"""Assemble the task graph from the existing nodes. Wiring only: no decision is made here.

Every conditional edge calls a function from ``orchestrator.router``; every node is one existing agent /
executor step. Nodes never call each other. Two user interrupts bracket the money path: the platform approval
(``await_approval``) and the payer's approval at PayPal (``await_payer``).

    intent -> (ask_user | discovery loop | stop_report)
    discovery loop -> guard_evaluate -> evaluate -> (select | discovery | coordinator)
    coordinator -> (discovery | switch_candidate | adapt | stop_report)
    select -> guard_execute -> recheck -> (request_approval | evaluate | adapt) -> await_approval
    await_approval -> decline_candidate -> choose_candidate -> guard_execute   (the user declined the product)
    await_approval -> guard_pay -> place_order -> await_payer -> settle -> (end | discovery | await_payer)

Every Guard failure, an exhausted Coordinator/Adapt, a rejected approval and the global limits (``GraphLimits``:
cycle cap, repeated progress hash) end at ``stop_report``. Verification (the Verifier's PASS/FAIL -> capture/void)
runs inside ``settle`` through the Executor, so there is no separate verifier node.

Monitor events enter at ``monitor_intake`` (see ``workers.monitor``), which hands the task back to Adapt.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit.events import AuditScope, AuditSink, Phase
from app.connectors.registry import ConnectorRegistry
from app.db.base import utcnow
from app.db.session import session_scope
from app.domains.base import DomainPlugin
from app.llm.gateway import LLMGatewayError
from app.llm.providers import LLMProvider
from app.orchestrator.agents.adapt.node import adapt_node, ask_user_node
from app.orchestrator.agents.coordinator.agent import CoordinatorResult, coordinate
from app.orchestrator.agents.discovery.agent import (
    discovery_adapt_node,
    discovery_advance_node,
    discovery_finish_node,
    discovery_search_node,
    discovery_start_node,
)
from app.orchestrator.agents.evaluator.agent import evaluator_node
from app.orchestrator.agents.coordinator.plan import build_plan
from app.orchestrator.agents.intent.agent import IntentError, extract_intent, to_state_update
from app.orchestrator.executor.executor import ExecutionBlocked, Executor
from app.orchestrator.executor.state import ExecutionState
from app.orchestrator.graph.approval import await_approval_node, request_approval_node
from app.orchestrator.graph.milestones import (
    STATUS_COMPLETED,
    STATUS_MILESTONE_READY,
    STATUS_PLAN_CREATED,
    STATUS_PLAN_PROPOSED,
    MilestoneFlow,
    create_plan_step,
    next_milestone_step,
    pay_milestone_step,
    propose_plan_step,
    settle_milestone_step,
)
from app.orchestrator.graph.negotiation import (
    STATUS_NEGOTIATING,
    CounterpartyChannel,
    EnvelopeFor,
    counterparty_reply,
    negotiate_step,
    send_counter_step,
)
from app.orchestrator.graph.stop import stop_report_node
from app.orchestrator.graph.timing import timed
from app.orchestrator.guard.approval_guard import signed_approval_verifier
from app.orchestrator.guard.graph_guard import (
    GUARD_BLOCKED,
    GUARD_PASSED,
    guard_discovery_data,
    guard_execution,
    guard_negotiation,
    guard_payment,
)
from app.orchestrator.router.coordinator import CoordinatorRoute, route_coordinator_result
from app.orchestrator.router.discovery import route_discovery
from app.orchestrator.router.evaluator import EvaluatorRoute, route_evaluation
from app.orchestrator.router.executor import ExecutorRoute, route_execution
from app.orchestrator.progress.hashing import progress_hash
from app.orchestrator.router.intent import IntentRoute, route_intent
from app.orchestrator.router.negotiator import route_negotiation
from app.orchestrator.router.limits import GraphLimits, LimitRoute, route_limits
from app.orchestrator.router.payment import PaymentRoute, route_payment_error
from app.payments.provider import ProviderError
from app.payments.service import PaymentPermit, PaymentServiceError
from app.schemas.adapt import RecoveryStep
from app.schemas.approval_terms import ApprovalTerms
from app.schemas.authorization import Action, PendingApproval
from app.schemas.candidate import Candidate
from app.schemas.coordinator import Action as CoordinatorAction
from app.schemas.coordinator import CoordinatorEvent, Trigger
from app.schemas.discovery import DiscoveryPolicy, DiscoveryRoute
from app.schemas.evaluator import TextSource
from app.schemas.intent import IntentResult, IntentStatus
from app.schemas.recheck import MismatchAction
from app.schemas.negotiator import NegotiatorPolicy, NegotiatorRoute
from app.schemas.state import TaskState


_MAX_CHOICES = 10  # options listed when the user must pick a product


@dataclass(frozen=True)
class GraphDeps:
    """Everything the nodes need from outside the orchestrator. Built once per worker process."""

    plugin: DomainPlugin
    registry: ConnectorRegistry
    text_source: TextSource
    executor: Executor
    session_factory: async_sessionmaker[AsyncSession]
    audit_sink: AuditSink
    # Domain rule: the exact agreement for a candidate (shopping: item + price + seller + delivery date).
    terms_for: Callable[[Candidate, TaskState], ApprovalTerms]
    approval_keys: dict[str, bytes]
    provider: LLMProvider | None = None
    discovery_policy: DiscoveryPolicy = field(default_factory=DiscoveryPolicy)
    now: Callable[[], datetime] = utcnow
    limits: GraphLimits = field(default_factory=GraphLimits)
    # Negotiation (contractor / A2A): the bounds builder, the counterparty transport and the round policy.
    envelope_for: EnvelopeFor | None = None
    counterparty: CounterpartyChannel | None = None
    negotiator_policy: NegotiatorPolicy = field(default_factory=NegotiatorPolicy)
    # Milestone engagements (contractor): plan proposal + per-milestone approval, payment and verification.
    milestones: MilestoneFlow | None = None
    # In-process counterparties (sandbox) report fulfilment here, before settlement opens its transaction.
    # Real counterparties report through webhooks and leave this unset.
    before_settle: Callable[[TaskState], Awaitable[None]] | None = None


def build_graph(deps: GraphDeps, checkpointer: BaseCheckpointSaver | None = None) -> Any:
    """Compile the graph. ``checkpointer`` makes it resumable across worker restarts."""
    d = deps

    def scope(state: TaskState) -> AuditScope:
        return AuditScope(d.audit_sink, state["run_id"])

    def candidate(state: TaskState) -> Candidate:
        wanted = state["selected_candidate_id"]
        return next(c for c in state.get("candidates", []) if c.candidate_id == wanted)

    def permit_for(state: TaskState, terms: ApprovalTerms) -> PaymentPermit:
        history = state.get("approvals", [])
        approved = history[-1]  # all approvals of one user decision share user, session and terms
        # Only the latest decision's approvals: an earlier one (a plan, or a deal later renegotiated) is for other terms.
        approvals = tuple(a for a in history if a.terms_hash == approved.terms_hash and a.session_id == approved.session_id)
        verifier = signed_approval_verifier(
            current_terms=terms, keys=d.approval_keys, run_id=state["run_id"], session_id=approved.session_id or "",
            approved_by=approved.approved_by,
        )
        return PaymentPermit(authorization=state["authorization"], platform=approved.platform, approvals=approvals, verifier=verifier)

    # ---- intent / user loop ------------------------------------------------------------------------

    async def intent(state: TaskState) -> dict[str, object]:
        # The plan template is the Coordinator's replanning surface; it is laid down once, on the first entry.
        plan = {} if state.get("plan") else build_plan(d.plugin, audit=scope(state))
        try:
            result, diff = await extract_intent(state["original_intent"], d.plugin, provider=d.provider, audit=scope(state))
        except (LLMGatewayError, IntentError) as exc:
            # No deterministic fallback can invent the user's goal: fail the run in the checkpoint, not in a log line.
            scope(state).emit(Phase.INTENT, "INTENT_FAILED", node="intent", decision="failed", reason=str(exc))
            return {**plan, "status": "failed", "errors": [f"could not understand the request: {exc}"], "domain": d.plugin.name}
        return {**plan, **to_state_update(result, diff), "domain": d.plugin.name}

    def after_intent(state: TaskState) -> str:
        if state.get("status") == "failed":
            return "stop_report"
        result = IntentResult(status=IntentStatus(state["status"]), goal=state.get("goal", ""), constraints=())
        route = route_intent(result, audit=scope(state))
        return {IntentRoute.DISCOVERY: "discovery_start", IntentRoute.ASK_USER: "ask_user", IntentRoute.REPORT: "stop_report"}[route]

    async def ask_user(state: TaskState) -> dict[str, object]:
        return await ask_user_node(state, plugin=d.plugin, provider=d.provider, audit=scope(state))

    def after_ask_user(state: TaskState) -> str:
        status = state.get("status")
        if status in {"failed", "cancelled", IntentStatus.OUT_OF_SCOPE.value}:
            return "stop_report"
        # Still ambiguous or conflicting after the reply: ask again (each round waits on the user, it never spins).
        return "ask_user" if status == IntentStatus.BLOCKED.value else "discovery_start"

    def adapt(state: TaskState) -> dict[str, object]:
        return adapt_node(state, plugin=d.plugin, now=d.now(), audit=scope(state))

    def after_adapt(state: TaskState) -> str:
        step = state["adapt"].next_step
        if step is RecoveryStep.ASK_USER:
            return "ask_user"
        return "stop_report" if step is None or step is RecoveryStep.STOP else "discovery_start"

    # ---- discovery loop ----------------------------------------------------------------------------

    def discovery_start(state: TaskState) -> dict[str, object]:
        digest = progress_hash((c.candidate_id for c in state.get("candidates", [])), state.get("constraints", []))
        # ``cycles`` / ``cycle_hashes`` use add-reducers: the node contributes a delta, never rewrites the total.
        return {**discovery_start_node(state, audit=scope(state)), "cycles": 1, "cycle_hashes": [digest]}

    def after_discovery_start(state: TaskState) -> str:
        return {LimitRoute.CONTINUE: "discovery_search", LimitRoute.STOP: "stop_report"}[route_limits(state, d.limits)]

    async def discovery_search(state: TaskState) -> dict[str, object]:
        return await discovery_search_node(
            state, registry=d.registry, plugin=d.plugin, policy=d.discovery_policy, audit=scope(state)
        )

    async def discovery_adapt(state: TaskState) -> dict[str, object]:
        return await discovery_adapt_node(
            state, plugin=d.plugin, policy=d.discovery_policy, provider=d.provider, audit=scope(state)
        )

    def discovery_advance(state: TaskState) -> dict[str, object]:
        return discovery_advance_node(state, audit=scope(state))

    def discovery_finish(state: TaskState) -> dict[str, object]:
        return discovery_finish_node(state, policy=d.discovery_policy, audit=scope(state))

    def after_discovery(state: TaskState) -> str:
        platforms = len(d.registry.scoped(state["authorization"], d.plugin.name))
        route = route_discovery(state["discovery"], platform_count=platforms, policy=d.discovery_policy, audit=scope(state))
        return {
            DiscoveryRoute.SEARCH: "discovery_search", DiscoveryRoute.ADAPT_QUERY: "discovery_adapt",
            DiscoveryRoute.NEXT_PLATFORM: "discovery_advance", DiscoveryRoute.EVALUATE: "discovery_finish",
        }[route]

    # ---- evaluate / coordinate ---------------------------------------------------------------------

    async def evaluate(state: TaskState) -> dict[str, object]:
        _, update = await evaluator_node(
            state, now=d.now(), text_source=d.text_source, provider=d.provider, audit=scope(state)
        )
        return update

    # ---- guard nodes: every path into the Evaluator, Executor and Payment passes through one ----------

    def guarded(passed: str) -> Callable[[TaskState], str]:
        """Route on the Guard's status. Unknown status fails closed to ``stop_report``."""
        table = {GUARD_PASSED: passed, GUARD_BLOCKED: "stop_report"}
        return lambda state: table.get(state.get("status", ""), "stop_report")

    def guard_evaluate(state: TaskState) -> dict[str, object]:
        return guard_discovery_data(state, now=d.now(), audit=scope(state))

    def guard_execute(state: TaskState) -> dict[str, object]:
        return guard_execution(state, now=d.now(), audit=scope(state))

    def guard_pay(state: TaskState) -> dict[str, object]:
        terms = state["approvals"][-1].terms if state.get("approvals") else None
        if terms is None:
            return {"status": GUARD_BLOCKED, "errors": ["guard blocked payment: no approved terms on record"]}
        return guard_payment(
            state, terms=state.get("agreed_terms") or d.terms_for(candidate(state), state), verifier=permit_for(state, terms).verifier,
            now=d.now(), audit=scope(state),
        )

    def stop_report(state: TaskState) -> dict[str, object]:
        return stop_report_node(state, limits=d.limits, audit=scope(state))

    def after_evaluate(state: TaskState) -> str:
        route = route_evaluation(state["evaluation"], audit=scope(state))
        if route is EvaluatorRoute.EXECUTE and state["evaluation"].needs_choice:
            return "choose_candidate"  # near-tie: the user picks, the code never breaks the tie silently
        return {EvaluatorRoute.EXECUTE: "select", EvaluatorRoute.SEARCH: "discovery_start", EvaluatorRoute.COORDINATOR: "coordinator"}[route]

    async def coordinator(state: TaskState) -> dict[str, object]:
        ranked = [r.candidate_id for r in state["evaluation"].ranked] if state.get("evaluation") else []
        event = CoordinatorEvent(trigger=Trigger.NONE_VIABLE, alternatives=tuple(ranked), can_widen_search=True)
        result, update = await coordinate(state, event, audit=scope(state), provider=d.provider)
        return {**update, "status": f"coordinator_{result.action.value}"}

    def after_coordinator(state: TaskState) -> str:
        progress = state["coordinator"]
        route = route_coordinator_result(
            CoordinatorResult(progress.last_action or CoordinatorAction.ASK_USER, "persisted", stop_reason=progress.stop_reason)
        )
        return {
            # WIDEN/REPLAN go through Adapt's recovery ladder (local -> cross-domain -> relaxation the user approves):
            # re-running discovery with unchanged constraints would only repeat the same pool.
            CoordinatorRoute.SEARCH: "adapt", CoordinatorRoute.SWITCH_CANDIDATE: "switch_candidate",
            CoordinatorRoute.ASK_USER: "adapt", CoordinatorRoute.STOP: "stop_report",
        }[route]

    def select(state: TaskState) -> dict[str, object]:
        ranked = state["evaluation"].ranked
        return {"selected_candidate_id": ranked[0].candidate_id, "status": "selected"}

    def choose_candidate(state: TaskState) -> dict[str, object]:
        """Pause for the user's pick (a near tie, or after they declined a product). Nothing above ``interrupt`` has side effects."""
        declined = set(state.get("declined_candidate_ids", []))
        ranked = [r.candidate_id for r in state["evaluation"].ranked if r.candidate_id not in declined][:_MAX_CHOICES]
        titles = {c.candidate_id: c.title for c in state.get("candidates", [])}
        lines = [f"{i}. {titles.get(cid, cid)[:80]}" for i, cid in enumerate(ranked, start=1)]
        intro = (
            "Okay, I won't buy that one. Reply with the number of the product you want instead:"
            if declined else "These options are nearly tied. Reply with the number of the one you want:"
        )
        raw = interrupt({"type": "candidate_choice", "options": ranked, "message": intro + "\n" + "\n".join(lines)})
        picked = _pick_candidate(raw, ranked, titles)
        if picked is None:
            return {"status": "choice_invalid", "errors": ["the reply did not name one of the offered options"]}
        return {"selected_candidate_id": picked, "status": "selected"}

    def after_choose(state: TaskState) -> str:
        return "guard_execute" if state.get("status") == "selected" else "choose_candidate"

    def switch_candidate(state: TaskState) -> dict[str, object]:
        ids = [r.candidate_id for r in state["evaluation"].ranked]
        current = state.get("selected_candidate_id")
        later = ids[ids.index(current) + 1 :] if current in ids else ids
        if not later:
            return {"status": "failed", "errors": ["no alternative candidate is left"]}
        return {"selected_candidate_id": later[0], "status": "selected"}

    def after_switch(state: TaskState) -> str:
        return "stop_report" if state.get("status") == "failed" else "guard_execute"

    # ---- execute: recheck -> approval -> order -> payer -> settle ---------------------------------------

    async def recheck(state: TaskState) -> dict[str, object]:
        chosen = candidate(state)
        terms = d.terms_for(chosen, state)
        async with session_scope(d.session_factory) as session:
            checked = await d.executor.recheck(session, run_id=state["run_id"], terms=terms)
        update: dict[str, object] = {"recheck_report": checked.report, "status": "rechecked", "agreed_terms": None}
        if checked.report.can_proceed:
            update["pending_approval"] = PendingApproval(
                action=Action.CREATE_ORDER, candidate_id=chosen.candidate_id, platform=chosen.source, terms=terms,
                terms_hash=terms.terms_hash(),
            )
        return update

    def after_recheck(state: TaskState) -> str:
        return _executor_target(route_execution(state["recheck_report"], audit=scope(state)))

    def request_approval(state: TaskState) -> dict[str, object]:
        return request_approval_node(state, audit=scope(state))

    def can_reselect(state: TaskState) -> bool:
        """A one-shot purchase can switch to another product; plans, milestones and deals cannot."""
        return d.milestones is None and not d.plugin.negotiates and bool(state.get("selected_candidate_id")) and "evaluation" in state

    def decline_candidate(state: TaskState) -> dict[str, object]:
        """The user declined the product: remember it and let them pick another from the evaluated list."""
        declined = [*state.get("declined_candidate_ids", []), state["selected_candidate_id"]]
        remaining = [r for r in state["evaluation"].ranked if r.candidate_id not in declined]
        if not remaining:
            return {"declined_candidate_ids": declined, "status": "failed", "errors": ["no other product is left to choose from"]}
        return {"declined_candidate_ids": declined, "status": "choosing"}

    def after_decline(state: TaskState) -> str:
        return "stop_report" if state.get("status") == "failed" else "choose_candidate"

    def after_approval(state: TaskState) -> str:
        if state.get("status") == "approval_rejected" and can_reselect(state):
            return "decline_candidate"
        if state.get("status") != "approved":
            return "stop_report"
        if d.milestones is not None and not state.get("milestone_seq"):
            return "guard_plan"  # the user approved the whole plan; each milestone is approved and paid on its own
        return "guard_pay"

    async def place_order(state: TaskState) -> dict[str, object]:
        terms = state["approvals"][-1].terms
        assert terms is not None
        permit = permit_for(state, terms)
        round_ = state.get("negotiation_round", 0)
        order_id = f"order-{state['run_id']}-{terms.terms_hash()[:12]}" + (f"-r{round_}" if round_ else "")
        try:
            async with session_scope(d.session_factory) as session:
                try:
                    placed = await d.executor.place_order(
                        session, permit=permit, approval=next(a for a in permit.approvals if a.action is Action.CREATE_ORDER),
                        terms=terms, order_id=order_id, payee_id=terms.parties["payee_id"],
                    )
                except ExecutionBlocked as blocked:  # commit the recheck audit row before routing on it
                    return {"recheck_report": blocked.report, "status": "blocked"}
        except PaymentServiceError as exc:
            return {"errors": [f"payment refused: {exc}"], "status": "payment_refused"}
        except ProviderError as exc:  # PayPal refused or was unreachable; no funds are held before authorization
            return {"errors": [f"payment provider error: {exc}"], "status": "payment_refused"}
        return {
            "order_id": order_id, "payment_id": str(placed.created.payment.id), "approval_url": placed.created.approval_url,
            "settle_attempt": 0, "status": "awaiting_payer",
            "anchor_at": d.now(),  # the Monitor turns "within N days" into a date from here
        }

    def after_place_order(state: TaskState) -> str:
        if state.get("status") == "blocked":
            report = state["recheck_report"]
            if report.action is MismatchAction.REAPPROVE:
                return "guard_execute"  # approval no longer matches: through the Guard, re-check live terms, ask again
            return _executor_target(route_execution(report, audit=scope(state)))
        return "adapt" if state.get("status") == "payment_refused" else "await_payer"

    def await_payer(state: TaskState) -> dict[str, object]:
        interrupt({"kind": "payer_approval", "approval_url": state.get("approval_url")})
        return {"status": "payer_returned"}

    async def settle(state: TaskState) -> dict[str, object]:
        if d.before_settle is not None:
            await d.before_settle(state)
        if d.milestones is not None and state.get("milestone_seq"):
            return await settle_milestone_step(state, flow=d.milestones, session_factory=d.session_factory, permit_for=permit_for)
        terms = state["approvals"][-1].terms
        assert terms is not None
        attempt = state.get("settle_attempt", 0) + 1
        try:
            async with session_scope(d.session_factory) as session:
                settlement = await d.executor.settle(
                    session, UUID(state["payment_id"]), permit=permit_for(state, terms), terms=terms,
                    order_id=state["order_id"], attempt=attempt,
                )
        except PaymentServiceError as exc:
            route = route_payment_error(exc)
            return {"status": "payer_pending" if route is PaymentRoute.WAIT else "payment_refused", "errors": [str(exc)]}
        except ProviderError as exc:
            # Uncertain (timeout/5xx): the service re-reads PayPal and replays the same keys, so settling again is safe.
            return {"status": "payer_pending" if exc.uncertain else "payment_refused", "errors": [f"payment provider error: {exc}"]}
        return {"settle_attempt": attempt, "status": settlement.state.value}

    def after_settle(state: TaskState) -> str:
        status = state.get("status")
        if d.milestones is not None and state.get("milestone_seq"):
            return {
                "payer_pending": "await_payer", ExecutionState.SETTLED.value: "milestone_next",
                ExecutionState.VOIDED.value: "milestone_next", ExecutionState.EXPIRED.value: "milestone_next",
                ExecutionState.AWAITING_CONFIRMATION.value: END,
            }.get(status or "", "stop_report")
        if status == "payer_pending":
            return "await_payer"  # the payer has not approved yet: pause again (PaymentNotReady is safe to retry)
        if status == "payment_refused":
            return "adapt"
        if status == ExecutionState.VOIDED.value:
            # Verification failed or the terms changed and the funds are released. A negotiating domain (contractor /
            # A2A) renegotiates inside the user's envelope; any other domain searches again.
            return "guard_negotiate" if can_negotiate(state) else "discovery_start"
        return END  # settled, or awaiting confirmation (the worker/Monitor resumes it), or expired

    # ---- negotiation (contractor / A2A): settle FAIL or a counter-offer -> Negotiator -> Router ---------------------

    def can_negotiate(state: TaskState) -> bool:
        return d.plugin.negotiates and d.envelope_for is not None and bool(state.get("approvals"))

    def guard_negotiate(state: TaskState) -> dict[str, object]:
        if d.envelope_for is None:  # this domain's runtime does not negotiate (e.g. fixed milestone plans)
            return {"status": GUARD_BLOCKED, "errors": ["guard blocked negotiation: not available for this task"]}
        return guard_negotiation(state, now=d.now(), audit=scope(state))

    # ---- milestones (contractor): plan -> approve plan -> per milestone: approve -> pay -> verify -> capture ----------

    def flow() -> MilestoneFlow:
        if d.milestones is None:
            raise RuntimeError("milestone node reached in a domain without a milestone flow")
        return d.milestones

    async def propose_plan(state: TaskState) -> dict[str, object]:
        return await propose_plan_step(state, candidate(state), flow=flow(), audit=scope(state))

    def after_propose_plan(state: TaskState) -> str:
        return "recheck" if state.get("status") == STATUS_PLAN_PROPOSED else "stop_report"

    async def milestone_create(state: TaskState) -> dict[str, object]:
        return await create_plan_step(state, flow=flow(), session_factory=d.session_factory)

    def after_milestone_create(state: TaskState) -> str:
        return "milestone_next" if state.get("status") == STATUS_PLAN_CREATED else "stop_report"

    async def milestone_next(state: TaskState) -> dict[str, object]:
        return await next_milestone_step(
            state, candidate(state), flow=flow(), session_factory=d.session_factory, audit=scope(state)
        )

    def after_milestone_next(state: TaskState) -> str:
        routes = {STATUS_MILESTONE_READY: "request_approval", STATUS_COMPLETED: END}
        return routes.get(state.get("status", ""), "stop_report")

    async def milestone_pay(state: TaskState) -> dict[str, object]:
        result = await pay_milestone_step(state, flow=flow(), session_factory=d.session_factory, permit_for=permit_for)
        return {**result, "anchor_at": d.now()} if result.get("status") == "awaiting_payer" else result

    def after_milestone_pay(state: TaskState) -> str:
        return "await_payer" if state.get("status") == "awaiting_payer" else "stop_report"

    async def negotiate(state: TaskState) -> dict[str, object]:
        assert d.envelope_for is not None  # can_negotiate / the intake guard it
        return await negotiate_step(
            state, envelope_for=d.envelope_for, policy=d.negotiator_policy, provider=d.provider, audit=scope(state)
        )

    def after_negotiate(state: TaskState) -> str:
        route = route_negotiation(state["negotiation_result"], audit=scope(state))
        return {
            # An accepted deal is always re-approved: the earlier approval is single-use and already spent.
            NegotiatorRoute.EXECUTE: "request_approval", NegotiatorRoute.ASK_USER_APPROVAL: "request_approval",
            NegotiatorRoute.SEND_COUNTER: "send_counter", NegotiatorRoute.REPORT: "stop_report",
        }[route]

    async def send_counter(state: TaskState) -> dict[str, object]:
        return await send_counter_step(state, channel=d.counterparty, audit=scope(state))

    def after_send_counter(state: TaskState) -> str:
        return "await_counterparty" if state.get("status") == "awaiting_counterparty" else "stop_report"

    def await_counterparty(state: TaskState) -> dict[str, object]:
        raw = interrupt({"kind": "counterparty_response", "round": state.get("negotiation_round", 0)})
        return counterparty_reply(raw, audit=scope(state))

    def after_counterparty(state: TaskState) -> str:
        return "guard_negotiate" if state.get("incoming_offer") is not None else "stop_report"

    def negotiation_intake(state: TaskState) -> dict[str, object]:
        """Entry for an unsolicited counter-offer (``RunController.receive_counter_offer``): the offer is already
        sanitized in state; it goes through the same Guard and Negotiator as a solicited reply."""
        return {"status": STATUS_NEGOTIATING}

    def monitor_intake(state: TaskState) -> dict[str, object]:
        """Entry for Monitor-driven replans (``aupdate_state(..., as_node="monitor_intake")``). The worker has
        already released any committed funds through the Guard; this hands the task back to Adapt."""
        return {"status": "replanning"}

    graph = StateGraph(TaskState)
    for name, node in {
        "intent": intent, "ask_user": ask_user, "adapt": adapt, "discovery_start": discovery_start,
        "discovery_search": discovery_search, "discovery_adapt": discovery_adapt, "discovery_advance": discovery_advance,
        "discovery_finish": discovery_finish, "evaluate": evaluate, "coordinator": coordinator, "select": select, "choose_candidate": choose_candidate,
        "switch_candidate": switch_candidate, "decline_candidate": decline_candidate, "recheck": recheck, "request_approval": request_approval,
        "await_approval": await_approval_node, "place_order": place_order, "await_payer": await_payer, "settle": settle,
        "monitor_intake": monitor_intake, "guard_negotiate": guard_negotiate, "negotiate": negotiate,
        "send_counter": send_counter, "await_counterparty": await_counterparty, "negotiation_intake": negotiation_intake,
        "guard_evaluate": guard_evaluate, "guard_execute": guard_execute, "guard_pay": guard_pay, "stop_report": stop_report,
        "propose_plan": propose_plan, "guard_plan": guard_pay, "milestone_create": milestone_create,
        "milestone_next": milestone_next, "milestone_pay": milestone_pay,
    }.items():
        # Interrupt nodes wait on a human/counterparty, so their duration is not compute time.
        graph.add_node(name, node if name.startswith("await_") else timed(name, node))

    graph.add_edge(START, "intent")
    graph.add_conditional_edges("intent", after_intent, ["discovery_start", "ask_user", "stop_report"])
    graph.add_conditional_edges("adapt", after_adapt, ["ask_user", "discovery_start", "stop_report"])
    graph.add_conditional_edges("ask_user", after_ask_user, ["discovery_start", "ask_user", "stop_report"])
    graph.add_conditional_edges("discovery_start", after_discovery_start, ["discovery_search", "stop_report"])
    for name in ("discovery_search", "discovery_adapt", "discovery_advance"):
        graph.add_conditional_edges(name, after_discovery, ["discovery_search", "discovery_adapt", "discovery_advance", "discovery_finish"])
    graph.add_edge("discovery_finish", "guard_evaluate")
    graph.add_conditional_edges("guard_evaluate", guarded("evaluate"), ["evaluate", "stop_report"])
    graph.add_conditional_edges("evaluate", after_evaluate, ["select", "choose_candidate", "discovery_start", "coordinator"])
    graph.add_conditional_edges("choose_candidate", after_choose, ["guard_execute", "choose_candidate"])
    graph.add_conditional_edges("coordinator", after_coordinator, ["switch_candidate", "adapt", "stop_report"])
    graph.add_edge("select", "guard_execute")
    graph.add_conditional_edges("switch_candidate", after_switch, ["guard_execute", "stop_report"])
    graph.add_conditional_edges(
        "guard_execute", guarded("propose_plan" if d.milestones else "recheck"), ["propose_plan", "recheck", "stop_report"]
    )
    graph.add_conditional_edges("propose_plan", after_propose_plan, ["recheck", "stop_report"])
    graph.add_conditional_edges("recheck", after_recheck, ["request_approval", "evaluate", "adapt"])
    graph.add_edge("request_approval", "await_approval")
    graph.add_conditional_edges("await_approval", after_approval, ["guard_pay", "guard_plan", "decline_candidate", "stop_report"])
    graph.add_conditional_edges("decline_candidate", after_decline, ["choose_candidate", "stop_report"])
    graph.add_conditional_edges(
        "guard_pay", guarded("milestone_pay" if d.milestones else "place_order"), ["place_order", "milestone_pay", "stop_report"]
    )
    graph.add_conditional_edges("guard_plan", guarded("milestone_create"), ["milestone_create", "stop_report"])
    graph.add_conditional_edges("milestone_create", after_milestone_create, ["milestone_next", "stop_report"])
    graph.add_conditional_edges("milestone_next", after_milestone_next, ["request_approval", "stop_report", END])
    graph.add_conditional_edges("milestone_pay", after_milestone_pay, ["await_payer", "stop_report"])
    graph.add_edge("stop_report", END)
    graph.add_conditional_edges("place_order", after_place_order, ["await_payer", "guard_execute", "evaluate", "adapt"])
    graph.add_edge("await_payer", "settle")
    graph.add_edge("monitor_intake", "adapt")
    graph.add_conditional_edges(
        "settle", after_settle, ["await_payer", "adapt", "discovery_start", "guard_negotiate", "milestone_next", "stop_report", END]
    )
    graph.add_conditional_edges("guard_negotiate", guarded("negotiate"), ["negotiate", "stop_report"])
    graph.add_conditional_edges("negotiate", after_negotiate, ["request_approval", "send_counter", "stop_report"])
    graph.add_conditional_edges("send_counter", after_send_counter, ["await_counterparty", "stop_report"])
    graph.add_conditional_edges("await_counterparty", after_counterparty, ["guard_negotiate", "stop_report"])
    graph.add_edge("negotiation_intake", "guard_negotiate")
    return graph.compile(checkpointer=checkpointer)


def _pick_candidate(raw: object, ranked: list[str], titles: dict[str, str] | None = None) -> str | None:
    """The user's reply as a 1-based option number, an exact candidate id, or an exact title; anything else picks nothing."""
    text = str(raw.get("text", "")) if isinstance(raw, dict) else str(raw)
    text = text.strip().rstrip(".")
    if text.isdigit() and 1 <= int(text) <= len(ranked):
        return ranked[int(text) - 1]
    if text in ranked:
        return text
    by_title = [cid for cid in ranked if titles and titles.get(cid, "").casefold() == text.casefold()]
    return by_title[0] if len(by_title) == 1 else None


def _executor_target(route: ExecutorRoute) -> str:
    return {
        ExecutorRoute.AUTHORIZE: "request_approval", ExecutorRoute.EVALUATE: "evaluate",
        ExecutorRoute.ADAPT: "adapt", ExecutorRoute.ASK_USER: "adapt",
    }[route]


__all__ = ["GraphDeps", "build_graph"]
