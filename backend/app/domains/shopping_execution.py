"""Shopping's execution adapters: the exact terms a user approves, the live recheck, and delivery evidence.

* ``ShoppingTerms``          candidate -> ``ApprovalTerms`` (item, price ceiling, payee, delivery deadline)
* ``ConnectorTermsObserver`` re-reads price / stock / delivery from the connector through the guarded registry
* ``MonitorEventEvidence``   delivery evidence from the stored, signature-verified Monitor events for the order

Authority is derived from the approved terms: the observer may only SEARCH the one approved platform, so a recheck
can never widen scope. Nothing here trusts connector text; only typed, numeric and boolean fields are read.
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.connectors.base import ConnectorError
from app.connectors.base.models import CandidateRequest
from app.connectors.registry import ConnectorRegistry
from app.core.logging import get_logger
from app.domains.base import DomainPlugin
from app.monitor.events.deadline import resolve_deadline
from app.monitor.events.store import applied_rows, latest_by_entity
from app.schemas.approval_terms import ApprovalTerms, from_minor_units, to_minor_units
from app.schemas.authorization import Action, TaskAuthorization
from app.schemas.candidate import Candidate
from app.schemas.monitor import Problem, TaskView
from app.schemas.recheck import ObservedTerms
from app.schemas.state import TaskState
from app.schemas.verification import Evidence


class TermsObserver(Protocol):
    async def observe(self, terms: ApprovalTerms) -> ObservedTerms: ...

logger = get_logger(__name__)

PERMISSION = "purchase"
DELIVERY_FIELD = "delivery_days"
_FAILED = frozenset({"DELIVERY_FAILED", "RETURNED"})
_OBSERVER_AUTH_TTL = timedelta(minutes=5)


class ShoppingTerms:
    """``GraphDeps.terms_for`` for shopping. ``payee_id`` is the merchant that receives the payment."""

    def __init__(self, payee_id: str, clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        if not payee_id:
            raise ValueError("a payee merchant id is required")
        self._payee_id, self._clock = payee_id, clock

    def __call__(self, candidate: Candidate, state: TaskState) -> ApprovalTerms:
        if candidate.total_cost is None or candidate.currency is None:
            raise ValueError(f"candidate {candidate.candidate_id} has no known total cost; it cannot be approved")
        minor = to_minor_units(candidate.total_cost, candidate.currency)
        return ApprovalTerms(
            subject_id=candidate.candidate_id,
            subject_attributes={"title": candidate.title},
            parties={"payee_id": self._payee_id},
            currency=candidate.currency,
            max_total_minor=minor,
            expected_total_minor=minor,
            latest_acceptable_at=self._deadline(state),
            permissions=frozenset({PERMISSION}),
            domain_data={"platform": candidate.source, "run_id": state["run_id"]},
            display={"title": candidate.title, "seller": candidate.source},
        )

    def _deadline(self, state: TaskState) -> datetime | None:
        view = TaskView(
            run_id=state["run_id"], domain="shopping", constraints=tuple(state.get("constraints", [])),
            anchor_at=state.get("anchor_at") or self._clock(),
        )
        resolved = resolve_deadline(view, DELIVERY_FIELD)
        return resolved.at if resolved else None


def _observed(terms: ApprovalTerms, **live: object) -> ObservedTerms:
    """Observed terms. Fields the connector cannot re-observe (payee, platform, run) are the approved ones."""
    return ObservedTerms(
        subject_id=terms.subject_id, currency=terms.currency, parties=dict(terms.parties), quantity=terms.quantity,
        domain_data=dict(terms.domain_data), **live,  # type: ignore[arg-type]
    )


class ConnectorTermsObserver:
    def __init__(self, registry: ConnectorRegistry, clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._registry, self._clock = registry, clock

    async def observe(self, terms: ApprovalTerms) -> ObservedTerms:
        platform, run_id = str(terms.domain_data["platform"]), str(terms.domain_data["run_id"])
        auth = TaskAuthorization(
            run_id=run_id, max_spend=from_minor_units(terms.max_total_minor, terms.currency), currency=terms.currency,
            permitted_platforms=frozenset({platform}), permitted_actions=frozenset({Action.SEARCH}),
            expires_at=self._clock() + _OBSERVER_AUTH_TTL,
        )
        request = CandidateRequest(candidate_id=terms.subject_id)
        try:
            candidate = await self._registry.details(auth, platform, request)
            stock = await self._registry.stock(auth, platform, request)
        except ConnectorError as exc:
            logger.warning("recheck could not read the connector", extra={"run_id": run_id, "reason": str(exc)})
            return _observed(terms)  # everything unknown -> INSUFFICIENT_DATA: refresh or ask, never guess
        return _observed(
            terms,
            available=stock.available and (stock.quantity is None or stock.quantity >= terms.quantity),
            subject_attributes={"title": candidate.title},
            total_minor=_total_minor(candidate, terms.currency),
            available_by=candidate.available_by,
        )


def _total_minor(candidate: Candidate, currency: str) -> int | None:
    if candidate.total_cost is None or candidate.currency is None or candidate.currency.upper() != currency:
        return None  # a different currency is not comparable: unknown, so the recheck blocks
    return to_minor_units(Decimal(candidate.total_cost), currency)


class MonitorEventEvidence:
    """Delivery evidence from stored Monitor events for the order (entity id == order id).

    DELIVERED confirms and its timestamp is the delivery date (a late one fails the deadline check); a failed
    delivery or return rejects; anything else is "not yet confirmed" and the executor keeps waiting, then voids."""

    def __init__(
        self,
        observer: TermsObserver,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        confirmed_types: frozenset[str] = frozenset({"DELIVERED"}),
        failed_types: frozenset[str] = _FAILED,
    ) -> None:
        self._observer, self._factory = observer, session_factory
        self._confirmed, self._failed = confirmed_types, failed_types

    @classmethod
    def for_plugin(
        cls, plugin: DomainPlugin, observer: TermsObserver, session_factory: async_sessionmaker[AsyncSession]
    ) -> "MonitorEventEvidence":
        """Evidence read through a domain's ``MonitorSpec``: its completion events confirm, its work-rejected and
        non-delivery events reject."""
        failed = frozenset(
            r.event_type for r in plugin.monitor.rules if r.problem in (Problem.WORK_REJECTED, Problem.NON_DELIVERY)
        )
        return cls(observer, session_factory, confirmed_types=plugin.monitor.completed_event_types, failed_types=failed)

    async def collect(self, terms: ApprovalTerms, order_id: str) -> Evidence:
        run_id = str(terms.domain_data["run_id"])
        async with self._factory() as session:  # read-only
            latest = latest_by_entity(await applied_rows(session, run_id, order_id)).get(order_id)
        observed = await self._observer.observe(terms)
        confirmed: bool | None = None
        if latest is not None and latest.event_type in self._confirmed:
            confirmed = True
            delivered_at = latest.occurred_at if latest.occurred_at.tzinfo else latest.occurred_at.replace(tzinfo=UTC)
            observed = observed.model_copy(update={"available_by": delivered_at})
        elif latest is not None and latest.event_type in self._failed:
            confirmed = False
        return Evidence(source="monitor_events", observed=observed, confirmed=confirmed)
