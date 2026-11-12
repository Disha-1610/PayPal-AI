"""Engagement adapters that serve both a counterparty the user named (a quote) and, when enabled, the in-app sandbox.

Each one recognises a quoted subject (``Candidate.source == user_supplied``) and otherwise delegates to the sandbox
implementation, so one domain graph handles both kinds of runs.
"""

from decimal import Decimal

from app.counterparty.desk import CounterpartyDesk
from app.domains.contractor.schemas import MilestonePlan, MilestoneSpec
from app.domains.shopping_execution import TermsObserver
from app.orchestrator.graph.milestones import MilestonePlanner
from app.orchestrator.graph.negotiation import CounterpartyChannel
from app.schemas.approval_terms import ApprovalTerms
from app.schemas.candidate import Candidate
from app.schemas.counterparty import QUOTE_ID_PREFIX, USER_SUPPLIED_SOURCE
from app.schemas.negotiator import CounterOffer
from app.schemas.recheck import ObservedTerms
from app.schemas.state import TaskState


def is_quoted(terms: ApprovalTerms) -> bool:
    """A quoted deal, or one milestone of a quoted plan (``<candidate id>:<sequence>``; quote ids start ``q-``)."""
    return terms.domain_data.get("platform") == USER_SUPPLIED_SOURCE or terms.subject_id.startswith(QUOTE_ID_PREFIX)


class QuoteTermsObserver:
    """A quote is binding as given: the live terms are the quoted terms. Anything else goes to ``inner``."""

    def __init__(self, inner: TermsObserver | None) -> None:
        self._inner = inner

    async def observe(self, terms: ApprovalTerms) -> ObservedTerms:
        if not is_quoted(terms):
            if self._inner is None:
                return ObservedTerms(subject_id=terms.subject_id, currency=terms.currency)  # unknown -> INSUFFICIENT_DATA
            return await self._inner.observe(terms)
        return ObservedTerms(
            available=True, subject_id=terms.subject_id, parties=dict(terms.parties), quantity=terms.quantity,
            currency=terms.currency, total_minor=terms.expected_total_minor, available_by=terms.latest_acceptable_at,
            domain_data=dict(terms.domain_data),
        )


class QuoteMilestonePlanner:
    """A quoted contractor is paid in one milestone for the quoted price, due on the quoted date (never after the
    user's deadline). Sandbox contractors keep their own templates."""

    def __init__(self, payee_id: str, sandbox: MilestonePlanner | None = None) -> None:
        if not payee_id:
            raise ValueError("a payee merchant id is required")
        self._payee_id, self._sandbox = payee_id, sandbox

    async def propose(self, candidate: Candidate, state: TaskState) -> MilestonePlan:
        if candidate.source != USER_SUPPLIED_SOURCE:
            if self._sandbox is None:
                raise ValueError(f"no milestone planner for {candidate.source!r}")
            return await self._sandbox.propose(candidate, state)
        if candidate.total_cost is None or candidate.currency is None or candidate.available_by is None:
            raise ValueError("the quote has no price or delivery date")
        deadline = state.get("deliver_by")
        due = min(candidate.available_by, deadline) if deadline else candidate.available_by
        total = Decimal(candidate.total_cost).quantize(Decimal("0.01"))
        contact = state.get("counterparty")
        name = contact.name if contact is not None else "the contractor"
        return MilestonePlan(
            project_id=candidate.candidate_id[:64], contractor_id=self._payee_id, currency=candidate.currency, budget=total,
            milestones=(
                MilestoneSpec(
                    description=f"Complete the quoted work ({name})"[:512], amount=total, due_date=due,
                    acceptance_criteria=("The work matches the request and you confirm delivery",),
                ),
            ),
        )


class RoutedCounterparty:
    """``CounterpartyChannel``: counter-offers go to the named counterparty when the run has one, else the sandbox."""

    def __init__(self, desk: CounterpartyDesk, sandbox: CounterpartyChannel | None) -> None:
        self._desk, self._sandbox = desk, sandbox

    async def send_counter(self, run_id: str, offer: CounterOffer) -> None:
        if await self._desk.has_counterparty(run_id) or self._sandbox is None:
            await self._desk.send_counter(run_id, offer)
        else:
            await self._sandbox.send_counter(run_id, offer)


__all__ = ["QuoteMilestonePlanner", "QuoteTermsObserver", "RoutedCounterparty", "is_quoted"]
