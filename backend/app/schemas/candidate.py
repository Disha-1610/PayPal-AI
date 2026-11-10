"""Candidate: one normalized option found by a connector, in any domain.

Universal core + domain ``attributes`` validated against the domain plug-in's fields
(see ``connectors.normalize``). Free text (reviews, descriptions) is never held here:
``text_refs`` carries database ids only (untrusted-data boundary, rule 2.5).
"""

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

AttributeValue = str | int | float | bool | list[str]


class DataQuality(StrEnum):
    MISSING = "missing"  # information was not provided
    STALE = "stale"  # information may be outdated
    UNRELIABLE = "unreliable"  # information cannot be trusted sufficiently


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Candidate(BaseModel):
    model_config = ConfigDict(frozen=True)

    candidate_id: str = Field(min_length=1)
    source: str = Field(min_length=1, description="Connector id that produced this candidate")
    domain: str = Field(min_length=1)
    title: str = Field(max_length=200)

    total_cost: Decimal | None = Field(default=None, ge=0, description="Price plus mandatory fees")
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    rating: float | None = Field(default=None, ge=0, le=5)
    review_count: int | None = Field(default=None, ge=0)
    first_review_at: datetime | None = Field(default=None, description="Earliest review date (burst detection)")
    latest_review_at: datetime | None = None
    complaint_count: int | None = Field(default=None, ge=0)
    return_window_days: int | None = Field(default=None, ge=0)
    available_by: datetime | None = Field(default=None, description="Delivery / service start date")

    attributes: dict[str, AttributeValue] = Field(default_factory=dict)
    quality: dict[str, DataQuality] = Field(default_factory=dict, description="field name -> quality flag")
    text_refs: tuple[str, ...] = ()
    fetched_at: datetime = Field(default_factory=_utcnow)

    @model_validator(mode="after")
    def _check_review_dates(self) -> Self:
        if self.first_review_at and self.latest_review_at and self.first_review_at > self.latest_review_at:
            raise ValueError("first_review_at must not be after latest_review_at")
        return self
