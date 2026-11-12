"""Monitor worker: turns applied Monitor results into graph actions, and sweeps for passed deadlines.

Runs in the worker process (never an LLM): ``route_monitor_result`` decides, this module executes.

* ADAPT / ASK_USER      -> hand the run to Adapt (``RunController.replan``)
* REPLAN_POST_PURCHASE  -> release the money first (void a hold, refund a capture) through the Guard-gated payment
                           service, and only then Adapt. If the release is blocked (missing approval, denied, provider
                           not ready) the run is NOT replanned: buying again while funds are committed is never allowed.
* REMEDY                -> request a refund through ``monitor.remedy`` (gated), then Adapt
* CLOSE                 -> mark the run completed
"""

from collections.abc import Awaitable, Callable, Sequence
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit.events import AuditScope, AuditSink, Phase
from app.core.logging import get_logger
from app.db.models.payment import Payment, PaymentStatus
from app.db.session import session_scope
from app.monitor.events.processor import EventProcessor
from app.monitor.remedy import RemedyOutcome, request_refund
from app.monitor.timers.scheduler import overdue_events
from app.orchestrator.graph.runtime import RunController, UnknownRun
from app.orchestrator.guard.approval_guard import signed_approval_verifier
from app.orchestrator.router.monitor import route_monitor_result
from app.orchestrator.executor.state import ExecutionState
from app.payments.provider import ProviderError
from app.payments.service import PaymentPermit, PaymentService, PaymentServiceError
from app.domains import get_plugin
from app.schemas.monitor import EventOutcome, MonitorResult, Problem, Route

logger = get_logger(__name__)
_NODE = "monitor_worker"
_HELD = frozenset({PaymentStatus.CREATED.value, PaymentStatus.AUTHORIZED.value})
_OUTCOME_PROBLEMS = frozenset({Problem.NON_DELIVERY, Problem.WORK_REJECTED})


def _is_delivery_outcome(result: MonitorResult) -> bool:
    """An applied event that settles the evidence question: completed work, or failed / rejected delivery.
    Progress events (shipped, placed) are not: re-settling on them would only burn verification attempts."""
    if result.outcome is not EventOutcome.APPLIED:
        return False
    spec = get_plugin(result.event.domain).monitor
    rule = spec.rule(result.event.event_type)
    return result.event.event_type in spec.completed_event_types or (rule is not None and rule.problem in _OUTCOME_PROBLEMS)


class MonitorWorker:
    def __init__(
        self,
        runs: RunController,
        payments: PaymentService,
        session_factory: async_sessionmaker[AsyncSession],
        audit_sink: AuditSink,
        approval_keys: dict[str, bytes],
        active_runs: Callable[[], Awaitable[Sequence[str]]],
    ) -> None:
        self._runs, self._payments, self._factory = runs, payments, session_factory
        self._sink, self._keys, self._active_runs = audit_sink, approval_keys, active_runs

    async def handle(self, result: MonitorResult) -> Route:
        """Act on one applied result. Never raises for a run that cannot be acted on: the event is already stored."""
        route = route_monitor_result(result)
        run_id = result.event.run_id
        audit = AuditScope(self._sink, run_id)
        audit.emit(
            Phase.MONITOR, "MONITOR_ROUTED", node=_NODE, decision=route.value,
            metadata={"event_type": result.event.event_type, "problem": result.problem.value if result.problem else None},
        )
        try:
            if _is_delivery_outcome(result) and await self._awaiting_evidence(run_id):
                # A delivery outcome for an order whose settlement is waiting on evidence: settle again, which
                # captures on confirmed delivery and voids on failure (never refund money that was only held).
                await self._runs.retry_settlement(run_id)
                return route
            match route:
                case Route.CONTINUE_MONITORING:
                    pass
                case Route.CLOSE:
                    await self._runs.set_status(run_id, "completed")
                case Route.ADAPT | Route.ASK_USER:
                    await self._runs.replan(run_id)
                case Route.REPLAN_POST_PURCHASE:
                    if await self._release_funds(run_id, audit):
                        await self._runs.replan(run_id)
                case Route.REMEDY:
                    if await self._refund(run_id, audit):
                        await self._runs.replan(run_id)
        except UnknownRun:
            logger.warning("monitor result for an unknown run", extra={"run_id": run_id})
        return route

    async def _awaiting_evidence(self, run_id: str) -> bool:
        state = await self._runs.state(run_id)
        return state.get("status") == ExecutionState.AWAITING_CONFIRMATION.value and bool(state.get("payment_id"))

    async def release_for_cancel(self, run_id: str) -> bool:
        """The user cancelled: release whatever is committed through the Guard-gated payment service. True when
        nothing is left committed (no payment yet, voided, or already released); False when it could not be released
        (captured money needs an approved refund), in which case the run must stay open."""
        state = await self._runs.state(run_id)
        if not state.get("payment_id"):
            return True  # nothing was ordered yet
        return await self._release_funds(run_id, AuditScope(self._sink, run_id))

    async def sweep(self, processor: EventProcessor) -> int:
        """Raise ``DEADLINE_PASSED`` for overdue unfinished work and handle what applies. Idempotent per deadline."""
        handled = 0
        for run_id in await self._active_runs():
            try:
                view = await self._runs.load_task(run_id)
            except UnknownRun:
                continue
            async with session_scope(self._factory) as session:
                events = await overdue_events(session, view)
                results = [await processor.process(session, event) for event in events]
            for result in results:
                if result.outcome.value == "applied":
                    await self.handle(result)
                    handled += 1
        return handled

    # ---- money -------------------------------------------------------------------------------------

    async def _permit(self, run_id: str) -> tuple[PaymentPermit, UUID]:
        state = await self._runs.state(run_id)
        approvals = tuple(state.get("approvals", []))
        if not approvals or not state.get("payment_id"):
            raise PaymentServiceError("run has no approved payment")
        approved = approvals[-1]
        assert approved.terms is not None
        verifier = signed_approval_verifier(
            current_terms=approved.terms, keys=self._keys, run_id=run_id, session_id=approved.session_id or "",
            approved_by=approved.approved_by,
        )
        permit = PaymentPermit(authorization=state["authorization"], platform=approved.platform, approvals=approvals, verifier=verifier)
        return permit, UUID(state["payment_id"])

    async def _release_funds(self, run_id: str, audit: AuditScope) -> bool:
        """Void a hold or refund a capture. True only when nothing is left committed."""
        try:
            permit, payment_id = await self._permit(run_id)
            async with session_scope(self._factory) as session:
                payment = await session.get(Payment, payment_id)
                if payment is None:
                    raise PaymentServiceError("payment not found")
                if payment.status in _HELD:
                    await self._payments.void(session, payment_id, permit=permit, reason="replan after a Monitor problem")
                    return True
                if payment.status == PaymentStatus.CAPTURED.value:
                    decision = await request_refund(
                        session, self._payments, payment_id, permit=permit, reason="replan after a Monitor problem"
                    )
                    if decision.outcome is not RemedyOutcome.EXECUTED:
                        audit.emit(Phase.MONITOR, "REPLAN_BLOCKED", node=_NODE, decision=decision.outcome.value, reason=decision.reason)
                        await self._runs.set_status(run_id, "release_blocked")
                        return False
                return True  # already voided / refunded / failed: nothing is committed
        except (PaymentServiceError, ProviderError) as exc:  # denied, not ready, PayPal unreachable: retried later
            audit.emit(Phase.MONITOR, "REPLAN_BLOCKED", node=_NODE, decision="release_failed", reason=str(exc))
            await self._runs.set_status(run_id, "release_blocked")
            return False

    async def _refund(self, run_id: str, audit: AuditScope) -> bool:
        try:
            permit, payment_id = await self._permit(run_id)
            async with session_scope(self._factory) as session:
                decision = await request_refund(session, self._payments, payment_id, permit=permit, reason="non-delivery")
        except (PaymentServiceError, ProviderError) as exc:
            audit.emit(Phase.MONITOR, "REMEDY_FAILED", node=_NODE, decision="failed", reason=str(exc))
            await self._runs.set_status(run_id, "remedy_blocked")
            return False
        if decision.outcome is not RemedyOutcome.EXECUTED:
            await self._runs.set_status(run_id, f"remedy_{decision.outcome.value}")
        return decision.outcome is RemedyOutcome.EXECUTED
