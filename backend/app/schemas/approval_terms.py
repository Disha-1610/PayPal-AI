"""Domain-independent description of the exact agreement a user approves.

``ApprovalTerms`` is the *only* thing that gets hashed and signed. Shopping, contractor, A2A and
commercial flows all fill the same structure; domain plug-ins put their extra material fields in
``domain_data`` (hashed). Everything is hashed by default: only ``display`` (presentation text) is
excluded, so a forgotten field fails safe instead of silently escaping the approval.
"""

import unicodedata
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.hashing import stable_hash

Scalar = str | int | float | bool | None
# Currencies with no minor unit; everything else is treated as 2 decimals.
_ZERO_DECIMAL = frozenset({"JPY", "KRW", "VND", "CLP", "ISK", "UGX"})


def minor_exponent(currency: str) -> int:
    return 0 if currency.upper() in _ZERO_DECIMAL else 2


def to_minor_units(amount: Decimal, currency: str) -> int:
    """Convert a money amount to integer minor units (paise, cents). Never use floats for money."""
    return int((amount * (10 ** minor_exponent(currency))).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


def from_minor_units(minor: int, currency: str) -> Decimal:
    return Decimal(minor) / (10 ** minor_exponent(currency))


def _normalize_text(value: str) -> str:
    return unicodedata.normalize("NFC", value).strip()


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("datetimes in approval terms must be timezone-aware")
    return value.astimezone(UTC).isoformat()


class ApprovalTerms(BaseModel):
    """What is being bought / agreed / delegated, between whom, for how much, by when."""

    model_config = ConfigDict(frozen=True)

    subject_id: str = Field(min_length=1, description="Product, work order, task or agreement id")
    subject_attributes: dict[str, Scalar] = Field(default_factory=dict, description="Variant, scope, spec ...")
    parties: dict[str, str] = Field(description="Role -> identity, e.g. buyer_id, payee_id, counterparty_id")
    quantity: int = Field(default=1, ge=1)
    currency: str = Field(min_length=3, max_length=3)
    max_total_minor: int = Field(ge=0, description="Highest total the user accepts, in minor units")
    expected_total_minor: int | None = Field(
        default=None, ge=0, description="Total shown to the user; lets a suspiciously large drop be detected"
    )
    latest_acceptable_at: datetime | None = Field(
        default=None, description="Hard deadline (delivery, completion). NOT the connector's estimate"
    )
    permissions: frozenset[str] = Field(default=frozenset(), description="Actions the executor may take")
    constraints: dict[str, Scalar] = Field(default_factory=dict, description="Conditions that must stay true")
    domain_data: dict[str, Scalar] = Field(default_factory=dict, description="Plug-in material fields (hashed)")
    display: dict[str, str] = Field(default_factory=dict, description="Presentation only; NOT hashed")

    @field_validator("latest_acceptable_at")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("latest_acceptable_at must be timezone-aware")
        return value

    @field_validator("currency")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.upper()

    def canonical(self) -> dict[str, Any]:
        """Deterministic, hash-ready form: sorted, normalized, no floats for money, no ``display``."""

        def clean(value: Scalar) -> Scalar:
            return _normalize_text(value) if isinstance(value, str) else value

        return {
            "subject_id": _normalize_text(self.subject_id),
            "subject_attributes": {_normalize_text(k): clean(v) for k, v in sorted(self.subject_attributes.items())},
            "parties": {_normalize_text(k): _normalize_text(v) for k, v in sorted(self.parties.items())},
            "quantity": self.quantity,
            "currency": self.currency,
            "max_total_minor": self.max_total_minor,
            "expected_total_minor": self.expected_total_minor,
            "latest_acceptable_at": _utc_iso(self.latest_acceptable_at) if self.latest_acceptable_at else None,
            "permissions": sorted(self.permissions),
            "constraints": {_normalize_text(k): clean(v) for k, v in sorted(self.constraints.items())},
            "domain_data": {_normalize_text(k): clean(v) for k, v in sorted(self.domain_data.items())},
        }

    def terms_hash(self) -> str:
        return stable_hash(self.canonical())
