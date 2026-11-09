"""FlakyConnector: wraps any connector and injects failures, latency or stale data."""

import asyncio
from datetime import timedelta

from pydantic import BaseModel

from app.connectors.base import Connector, CapabilitySpec
from app.connectors.base.models import Listing, SearchResponse


class FlakyConnector:
    def __init__(
        self,
        inner: Connector,
        *,
        fail_capabilities: frozenset[str] = frozenset(),
        failures: int | None = None,
        delay: float = 0.0,
        stale_by: timedelta | None = None,
    ) -> None:
        """Fail ``fail_capabilities`` (the first ``failures`` calls, or always if None)."""
        self._inner = inner
        self._fail = fail_capabilities
        self._remaining = failures
        self._delay = delay
        self._stale_by = stale_by

    @property
    def id(self) -> str:
        return self._inner.id

    @property
    def domain(self) -> str:
        return self._inner.domain

    @property
    def capabilities(self) -> frozenset[CapabilitySpec]:
        return self._inner.capabilities

    @staticmethod
    def _age(listing: Listing, by: timedelta) -> Listing:
        candidate = listing.candidate
        return listing.model_copy(
            update={"candidate": candidate.model_copy(update={"fetched_at": candidate.fetched_at - by})}
        )

    async def execute(self, capability: str, request: BaseModel) -> BaseModel:
        if self._delay:
            await asyncio.sleep(self._delay)
        if capability in self._fail and (self._remaining is None or self._remaining > 0):
            if self._remaining is not None:
                self._remaining -= 1
            raise RuntimeError(f"injected failure in {self.id}.{capability}")
        response = await self._inner.execute(capability, request)
        by = self._stale_by
        if by is not None:
            if isinstance(response, SearchResponse):
                return SearchResponse(listings=tuple(self._age(item, by) for item in response.listings))
            if isinstance(response, Listing):
                return self._age(response, by)
        return response
