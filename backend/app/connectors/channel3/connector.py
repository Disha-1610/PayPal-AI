"""Channel3 connector: real product data for the ``shopping`` domain.

Channel3 offers search, product detail, aggregate ratings and per-offer stock. It has no
individual reviews, delivery dates or checkout (buy links are affiliate redirects), so this
connector declares only ``search``, ``details`` and ``stock``.
"""

import logging
from decimal import Decimal

import httpx
from pydantic import BaseModel, ValidationError

from app.connectors.base import Capability, ConnectorError, specs
from app.connectors.base.models import (
    CandidateRequest,
    Listing,
    SearchRequest,
    SearchResponse,
    StockResponse,
)
from app.connectors.channel3.schemas import Product
from app.connectors.channel3.schemas import SearchResponse as WireSearchResponse
from app.core.config import Settings
from app.schemas.candidate import Candidate

logger = logging.getLogger(__name__)

CONNECTOR_ID = "channel3"
_IN_STOCK = "InStock"
_TITLE_MAX = 200


def to_listing(product: Product) -> Listing:
    """Map a Channel3 product to the normalized Candidate (cheapest in-stock offer wins)."""
    in_stock = [o for o in product.offers if o.availability == _IN_STOCK and o.price.value is not None]
    best = min(in_stock, key=lambda o: o.price.value or Decimal(0), default=None)
    attributes: dict[str, str] = {}
    if product.brands:
        attributes["brand"] = product.brands[0].name
    if colors := product.structured_attributes.get("color"):
        attributes["color"] = colors[0]
    currency = best.price.currency if best and best.price.currency and len(best.price.currency) == 3 else None
    candidate = Candidate(
        candidate_id=product.id,
        source=CONNECTOR_ID,
        domain="shopping",
        title=product.title[:_TITLE_MAX],
        total_cost=best.price.value if best else None,
        currency=currency,
        rating=product.ratings.value if product.ratings else None,
        review_count=product.ratings.count if product.ratings else None,
        attributes=attributes,
    )
    return Listing(candidate=candidate, description=product.description)


class Channel3Connector:
    id = CONNECTOR_ID
    domain = "shopping"
    capabilities = specs(Capability.SEARCH, Capability.DETAILS, Capability.STOCK)

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.trychannel3.com",
        country: str | None = None,
        currency: str | None = None,
        timeout: float = 10.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("Channel3 API key is required (set CHANNEL3_API_KEY)")
        self._config = {k: v for k, v in {"country": country, "currency": currency}.items() if v}
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._base_url = base_url.rstrip("/")
        self._headers = {"x-api-key": api_key}

    @classmethod
    def from_settings(cls, settings: Settings) -> "Channel3Connector":
        if settings.channel3_api_key is None:
            raise ValueError("Channel3 API key is required (set CHANNEL3_API_KEY)")
        return cls(
            settings.channel3_api_key.get_secret_value(),
            base_url=settings.channel3_base_url,
            country=settings.channel3_country,
            currency=settings.channel3_currency,
            timeout=settings.channel3_timeout_seconds,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def execute(self, capability: str, request: BaseModel) -> BaseModel:
        if capability == Capability.SEARCH.value and isinstance(request, SearchRequest):
            return await self._search(request)
        if capability == Capability.DETAILS.value and isinstance(request, CandidateRequest):
            return to_listing(await self._product(request.candidate_id))
        if capability == Capability.STOCK.value and isinstance(request, CandidateRequest):
            product = await self._product(request.candidate_id)
            return StockResponse(available=any(o.availability == _IN_STOCK for o in product.offers))
        raise ConnectorError(f"{self.id}: unsupported call {capability!r}")

    # --- HTTP ------------------------------------------------------------------------------

    async def _request(self, method: str, path: str, **kwargs: object) -> httpx.Response:
        try:
            response = await self._client.request(method, f"{self._base_url}{path}", headers=self._headers, **kwargs)
        except httpx.TimeoutException:
            raise ConnectorError(f"{self.id}: request timed out") from None
        except httpx.HTTPError as exc:
            raise ConnectorError(f"{self.id}: network error ({type(exc).__name__})") from None
        if response.status_code == 401:
            raise ConnectorError(f"{self.id}: authentication failed (check CHANNEL3_API_KEY)")
        if response.status_code == 402:
            raise ConnectorError(f"{self.id}: free credits exhausted")
        if response.status_code == 404:
            raise ConnectorError(f"{self.id}: not found")
        if response.status_code == 429:
            raise ConnectorError(f"{self.id}: rate limited")
        if response.status_code >= 400:
            logger.warning("channel3 %s %s -> %s", method, path, response.status_code)
            raise ConnectorError(f"{self.id}: upstream error {response.status_code}")
        return response

    async def _search(self, request: SearchRequest) -> SearchResponse:
        body: dict[str, object] = {"query": request.query, "limit": min(request.limit, 30)}
        filters: dict[str, object] = {}
        budget = request.filters.get("budget")
        if isinstance(budget, (int, float)) and not isinstance(budget, bool):
            filters["price"] = {"max_price": budget}
        color = request.filters.get("color")
        if isinstance(color, str):
            filters["attributes"] = {"color": [color]}
        if filters:
            body["filters"] = filters
        if self._config:
            body["config"] = self._config
        response = await self._request("POST", "/v1/search", json=body)
        try:
            wire = WireSearchResponse.model_validate(response.json())
        except (ValueError, ValidationError):
            raise ConnectorError(f"{self.id}: malformed search response") from None
        return SearchResponse(listings=tuple(to_listing(p) for p in wire.products))

    async def _product(self, product_id: str) -> Product:
        response = await self._request("GET", f"/v1/products/{product_id}", params=self._config)
        try:
            return Product.model_validate(response.json())
        except (ValueError, ValidationError):
            raise ConnectorError(f"{self.id}: malformed product response") from None
