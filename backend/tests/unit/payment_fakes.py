"""In-memory PaymentProvider for tests. Honours idempotency keys the way PayPal does."""

from decimal import Decimal

from app.payments.provider import (
    ProviderAuthorization,
    ProviderCapture,
    ProviderError,
    ProviderOrder,
    ProviderRefund,
)


class FakeProvider:
    """In-memory provider that honours idempotency keys like PayPal does."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []
        self.seen: dict[str, object] = {}
        self.order_status = "created"
        self.order_amount: Decimal | None = None
        self.order_payee: str | None = None
        self.omit_payee = False
        self.capture_status = "completed"
        self.refund_status = "completed"
        self.fail_next: ProviderError | None = None
        self.auth_status = "created"
        self.get_authorization_fails = False
        self.amount = Decimal(0)
        self.currency = "INR"
        self.payee = "m1"

    def _once(self, name: str, request_id: str, make):
        self.calls.append((name, request_id))
        if self.fail_next:
            error, self.fail_next = self.fail_next, None
            raise error
        return self.seen.setdefault(f"{name}:{request_id}", make())

    def _order(self, authorized: bool = False) -> ProviderOrder:
        return ProviderOrder(
            order_id="ord-1", status="authorized" if authorized else self.order_status,
            amount=self.order_amount if self.order_amount is not None else self.amount, currency=self.currency,
            payee_id=None if self.omit_payee else (self.order_payee or self.payee), approval_url="https://pay/approve",
            authorization_id="auth-1" if authorized else None,
        )

    async def create_order(self, *, request_id, amount, currency, payee_id, reference):
        self.amount, self.currency, self.payee = amount, currency, payee_id
        return self._once("create", request_id, self._order)

    async def get_order(self, order_id):
        self.calls.append(("get_order", None))
        return self._order(authorized=self.order_status == "authorized")

    async def authorize_order(self, *, request_id, order_id):
        self.order_status = "authorized"
        return self._once("authorize", request_id, lambda: self._order(authorized=True))

    async def get_authorization(self, authorization_id):
        if self.get_authorization_fails:
            raise ProviderError("timeout", uncertain=True)
        return ProviderAuthorization(authorization_id=authorization_id, status=self.auth_status, amount=self.amount, currency=self.currency)

    async def capture(self, *, request_id, authorization_id, amount, currency, final_capture):
        return self._once(
            "capture", request_id,
            lambda: ProviderCapture(capture_id="cap-1", status=self.capture_status, amount=amount, currency=currency),
        )

    async def reauthorize(self, *, request_id, authorization_id, amount, currency):
        return self._once(
            "reauthorize", request_id,
            lambda: ProviderAuthorization(
                authorization_id=f"auth-re-{len(self.seen)}", status="created", amount=amount, currency=currency
            ),
        )

    async def void(self, *, request_id, authorization_id):
        self._once("void", request_id, lambda: None)

    async def refund(self, *, request_id, capture_id, amount, currency):
        return self._once(
            "refund", request_id,
            lambda: ProviderRefund(refund_id="ref-1", status=self.refund_status, amount=amount, currency=currency),
        )
