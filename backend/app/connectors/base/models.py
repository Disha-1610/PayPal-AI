"""Request/response models for the standard connector capabilities.

``Listing`` and ``ReviewText`` carry untrusted free text. They are what a *connector* returns;
the registry persists that text and hands callers text-free results (``connectors.registry``).
"""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.candidate import AttributeValue, Candidate


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True)


class SearchRequest(_Model):
    query: str = Field(min_length=1)
    domain: str
    filters: dict[str, AttributeValue] = Field(default_factory=dict)
    limit: int = Field(default=10, ge=1, le=50)
    currency: str | None = Field(default=None, min_length=3, max_length=3, description="The run's currency, for sources that price in several")


class CandidateRequest(_Model):
    candidate_id: str


class ReviewsRequest(CandidateRequest):
    limit: int = Field(default=20, ge=1, le=100)


class DeliveryRequest(CandidateRequest):
    destination: str


class CheckoutRequest(CandidateRequest):
    amount: Decimal = Field(ge=0)
    currency: str = Field(min_length=3, max_length=3)
    quantity: int = Field(default=1, ge=1)


class Listing(_Model):
    candidate: Candidate
    description: str | None = Field(default=None, description="UNTRUSTED seller text")


class SearchResponse(_Model):
    listings: tuple[Listing, ...] = ()


class ReviewText(_Model):
    rating: float | None = Field(default=None, ge=0, le=5)
    text: str = Field(description="UNTRUSTED customer text")
    posted_at: datetime | None = None


class ReviewsResponse(_Model):
    reviews: tuple[ReviewText, ...] = ()


class StockResponse(_Model):
    available: bool
    quantity: int | None = Field(default=None, ge=0)


class DeliveryResponse(_Model):
    available_by: datetime | None = None
    fee: Decimal = Field(default=Decimal(0), ge=0)


class CheckoutResponse(_Model):
    order_ref: str
    status: str
