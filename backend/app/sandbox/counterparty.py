"""The sandbox counterparty's moving parts, each plugged in where a real marketplace or agent would be.

* ``SandboxConnector``      a connector over the catalog (search, details, reviews, stock), reusing ``FixtureConnector``
* ``SandboxCounterparty``   answers counter-offers (``CounterpartyChannel``), keeps its latest quote per run, and
                            reports delivery as Monitor events through the shared ``EventProcessor``
* ``QuotedTermsObserver``   the live price is the counterparty's latest quote for the run, else its listed price
* ``fulfilment``            the graph's ``before_settle`` hook: the counterparty does the paid work and reports it
* ``SandboxMilestonePlanner`` a contractor's milestone plan from its standard template, inside the user's deadline
* ``SandboxMilestoneObserver`` live view of one milestone: the contractor honours the amount and due date it agreed

Replies re-enter the run only through ``RunController`` (sanitized offer -> Guard -> Negotiator); delivery reports
go through the same validation, dedupe and audit as external events. Nothing here can approve or move money.
"""

import asyncio
import math
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.connectors.base import Capability, ConnectorError, specs
from app.connectors.base.models import SearchRequest, SearchResponse
from app.connectors.mocks.fixture import FixtureConnector
from app.core.logging import get_logger
from app.db.session import session_scope
from app.domains.base import DomainPlugin
from app.domains.contractor.schemas import MilestonePlan, MilestoneSpec
from app.domains.shopping_execution import TermsObserver
from app.monitor.events.processor import EventProcessor
from app.orchestrator.graph.runtime import RunController
from app.sandbox.catalog import Behavior, Offering, offering_for, parse_subject
from app.schemas.approval_terms import ApprovalTerms, to_minor_units
from app.schemas.candidate import Candidate
from app.schemas.monitor import MonitorEvent, Problem
from app.schemas.negotiator import CounterOffer
from app.schemas.recheck import ObservedTerms
from app.schemas.state import TaskState

logger = get_logger(__name__)

EVENT_SOURCE = "sandbox"
_RENEGOTIATED = re.compile(r"-r\d+$")  # ``place_order`` suffixes the order id with the negotiation round
_PAUSE_WAIT_SECONDS = 30.0
_PAUSE_POLL_SECONDS = 0.25
_sleep = asyncio.sleep  # bound at import: the poll must really yield to the run it waits for


def _now() -> datetime:
    return datetime.now(UTC)


class SandboxConnector:
    """Catalog listings for one source. Prices are listed per currency; a search returns only the run's currency."""

    def __init__(self, source: str, domain: str, offerings: tuple[Offering, ...], clock: Callable[[], datetime] = _now) -> None:
        self.id, self.domain = source, domain
        self.capabilities = specs(Capability.SEARCH, Capability.DETAILS, Capability.REVIEWS, Capability.STOCK)
        self._offerings = tuple(o for o in offerings if o.source == source and o.domain == domain)
        if not self._offerings:
            raise ValueError(f"no sandbox offerings for {source!r} in domain {domain!r}")
        self._clock = clock

    def _fixture(self) -> FixtureConnector:
        """Rebuilt per call so completion dates stay relative to now."""
        now = self._clock()
        currencies = sorted({c for o in self._offerings for c in o.prices})
        return FixtureConnector(
            id=self.id, domain=self.domain, capabilities=self.capabilities,
            listings=tuple(o.listing(c, now) for o in self._offerings for c in currencies if c in o.prices),
            reviews={o.candidate_id(c): o.review_texts(now) for o in self._offerings for c in o.prices},
            out_of_stock=frozenset(o.candidate_id(c) for o in self._offerings if not o.available for c in o.prices),
        )

    async def execute(self, capability: str, request: BaseModel) -> BaseModel:
        response = await self._fixture().execute(capability, request)
        if isinstance(request, SearchRequest) and isinstance(response, SearchResponse) and request.currency:
            wanted = request.currency.upper()
            return SearchResponse(listings=tuple(item for item in response.listings if item.candidate.currency == wanted))
        return response


class SandboxCounterparty:
    """The other side of every sandbox engagement. Call ``bind`` once the ``RunController`` exists."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], clock: Callable[[], datetime] = _now) -> None:
        self._factory, self._clock = session_factory, clock
        self._runs: RunController | None = None
        self._processor: EventProcessor | None = None
        self._quotes: dict[tuple[str, str], int] = {}  # (run_id, subject_id) -> latest price offered, minor units
        self._tasks: set[asyncio.Task[None]] = set()

    def bind(self, runs: RunController) -> None:
        self._runs = runs
        self._processor = EventProcessor(runs.load_task, clock=self._clock)

    def quote(self, run_id: str, subject_id: str) -> int | None:
        return self._quotes.get((run_id, subject_id))

    # ---- negotiation (CounterpartyChannel) --------------------------------------------------------

    async def send_counter(self, run_id: str, offer: CounterOffer) -> None:
        """Our counter-offer arrived. The reply is delivered once the run is paused waiting for it."""
        reply = self._reply(run_id, offer)
        task = asyncio.create_task(self._deliver(run_id, reply), name=f"sandbox-reply-{run_id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _reply(self, run_id: str, offer: CounterOffer) -> dict[str, object]:
        """Accept a price at or above the listed one; otherwise meet halfway. Anything unknown is declined."""
        found = offering_for(offer.subject_id)
        if found is None or offer.milestones or offer.currency not in found[0].prices:
            return {"decision": "decline"}
        listed = to_minor_units(found[0].prices[offer.currency], offer.currency)
        price = offer.price_minor if offer.price_minor >= listed else (offer.price_minor + listed + 1) // 2
        self._quotes[(run_id, offer.subject_id)] = price
        return {
            "subject_id": offer.subject_id, "counterparty_id": offer.counterparty_id, "currency": offer.currency,
            "price_minor": price, "delivery_due": offer.delivery_due.isoformat(), "terms": dict(offer.terms),
        }

    async def _deliver(self, run_id: str, reply: dict[str, object]) -> None:
        runs = self._runs
        if runs is None:
            logger.error("sandbox counterparty is not bound to the run controller", extra={"run_id": run_id})
            return
        try:
            deadline = asyncio.get_running_loop().time() + _PAUSE_WAIT_SECONDS
            while "await_counterparty" not in (await runs.snapshot(run_id)).next:
                if asyncio.get_running_loop().time() > deadline:
                    logger.warning("run never paused for the counterparty's reply", extra={"run_id": run_id})
                    return
                await _sleep(_PAUSE_POLL_SECONDS)
            if reply.get("decision") == "decline":
                await runs.resume(run_id, reply)
            else:
                await runs.receive_counter_offer(run_id, reply)
        except Exception:
            logger.exception("sandbox counterparty reply failed", extra={"run_id": run_id})

    # ---- delivery ---------------------------------------------------------------------------------

    async def perform(self, plugin: DomainPlugin, terms: ApprovalTerms, order_id: str) -> None:
        """Do the paid work and report it as a Monitor event (idempotent per order and outcome)."""
        processor = self._processor
        found = parse_subject(terms.subject_id)
        if processor is None or found is None:
            return  # unknown subject or unbound: no report, so the verifier sees "not yet confirmed"
        offering, _, sequence = found
        if sequence is not None:  # one milestone of a plan
            rejects = offering.rejects_milestone == sequence
        else:
            rejects = offering.behavior is Behavior.REJECTS_FIRST and not _RENEGOTIATED.search(order_id)
        event_type = _failure_type(plugin) if rejects else _completion_type(plugin)
        event = MonitorEvent(
            event_id=f"{order_id}:{event_type}", run_id=str(terms.domain_data["run_id"]), source=EVENT_SOURCE,
            domain=plugin.name, event_type=event_type, entity_id=order_id, occurred_at=self._clock(),
            payload={"behavior": offering.behavior.value},
        )
        async with session_scope(self._factory) as session:
            await processor.process(session, event)

    def fulfilment(self, plugin: DomainPlugin) -> Callable[[TaskState], Awaitable[None]]:
        """``GraphDeps.before_settle``: report delivery of the order in flight, in its own committed transaction,
        before settlement reads the evidence. Idempotent per order and outcome."""
        check_plugin(plugin)

        async def deliver(state: TaskState) -> None:
            order_id = state.get("order_id")
            terms = state.get("agreed_terms") or (state["approvals"][-1].terms if state.get("approvals") else None)
            if order_id and terms is not None:
                await self.perform(plugin, terms, order_id)

        return deliver

    async def aclose(self) -> None:
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)


def _completion_type(plugin: DomainPlugin) -> str:
    types = sorted(plugin.monitor.completed_event_types)
    if not types:
        raise ConnectorError(f"domain {plugin.name!r} declares no completion event")
    return types[0]


def _failure_type(plugin: DomainPlugin) -> str:
    rule = next((r for r in plugin.monitor.rules if r.problem is Problem.WORK_REJECTED), None)
    if rule is None:
        raise ConnectorError(f"domain {plugin.name!r} declares no work-rejected event")
    return rule.event_type


def check_plugin(plugin: DomainPlugin) -> None:
    """Fail at startup, not mid-run, if a domain cannot be served by the sandbox."""
    _completion_type(plugin)
    _failure_type(plugin)


class QuotedTermsObserver:
    def __init__(self, inner: TermsObserver, counterparty: SandboxCounterparty) -> None:
        self._inner, self._counterparty = inner, counterparty

    async def observe(self, terms: ApprovalTerms) -> ObservedTerms:
        observed = await self._inner.observe(terms)
        quoted = self._counterparty.quote(str(terms.domain_data.get("run_id", "")), terms.subject_id)
        if quoted is None or observed.total_minor is None:
            return observed  # unknown live price stays unknown
        return observed.model_copy(update={"total_minor": quoted})


class SandboxMilestonePlanner:
    """Splits the contractor's price over its standard milestones. Due dates follow its lead time and never pass the
    user's deadline; amounts are exact shares of the price, the last one absorbing rounding."""

    def __init__(self, payee_id: str, clock: Callable[[], datetime] = _now) -> None:
        if not payee_id:
            raise ValueError("a payee merchant id is required")
        self._payee_id, self._clock = payee_id, clock

    async def propose(self, candidate: Candidate, state: TaskState) -> MilestonePlan:
        found = parse_subject(candidate.candidate_id)
        if found is None or found[2] is not None:
            raise ValueError(f"{candidate.candidate_id!r} is not a sandbox contractor")
        offering, currency, _ = found
        deadline = state.get("deliver_by")
        if deadline is None:
            raise ValueError("a milestone plan needs the user's completion deadline")
        if candidate.total_cost is None or candidate.currency != currency:
            raise ValueError("the contractor's price is unknown")
        total, templates, now = Decimal(candidate.total_cost), offering.milestones, self._clock()
        if deadline <= now:
            raise ValueError("the completion deadline has already passed")
        specs: list[MilestoneSpec] = []
        allocated = Decimal(0)
        for index, template in enumerate(templates, start=1):
            share = (total * template.share_percent / 100).quantize(Decimal("0.01"))
            amount = total - allocated if index == len(templates) else share
            allocated += amount
            due = min(deadline, now + timedelta(days=math.ceil(offering.lead_days * index / len(templates))))
            specs.append(MilestoneSpec(description=template.description, amount=amount, due_date=due, acceptance_criteria=template.criteria))
        return MilestonePlan(
            project_id=candidate.candidate_id, contractor_id=self._payee_id, currency=currency, budget=total, milestones=tuple(specs)
        )


class SandboxMilestoneObserver:
    async def observe(self, terms: ApprovalTerms) -> ObservedTerms:
        found = parse_subject(terms.subject_id)
        if found is None or found[2] is None:
            return ObservedTerms(subject_id=terms.subject_id, currency=terms.currency)  # unknown -> INSUFFICIENT_DATA
        return ObservedTerms(
            available=found[0].available, subject_id=terms.subject_id, parties=dict(terms.parties), quantity=terms.quantity,
            currency=terms.currency, total_minor=terms.expected_total_minor, available_by=terms.latest_acceptable_at,
            domain_data=dict(terms.domain_data),
        )


__all__ = [
    "EVENT_SOURCE", "QuotedTermsObserver", "SandboxConnector", "SandboxCounterparty",
    "SandboxMilestoneObserver", "SandboxMilestonePlanner", "check_plugin",
]
