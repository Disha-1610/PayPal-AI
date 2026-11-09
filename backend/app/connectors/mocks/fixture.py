"""FixtureConnector: an in-memory connector serving fixed data for any domain.

The mock platforms are instances of this one class, which also shows that a connector
is just "declare capabilities + map data to Candidate".
"""

from datetime import datetime, timezone

from pydantic import BaseModel

from app.connectors.base import Capability, CapabilitySpec, ConnectorError
from app.connectors.base.models import (
    CandidateRequest,
    CheckoutRequest,
    CheckoutResponse,
    DeliveryRequest,
    DeliveryResponse,
    Listing,
    ReviewsRequest,
    ReviewsResponse,
    ReviewText,
    SearchRequest,
    SearchResponse,
    StockResponse,
)


class FixtureConnector:
    def __init__(
        self,
        *,
        id: str,
        domain: str,
        capabilities: frozenset[CapabilitySpec],
        listings: tuple[Listing, ...],
        reviews: dict[str, tuple[ReviewText, ...]] | None = None,
        out_of_stock: frozenset[str] = frozenset(),
    ) -> None:
        self.id = id
        self.domain = domain
        self.capabilities = capabilities
        self._listings = {item.candidate.candidate_id: item for item in listings}
        self._reviews = reviews or {}
        self._out_of_stock = out_of_stock

    def _fresh(self, listing: Listing) -> Listing:
        candidate = listing.candidate.model_copy(update={"fetched_at": datetime.now(timezone.utc)})
        return listing.model_copy(update={"candidate": candidate})

    def _listing(self, candidate_id: str) -> Listing:
        try:
            return self._fresh(self._listings[candidate_id])
        except KeyError:
            raise ConnectorError(f"{self.id}: unknown candidate {candidate_id!r}") from None

    async def execute(self, capability: str, request: BaseModel) -> BaseModel:
        match capability, request:
            case Capability.SEARCH.value, SearchRequest():
                words = request.query.lower().split()
                hits = [
                    self._fresh(item)
                    for item in self._listings.values()
                    if item.candidate.domain == request.domain
                    and any(w in item.candidate.title.lower() for w in words)
                ]
                return SearchResponse(listings=tuple(hits[: request.limit]))
            case Capability.DETAILS.value, CandidateRequest():
                return self._listing(request.candidate_id)
            case Capability.REVIEWS.value, ReviewsRequest():
                self._listing(request.candidate_id)
                return ReviewsResponse(reviews=self._reviews.get(request.candidate_id, ())[: request.limit])
            case Capability.STOCK.value, CandidateRequest():
                self._listing(request.candidate_id)
                return StockResponse(available=request.candidate_id not in self._out_of_stock)
            case Capability.DELIVERY.value, DeliveryRequest():
                return DeliveryResponse(available_by=self._listing(request.candidate_id).candidate.available_by)
            case Capability.CHECKOUT.value, CheckoutRequest():
                self._listing(request.candidate_id)
                return CheckoutResponse(order_ref=f"{self.id}-order-{request.candidate_id}", status="created")
        raise ConnectorError(f"{self.id}: unsupported call {capability!r} with {type(request).__name__}")
