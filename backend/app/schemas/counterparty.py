"""The counterparty the user names for a contractor or A2A run, and the quote it sends back.

Contact details are user input and quotes are counterparty input: both are validated into strict models here and
nothing else is kept. A quote becomes the run's single ``Candidate`` (``quote_candidate``), so the rest of the graph
(evaluate, recheck, approval, payment) treats it like any discovered option.
"""

import hashlib
import ipaddress
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.approval_terms import from_minor_units, to_minor_units
from app.schemas.candidate import Candidate

USER_SUPPLIED_SOURCE = "user_supplied"  # Candidate.source of a quoted counterparty
QUOTE_ID_PREFIX = "q-"  # Candidate.candidate_id prefix of a quote
Channel = Literal["email", "sms", "whatsapp"]

_EMAIL = re.compile(r"^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,24}$")
_PHONE = re.compile(r"^\+[1-9]\d{7,14}$")  # E.164
_EMAIL_IN_TEXT = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,24}")
_PHONE_IN_TEXT = re.compile(r"\+[1-9][\d\s().-]{7,20}\d")
_URL_IN_TEXT = re.compile(r"https://[^\s<>\"']{3,2000}")


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def is_public_https_url(url: str) -> bool:
    """https with a host that is not a literal private, loopback, link-local or reserved address. Hostnames are
    re-checked against their resolved addresses at send time (``counterparty.netguard``)."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        return False
    host = parts.hostname.lower()
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return "." in host
    return address.is_global


def normalize_phone(raw: str) -> str:
    return re.sub(r"[\s().-]", "", raw)


class CounterpartyContact(BaseModel):
    """Who the agent negotiates with. A contractor needs an email or phone; an agent needs its HTTPS endpoint."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=120)
    website: str | None = Field(default=None, max_length=2048)
    email: str | None = Field(default=None, max_length=254)
    phone: str | None = Field(default=None, max_length=24)
    channels: tuple[Channel, ...] = ()  # how to reach the phone/email; empty: every channel the details allow
    agent_endpoint: str | None = Field(default=None, max_length=2048)

    @field_validator("name")
    @classmethod
    def _clean_name(cls, value: str) -> str:
        cleaned = " ".join(value.split())
        if not cleaned or any(ch in cleaned for ch in "<>\r\n"):
            raise ValueError("name must be plain text")
        return cleaned

    @field_validator("email")
    @classmethod
    def _check_email(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        value = value.strip()
        if not _EMAIL.match(value):
            raise ValueError("email is not a valid address")
        return value.lower()

    @field_validator("phone")
    @classmethod
    def _check_phone(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        value = normalize_phone(value.strip())
        if not _PHONE.match(value):
            raise ValueError("phone must be in international format, e.g. +14155550123")
        return value

    @field_validator("website", "agent_endpoint")
    @classmethod
    def _check_url(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        value = value.strip()
        if not is_public_https_url(value):
            raise ValueError("must be a public https:// URL")
        return value

    @model_validator(mode="after")
    def _channels_match_details(self) -> Self:
        if "email" in self.channels and self.email is None:
            raise ValueError("the email channel needs an email address")
        if {"sms", "whatsapp"} & set(self.channels) and self.phone is None:
            raise ValueError("sms and whatsapp need a phone number")
        return self

    @property
    def reachable_channels(self) -> tuple[Channel, ...]:
        if self.channels:
            return self.channels
        found: list[Channel] = []
        if self.email:
            found.append("email")
        if self.phone:
            found.append("sms")
        return tuple(found)

    @property
    def counterparty_id(self) -> str:
        """Stable id for this counterparty (offers and terms carry it, never the raw contact details)."""
        key = "|".join(str(v or "") for v in (self.agent_endpoint, self.email, self.phone, self.website, self.name.lower()))
        return "cp-" + hashlib.sha256(key.encode()).hexdigest()[:24]

    def describe(self) -> str:
        """One line for the intent text and the chat: the name and the ways it is reached (no secrets)."""
        ways = [w for w in (self.email, self.phone, self.website, self.agent_endpoint) if w]
        return f"{self.name} ({', '.join(ways)})" if ways else self.name

    def require_for(self, domain: str) -> None:
        if domain == "a2a" and self.agent_endpoint is None:
            raise ValueError("an agent-to-agent run needs the other agent's https endpoint")
        if domain == "contractor" and self.email is None and self.phone is None:
            raise ValueError("a contractor run needs the contractor's email or phone number")


def contact_from_text(text: str, *, name: str | None = None) -> CounterpartyContact | None:
    """A new counterparty from a chat reply ("try bob@example.com instead"): the first email, phone and https URL
    in the text. None when the text holds no way to reach anyone."""
    email = _EMAIL_IN_TEXT.search(text)
    phone = _PHONE_IN_TEXT.search(text)
    url = _URL_IN_TEXT.search(text)
    if not (email or phone or url):
        return None
    if name:
        label = name
    elif email:
        label = email.group(0).split("@")[0]
    elif phone:
        label = phone.group(0)
    else:
        label = (urlsplit(url.group(0)).hostname if url else None) or "counterparty"
    try:
        return CounterpartyContact(
            name=label[:120], email=email.group(0) if email else None,
            phone=normalize_phone(phone.group(0)) if phone else None,
            website=url.group(0) if url else None,
        )
    except ValueError:
        return None


class Quote(BaseModel):
    """The counterparty's quote for the requested work, sanitized: a price, a delivery date and nothing else."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    price: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    currency: str = Field(min_length=3, max_length=3)
    delivery_due: datetime

    _utc = field_validator("delivery_due")(_aware)

    @field_validator("currency")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.upper()

    @property
    def price_minor(self) -> int:
        return to_minor_units(self.price, self.currency)


def parse_quote(raw: Mapping[str, object]) -> Quote | None:
    """A quote from a reply payload; None for a decline. ``ValidationError`` rejects a malformed quote as a whole.
    Accepts ``price`` (decimal string/number) or ``price_minor`` (int), as both portal and agent replies send."""
    if raw.get("decision") == "decline":
        return None
    fields: dict[str, object] = {k: raw[k] for k in ("price", "currency", "delivery_due") if k in raw}
    minor, currency = raw.get("price_minor"), raw.get("currency")
    if "price" not in fields and isinstance(minor, int) and not isinstance(minor, bool) and isinstance(currency, str):
        fields["price"] = from_minor_units(minor, currency.upper())
    return Quote.model_validate(fields)


def quote_candidate(contact: CounterpartyContact, quote: Quote, *, run_id: str, domain: str, goal: str) -> Candidate:
    """The quoted counterparty as the run's one candidate. The id names the run, so it cannot be replayed elsewhere."""
    return Candidate(
        candidate_id=f"{QUOTE_ID_PREFIX}{run_id[:24]}-{contact.counterparty_id[3:15]}", source=USER_SUPPLIED_SOURCE, domain=domain,
        title=f"{contact.name}: {goal}"[:200], total_cost=quote.price, currency=quote.currency,
        available_by=quote.delivery_due, product_url=contact.website,
        attributes={"counterparty": contact.name, "counterparty_id": contact.counterparty_id},
    )


__all__ = [
    "QUOTE_ID_PREFIX", "USER_SUPPLIED_SOURCE", "Channel", "CounterpartyContact", "Quote", "contact_from_text", "is_public_https_url",
    "normalize_phone", "parse_quote", "quote_candidate",
]
