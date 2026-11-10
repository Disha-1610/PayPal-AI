"""Payment provider contract. The service talks only to this; PayPal is one implementation.

Every mutating call takes a ``request_id`` (idempotency key). Providers MUST send it so a retried
call returns the original result instead of repeating the operation.
"""

from decimal import Decimal
from typing import Protocol

from pydantic import BaseModel, ConfigDict


class ProviderError(Exception):
    """A provider call failed. ``uncertain`` means the outcome is unknown (timeout, 5xx):
    the caller must re-read the provider's state before retrying or giving up."""

    def __init__(self, message: str, *, uncertain: bool = False) -> None:
        super().__init__(message)
        self.uncertain = uncertain


class ProviderOrder(BaseModel):
    model_config = ConfigDict(frozen=True)

    order_id: str
    status: str  # normalised: created | payer_approved | authorized | voided | other
    amount: Decimal
    currency: str
    payee_id: str | None = None
    reference: str | None = None
    approval_url: str | None = None  # where the payer approves, when still pending
    authorization_id: str | None = None  # set once the order has been authorized


class ProviderAuthorization(BaseModel):
    model_config = ConfigDict(frozen=True)

    authorization_id: str
    status: str  # normalised: created | captured | partially_captured | voided | expired | other
    amount: Decimal
    currency: str


class ProviderCapture(BaseModel):
    model_config = ConfigDict(frozen=True)

    capture_id: str
    status: str  # normalised: completed | pending | declined | other
    amount: Decimal
    currency: str


class ProviderRefund(BaseModel):
    model_config = ConfigDict(frozen=True)

    refund_id: str
    status: str  # normalised: completed | pending | failed | other
    amount: Decimal
    currency: str


class PaymentProvider(Protocol):
    async def create_order(
        self, *, request_id: str, amount: Decimal, currency: str, payee_id: str, reference: str
    ) -> ProviderOrder: ...

    async def get_order(self, order_id: str) -> ProviderOrder: ...

    async def authorize_order(self, *, request_id: str, order_id: str) -> ProviderOrder: ...

    async def get_authorization(self, authorization_id: str) -> ProviderAuthorization: ...

    async def capture(
        self, *, request_id: str, authorization_id: str, amount: Decimal, currency: str, final_capture: bool
    ) -> ProviderCapture: ...

    async def reauthorize(
        self, *, request_id: str, authorization_id: str, amount: Decimal, currency: str
    ) -> ProviderAuthorization: ...  # returns the NEW authorization (new id)

    async def void(self, *, request_id: str, authorization_id: str) -> None: ...

    async def refund(
        self, *, request_id: str, capture_id: str, amount: Decimal, currency: str
    ) -> ProviderRefund: ...
