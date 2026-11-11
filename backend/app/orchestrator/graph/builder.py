"""Assemble the task graph from the existing nodes. Wiring only: no decision is made here.

Every conditional edge calls a function from ``orchestrator.router``; every node is one existing agent /
executor step. Nodes never call each other. Two user interrupts bracket the money path: the platform approval
(``await_approval``) and the payer's approval at PayPal (``await_payer``).

    intent -> (ask_user | discovery loop | end)
    discovery loop -> evaluate -> (select | discovery | coordinator)
    coordinator -> (discovery | switch_candidate | adapt | end)
    select -> recheck -> (request_approval | evaluate | adapt) -> await_approval -> place_order
    place_order -> await_payer -> settle -> (end | discovery | await_payer)

Monitor events enter at ``monitor_intake`` (see ``workers.monitor``), which hands the task back to Adapt.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit.events import AuditScope, AuditSink
from app.connectors.registry import ConnectorRegistry
from app.db.base import utcnow
from app.db.session import session_scope
from app.domains.base import DomainPlugin
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
from app.orchestrator.agents.intent.agent import extract_intent, to_state_update
from app.orchestrator.executor.executor import ExecutionBlocked, Executor
from app.orchestrator.executor.state import ExecutionState
from app.orchestrator.graph.approval import await_approval_node, request_approval_node
from app.orchestrator.guard.approval_guard import signed_approval_verifier
from app.orchestrator.router.coordinator import CoordinatorRoute, route_coordinator_result
from app.orchestrator.router.discovery import route_discovery
from app.orchestrator.router.evaluator import EvaluatorRoute, route_evaluation
from app.orchestrator.router.executor import ExecutorRoute, route_execution
from app.orchestrator.router.intent import IntentRoute, route_intent
from app.orchestrator.router.payment import PaymentRoute, route_payment_error
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
from app.schemas.state import TaskState


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


def build_graph(deps: GraphDeps, checkpointer: BaseCheckpointSaver | None = None) -> Any:
    """Compile the graph. ``checkpointer`` makes it resumable across worker restarts."""
    d = deps

    def scope(state: TaskState) -> AuditScope:
        return AuditScope(d.audit_sink, state["run_id"])

    def candidate(state: TaskState) -> Candidate:
        wanted = state["selected_candidate_id"]
        return next(c for c in state.get("candidates", []) if c.candidate_id == wanted)

    def permit_for(state: TaskState, terms: ApprovalTerms) -> PaymentPermit:
        approvals = tuple(state.get("approvals", []))
        approved = approvals[-1]  # all approvals of one user decision share user, session and terms
        verifier = signed_approval_verifier(
            current_terms=terms, keys=d.approval_keys, run_id=state["run_id"], session_id=approved.session_id or "",
            approved_by=approved.approved_by,
        )
        return PaymentPermit(authorization=state["authorization"], platform=approved.platform, approvals=approvals, verifier=verifier)

    # ---- intent / user loop ------------------------------------------------------------------------

    async def intent(state: TaskState) -> dict[str, object]:
        result, diff = await extract_intent(state["original_intent"], d.plugin, provider=d.provider, audit=scope(state))
        return {**to_state_update(result, diff), "domain": d.plugin.name}

    def after_intent(state: TaskState) -> str:
        result = IntentResult(status=IntentStatus(state["status"]), goal=state.get("goal", ""), constraints=())
        route = route_intent(result, audit=scope(state))
        return {IntentRoute.DISCOVERY: "discovery_start", IntentRoute.ASK_USER: "adapt", IntentRoute.REPORT: END}[route]

    async def ask_user(state: TaskState) -> dict[str, object]:
        return await ask_user_node(state, plugin=d.plugin, provider=d.provider, audit=scope(state))

    def after_ask_user(state: TaskState) -> str:
        return END if state.get("status") in {"failed", "cancelled"} else "discovery_start"

    def adapt(state: TaskState) -> dict[str, object]:
        return adapt_node(state, plugin=d.plugin, now=d.now(), audit=scope(state))

    def after_adapt(state: TaskState) -> str:
        step = state["adapt"].next_step
        if step is RecoveryStep.ASK_USER:
            return "ask_user"
        return END if step is None or step is RecoveryStep.STOP else "discovery_start"

    # ---- discovery loop ----------------------------------------------------------------------------

    def discovery_start(state: TaskState) -> dict[str, object]:
        return discovery_start_node(state, audit=scope(state))

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

    def after_evaluate(state: TaskState) -> str:
        route = route_evaluation(state["evaluation"], audit=scope(state))
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
            CoordinatorRoute.SEARCH: "discovery_start", CoordinatorRoute.SWITCH_CANDIDATE: "switch_candidate",
            CoordinatorRoute.ASK_USER: "adapt", CoordinatorRoute.STOP: END,
        }[route]

    def select(state: TaskState) -> dict[str, object]:
        ranked = state["evaluation"].ranked
        return {"selected_candidate_id": ranked[0].candidate_id, "status": "selected"}

    def switch_candidate(state: TaskState) -> dict[str, object]:
        ids = [r.candidate_id for r in state["evaluation"].ranked]
        current = state.get("selected_candidate_id")
        later = ids[ids.index(current) + 1 :] if current in ids else ids
        if not later:
            return {"status": "failed", "errors": ["no alternative candidate is left"]}
        return {"selected_candidate_id": later[0], "status": "selected"}

    def after_switch(state: TaskState) -> str:
        return END if state.get("status") == "failed" else "recheck"

    # ---- execute: recheck -> approval -> order -> payer -> settle ---------------------------------------

    async def recheck(state: TaskState) -> dict[str, object]:
        chosen = candidate(state)
        terms = d.terms_for(chosen, state)
        async with session_scope(d.session_factory) as session:
            checked = await d.executor.recheck(session, run_id=state["run_id"], terms=terms)
        update: dict[str, object] = {"recheck_report": checked.report, "status": "rechecked"}
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

    def after_approval(state: TaskState) -> str:
        return "place_order" if state.get("status") == "approved" else END

    async def place_order(state: TaskState) -> dict[str, object]:
        terms = state["approvals"][-1].terms
        assert terms is not None
        permit = permit_for(state, terms)
        order_id = f"order-{state['run_id']}-{terms.terms_hash()[:12]}"
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
        return {
            "order_id": order_id, "payment_id": str(placed.created.payment.id), "approval_url": placed.created.approval_url,
            "settle_attempt": 0, "status": "awaiting_payer",
        }

    def after_place_order(state: TaskState) -> str:
        if state.get("status") == "blocked":
            return _executor_target(route_execution(state["recheck_report"], audit=scope(state)))
        return "adapt" if state.get("status") == "payment_refused" else "await_payer"

    def await_payer(state: TaskState) -> dict[str, object]:
        interrupt({"kind": "payer_approval", "approval_url": state.get("approval_url")})
        return {"status": "payer_returned"}

    async def settle(state: TaskState) -> dict[str, object]:
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
        return {"settle_attempt": attempt, "status": settlement.state.value}

    def after_settle(state: TaskState) -> str:
        status = state.get("status")
        if status == "payer_pending":
            return "await_payer"  # the payer has not approved yet: pause again (PaymentNotReady is safe to retry)
        if status == "payment_refused":
            return "adapt"
        if status == ExecutionState.VOIDED.value:
            return "discovery_start"  # verification failed or terms changed: funds released, search again
        return END  # settled, or awaiting confirmation (the worker/Monitor resumes it), or expired

    def monitor_intake(state: TaskState) -> dict[str, object]:
        """Entry for Monitor-driven replans (``aupdate_state(..., as_node="monitor_intake")``). The worker has
        already released any committed funds through the Guard; this hands the task back to Adapt."""
        return {"status": "replanning"}

    graph = StateGraph(TaskState)
    for name, node in {
        "intent": intent, "ask_user": ask_user, "adapt": adapt, "discovery_start": discovery_start,
        "discovery_search": discovery_search, "discovery_adapt": discovery_adapt, "discovery_advance": discovery_advance,
        "discovery_finish": discovery_finish, "evaluate": evaluate, "coordinator": coordinator, "select": select,
        "switch_candidate": switch_candidate, "recheck": recheck, "request_approval": request_approval,
        "await_approval": await_approval_node, "place_order": place_order, "await_payer": await_payer, "settle": settle,
        "monitor_intake": monitor_intake,
    }.items():
        graph.add_node(name, node)

    graph.add_edge(START, "intent")
    graph.add_conditional_edges("intent", after_intent, ["discovery_start", "adapt", END])
    graph.add_conditional_edges("adapt", after_adapt, ["ask_user", "discovery_start", END])
    graph.add_conditional_edges("ask_user", after_ask_user, ["discovery_start", END])
    graph.add_edge("discovery_start", "discovery_search")
    for name in ("discovery_search", "discovery_adapt", "discovery_advance"):
        graph.add_conditional_edges(name, after_discovery, ["discovery_search", "discovery_adapt", "discovery_advance", "discovery_finish"])
    graph.add_edge("discovery_finish", "evaluate")
    graph.add_conditional_edges("evaluate", after_evaluate, ["select", "discovery_start", "coordinator"])
    graph.add_conditional_edges("coordinator", after_coordinator, ["discovery_start", "switch_candidate", "adapt", END])
    graph.add_edge("select", "recheck")
    graph.add_conditional_edges("switch_candidate", after_switch, ["recheck", END])
    graph.add_conditional_edges("recheck", after_recheck, ["request_approval", "evaluate", "adapt"])
    graph.add_edge("request_approval", "await_approval")
    graph.add_conditional_edges("await_approval", after_approval, ["place_order", END])
    graph.add_conditional_edges("place_order", after_place_order, ["await_payer", "evaluate", "adapt"])
    graph.add_edge("await_payer", "settle")
    graph.add_edge("monitor_intake", "adapt")
    graph.add_conditional_edges("settle", after_settle, ["await_payer", "adapt", "discovery_start", END])
    return graph.compile(checkpointer=checkpointer)


def _executor_target(route: ExecutorRoute) -> str:
    return {
        ExecutorRoute.AUTHORIZE: "request_approval", ExecutorRoute.EVALUATE: "evaluate",
        ExecutorRoute.ADAPT: "adapt", ExecutorRoute.ASK_USER: "adapt",
    }[route]


__all__ = ["GraphDeps", "build_graph"]
