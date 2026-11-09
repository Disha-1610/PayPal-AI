"""Connector contract: what any source (store, marketplace, contractor board, ...) must provide.

A connector is domain-agnostic. It declares a set of capabilities and answers
``execute(capability, request)``. Authorization, scoping, text storage and data-quality
rules are enforced by the registry, never by individual connectors.
"""

from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from app.schemas.authorization import Action


class Capability(StrEnum):
    """Standard capability ids. Connectors may also declare custom string ids."""

    SEARCH = "search"
    DETAILS = "details"
    REVIEWS = "reviews"
    STOCK = "stock"
    DELIVERY = "delivery"
    CHECKOUT = "checkout"


class CapabilitySpec(BaseModel):
    """One capability and the authorization action it requires."""

    model_config = ConfigDict(frozen=True)

    name: str
    action: Action
    side_effects: bool = False


STANDARD_SPECS: dict[Capability, CapabilitySpec] = {
    Capability.SEARCH: CapabilitySpec(name="search", action=Action.SEARCH),
    Capability.DETAILS: CapabilitySpec(name="details", action=Action.SEARCH),
    Capability.REVIEWS: CapabilitySpec(name="reviews", action=Action.SEARCH),
    Capability.STOCK: CapabilitySpec(name="stock", action=Action.SEARCH),
    Capability.DELIVERY: CapabilitySpec(name="delivery", action=Action.SEARCH),
    Capability.CHECKOUT: CapabilitySpec(name="checkout", action=Action.CREATE_ORDER, side_effects=True),
}


def specs(*capabilities: Capability) -> frozenset[CapabilitySpec]:
    return frozenset(STANDARD_SPECS[c] for c in capabilities)


class ConnectorError(Exception):
    """A connector call failed or was refused. Never triggers fallback to another platform."""


@runtime_checkable
class Connector(Protocol):
    id: str
    domain: str
    capabilities: frozenset[CapabilitySpec]

    async def execute(self, capability: str, request: BaseModel) -> BaseModel: ...


class TextRecord(BaseModel):
    """One piece of untrusted text to persist: a review or a listing description."""

    model_config = ConfigDict(frozen=True)

    kind: str  # "review" | "listing"
    body: str
    rating: float | None = None
    posted_at: datetime | None = None


class TextStore(Protocol):
    """Persists untrusted free text and returns database ids in order (rule 2.5)."""

    async def save(self, *, run_id: str, source: str, subject_id: str, records: list[TextRecord]) -> list[str]: ...
