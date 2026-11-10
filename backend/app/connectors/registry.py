"""ConnectorRegistry: the only way agents reach a connector.

Enforced here for every connector, so none can opt out:
  * platform scoping (2.2): only ids in ``authorization.permitted_platforms``; no fallback
  * capability + action authorization via ``evaluate_authorization``
  * data quality (2.4) and attribute validation on every candidate
  * untrusted-data boundary (2.5): review/listing text goes to the ``TextStore``; callers only
    ever receive ids and aggregates, never the text
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from statistics import fmean

from pydantic import BaseModel, ConfigDict

from app.connectors.base import Capability, Connector, ConnectorError, TextRecord, TextStore
from app.connectors.base.models import (
    CandidateRequest,
    CheckoutRequest,
    CheckoutResponse,
    DeliveryRequest,
    DeliveryResponse,
    Listing,
    ReviewsRequest,
    ReviewsResponse,
    SearchRequest,
    SearchResponse,
    StockResponse,
)
from app.connectors.normalize import assess_quality, validate_attributes
from app.schemas.authorization import Approval, ApprovalVerifier, TaskAuthorization, evaluate_authorization
from app.schemas.candidate import Candidate

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 10.0
DEFAULT_MAX_AGE = timedelta(hours=24)


class ReviewSummary(BaseModel):
    """Text-free view of a candidate's reviews: ids plus aggregates."""

    model_config = ConfigDict(frozen=True)

    candidate_id: str
    text_refs: tuple[str, ...] = ()
    review_count: int = 0
    average_rating: float | None = None
    latest_review_at: datetime | None = None


class DiscoveryResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    candidates: tuple[Candidate, ...] = ()
    errors: tuple[str, ...] = ()


def _expect[T: BaseModel](response: BaseModel, expected: type[T], connector_id: str) -> T:
    if not isinstance(response, expected):
        raise ConnectorError(f"connector {connector_id!r} returned {type(response).__name__}, expected {expected.__name__}")
    return response


class ConnectorRegistry:
    def __init__(
        self,
        text_store: TextStore,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        max_age: timedelta = DEFAULT_MAX_AGE,
        approval_verifier: ApprovalVerifier | None = None,
    ) -> None:
        self._connectors: dict[str, Connector] = {}
        self._text_store = text_store
        self._timeout = timeout
        self._max_age = max_age
        self._approval_verifier = approval_verifier

    def register(self, connector: Connector) -> None:
        if connector.id in self._connectors:
            raise ValueError(f"connector {connector.id!r} already registered")
        self._connectors[connector.id] = connector

    def scoped(self, auth: TaskAuthorization, domain: str | None = None) -> list[Connector]:
        """Registered connectors the task may use. The hard platform scope (2.2)."""
        return [
            c
            for c in self._connectors.values()
            if c.id in auth.permitted_platforms and (domain is None or c.domain == domain)
        ]

    # --- guarded call --------------------------------------------------------------------

    async def _call(
        self,
        auth: TaskAuthorization,
        connector_id: str,
        capability: Capability,
        request: BaseModel,
        *,
        amount: Decimal = Decimal(0),
        currency: str | None = None,
        approvals: list[Approval] | None = None,
    ) -> BaseModel:
        connector = self._connectors.get(connector_id)
        if connector is None:
            raise ConnectorError(f"unknown connector {connector_id!r}")
        spec = next((s for s in connector.capabilities if s.name == capability.value), None)
        if spec is None:
            raise ConnectorError(f"connector {connector_id!r} does not support {capability.value!r}")
        decision = evaluate_authorization(
            auth,
            action=spec.action,
            platform=connector_id,
            amount=amount,
            currency=currency or auth.currency,
            approvals=approvals,
            approval_verifier=self._approval_verifier,
        )
        if not decision.allowed:
            raise ConnectorError(f"{capability.value} on {connector_id!r} denied: {decision.reason}")
        try:
            return await asyncio.wait_for(connector.execute(capability.value, request), self._timeout)
        except ConnectorError:
            raise
        except TimeoutError:
            raise ConnectorError(f"{capability.value} on {connector_id!r} timed out") from None
        except Exception as exc:  # connector bug or upstream failure: surface, never swallow
            logger.exception("connector %s failed on %s", connector_id, capability.value)
            raise ConnectorError(f"{capability.value} on {connector_id!r} failed: {type(exc).__name__}") from exc

    # --- normalization + text boundary ----------------------------------------------------

    async def _ingest(self, run_id: str, connector_id: str, listing: Listing) -> Candidate:
        candidate = listing.candidate
        if candidate.source != connector_id:
            raise ConnectorError(f"candidate {candidate.candidate_id} claims source {candidate.source!r}")
        validate_attributes(candidate)
        refs = candidate.text_refs
        if listing.description:
            ids = await self._text_store.save(
                run_id=run_id,
                source=connector_id,
                subject_id=candidate.candidate_id,
                records=[TextRecord(kind="listing", body=listing.description)],
            )
            refs = (*refs, *ids)
        candidate = candidate.model_copy(update={"text_refs": refs})
        return assess_quality(candidate, now=datetime.now(timezone.utc), max_age=self._max_age)

    # --- typed capabilities (text-free outputs) -------------------------------------------

    async def search_all(self, auth: TaskAuthorization, request: SearchRequest) -> DiscoveryResult:
        """Search every permitted connector for the domain. A failure never widens the scope."""
        connectors = [c for c in self.scoped(auth, request.domain) if any(s.name == "search" for s in c.capabilities)]
        outcomes = await asyncio.gather(
            *(self._call(auth, c.id, Capability.SEARCH, request) for c in connectors), return_exceptions=True
        )
        candidates: list[Candidate] = []
        errors: list[str] = []
        for connector, outcome in zip(connectors, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                errors.append(str(outcome) if isinstance(outcome, ConnectorError) else f"{connector.id}: {outcome!r}")
                continue
            try:
                response = _expect(outcome, SearchResponse, connector.id)
            except ConnectorError as exc:
                errors.append(str(exc))
                continue
            for listing in response.listings:
                try:
                    candidates.append(await self._ingest(auth.run_id, connector.id, listing))
                except ConnectorError as exc:
                    errors.append(str(exc))
        return DiscoveryResult(candidates=tuple(candidates), errors=tuple(errors))

    async def details(self, auth: TaskAuthorization, connector_id: str, request: CandidateRequest) -> Candidate:
        response = _expect(await self._call(auth, connector_id, Capability.DETAILS, request), Listing, connector_id)
        return await self._ingest(auth.run_id, connector_id, response)

    async def reviews(self, auth: TaskAuthorization, connector_id: str, request: ReviewsRequest) -> ReviewSummary:
        response = _expect(
            await self._call(auth, connector_id, Capability.REVIEWS, request), ReviewsResponse, connector_id
        )
        reviews = response.reviews
        if not reviews:
            return ReviewSummary(candidate_id=request.candidate_id)
        ids = await self._text_store.save(
            run_id=auth.run_id,
            source=connector_id,
            subject_id=request.candidate_id,
            records=[TextRecord(kind="review", body=r.text, rating=r.rating, posted_at=r.posted_at) for r in reviews],
        )
        ratings = [r.rating for r in reviews if r.rating is not None]
        dates = [r.posted_at for r in reviews if r.posted_at is not None]
        return ReviewSummary(
            candidate_id=request.candidate_id,
            text_refs=tuple(ids),
            review_count=len(reviews),
            average_rating=round(fmean(ratings), 2) if ratings else None,
            latest_review_at=max(dates) if dates else None,
        )

    async def stock(self, auth: TaskAuthorization, connector_id: str, request: CandidateRequest) -> StockResponse:
        return _expect(await self._call(auth, connector_id, Capability.STOCK, request), StockResponse, connector_id)

    async def delivery(self, auth: TaskAuthorization, connector_id: str, request: DeliveryRequest) -> DeliveryResponse:
        return _expect(
            await self._call(auth, connector_id, Capability.DELIVERY, request), DeliveryResponse, connector_id
        )

    async def checkout(
        self,
        auth: TaskAuthorization,
        connector_id: str,
        request: CheckoutRequest,
        approvals: list[Approval],
    ) -> CheckoutResponse:
        """Side-effecting: needs CREATE_ORDER permission, amount within max_spend, and any approval the grant requires."""
        response = await self._call(
            auth,
            connector_id,
            Capability.CHECKOUT,
            request,
            amount=request.amount,
            currency=request.currency,
            approvals=approvals,
        )
        return _expect(response, CheckoutResponse, connector_id)
