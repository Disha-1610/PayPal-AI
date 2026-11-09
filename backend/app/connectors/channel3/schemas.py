"""Subset of the Channel3 v1 response we depend on. Unknown fields are ignored; shape is validated."""

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class _Wire(BaseModel):
    model_config = ConfigDict(extra="ignore")


class Brand(_Wire):
    name: str


class Price(_Wire):
    # The schema says `price`; one guide example shows `amount`. Accept both.
    price: Decimal | None = None
    amount: Decimal | None = None
    currency: str | None = None

    @property
    def value(self) -> Decimal | None:
        return self.price if self.price is not None else self.amount


class Offer(_Wire):
    domain: str
    price: Price
    availability: str


class Ratings(_Wire):
    value: float = Field(ge=0, le=5)
    count: int = Field(ge=0)


class Product(_Wire):
    id: str
    title: str
    description: str | None = None
    brands: list[Brand] = Field(default_factory=list)
    offers: list[Offer] = Field(default_factory=list)
    ratings: Ratings | None = None
    structured_attributes: dict[str, list[str]] = Field(default_factory=dict)


class SearchResponse(_Wire):
    products: list[Product]
