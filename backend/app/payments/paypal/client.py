"""PayPal Orders v2 / Payments v2 implementation of ``PaymentProvider`` (``intent=AUTHORIZE``).

Credentials come from settings (env). Every mutating call sends ``PayPal-Request-Id``. Network
failures and 5xx responses raise ``ProviderError(uncertain=True)``: the outcome is unknown, so the
service re-reads provider state before retrying. 4xx responses are definite refusals.
"""

import time
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from pydantic import ValidationError

from app.core.config import Settings
from app.core.logging import get_logger
from app.payments.provider import (
    ProviderAuthorization,
    ProviderCapture,
    ProviderError,
    ProviderOrder,
    ProviderRefund,
)
from app.schemas.approval_terms import minor_exponent

logger = get_logger(__name__)

_ORDER_STATUS = {"CREATED": "created", "APPROVED": "payer_approved", "COMPLETED": "authorized", "VOIDED": "voided"}
_AUTH_STATUS = {
    "CREATED": "created", "CAPTURED": "captured", "PARTIALLY_CAPTURED": "partially_captured",
    "VOIDED": "voided", "EXPIRED": "expired",
}
_CAPTURE_STATUS = {"COMPLETED": "completed", "PENDING": "pending", "DECLINED": "declined"}
_REFUND_STATUS = {"COMPLETED": "completed", "PENDING": "pending", "FAILED": "failed", "CANCELLED": "failed"}
_TOKEN_SKEW_SECONDS = 60
# PayPal returns a minimal body (id, status, links) by default; we need amount/payee/authorizations.
_FULL_BODY = {"Prefer": "return=representation"}


def format_amount(amount: Decimal, currency: str) -> str:
    """PayPal wants a decimal string with the currency's number of decimals."""
    return f"{amount:.{minor_exponent(currency)}f}"


class PayPalClient:
    def __init__(self, settings: Settings, http: httpx.AsyncClient | None = None) -> None:
        if not settings.paypal_client_id or settings.paypal_client_secret is None:
            raise ProviderError("PayPal credentials are not configured")
        self._webhook_id = settings.paypal_webhook_id
        self._client_id = settings.paypal_client_id
        self._secret = settings.paypal_client_secret
        self._http = http or httpx.AsyncClient(base_url=settings.paypal_base_url, timeout=settings.paypal_timeout_seconds)
        self._token: str | None = None
        self._token_expiry = 0.0

    async def aclose(self) -> None:
        await self._http.aclose()

    # ---- PaymentProvider ----------------------------------------------------------------------

    async def create_order(
        self, *, request_id: str, amount: Decimal, currency: str, payee_id: str, reference: str
    ) -> ProviderOrder:
        body = {
            "intent": "AUTHORIZE",
            "purchase_units": [
                {
                    "reference_id": reference,
                    "custom_id": reference,
                    "amount": {"currency_code": currency, "value": format_amount(amount, currency)},
                    "payee": {"merchant_id": payee_id},
                }
            ],
        }
        data = await self._request("POST", "/v2/checkout/orders", request_id=request_id, json=body, headers=_FULL_BODY)
        return _parse(_order, data)

    async def get_order(self, order_id: str) -> ProviderOrder:
        return _parse(_order, await self._request("GET", f"/v2/checkout/orders/{order_id}"))

    async def authorize_order(self, *, request_id: str, order_id: str) -> ProviderOrder:
        data = await self._request(
            "POST", f"/v2/checkout/orders/{order_id}/authorize", request_id=request_id, json={}, headers=_FULL_BODY
        )
        return _parse(_order, data)

    async def get_authorization(self, authorization_id: str) -> ProviderAuthorization:
        data = await self._request("GET", f"/v2/payments/authorizations/{authorization_id}")
        return _parse(
            lambda d: ProviderAuthorization(
                authorization_id=d["id"],
                status=_AUTH_STATUS.get(d.get("status", ""), "other"),
                amount=Decimal(d["amount"]["value"]),
                currency=d["amount"]["currency_code"],
            ),
            data,
        )

    async def capture(
        self, *, request_id: str, authorization_id: str, amount: Decimal, currency: str, final_capture: bool
    ) -> ProviderCapture:
        body = {"amount": {"currency_code": currency, "value": format_amount(amount, currency)}, "final_capture": final_capture}
        data = await self._request(
            "POST", f"/v2/payments/authorizations/{authorization_id}/capture", request_id=request_id, json=body
        )
        return _parse(
            lambda d: ProviderCapture(
                capture_id=d["id"],
                status=_CAPTURE_STATUS.get(d.get("status", ""), "other"),
                # Report what PayPal says it captured; fall back to the request when the body omits it.
                amount=Decimal(d["amount"]["value"]) if "amount" in d else amount,
                currency=d["amount"]["currency_code"] if "amount" in d else currency,
            ),
            data,
        )

    async def reauthorize(
        self, *, request_id: str, authorization_id: str, amount: Decimal, currency: str
    ) -> ProviderAuthorization:
        """Extend a hold past its honor period. PayPal issues a new authorization id."""
        body = {"amount": {"currency_code": currency, "value": format_amount(amount, currency)}}
        data = await self._request(
            "POST", f"/v2/payments/authorizations/{authorization_id}/reauthorize", request_id=request_id, json=body,
            headers=_FULL_BODY,
        )
        return _parse(
            lambda d: ProviderAuthorization(
                authorization_id=d["id"],
                status=_AUTH_STATUS.get(d.get("status", ""), "other"),
                amount=Decimal(d["amount"]["value"]) if "amount" in d else amount,
                currency=d["amount"]["currency_code"] if "amount" in d else currency,
            ),
            data,
        )

    async def void(self, *, request_id: str, authorization_id: str) -> None:
        await self._request("POST", f"/v2/payments/authorizations/{authorization_id}/void", request_id=request_id)

    async def refund(
        self, *, request_id: str, capture_id: str, amount: Decimal, currency: str
    ) -> ProviderRefund:
        body = {"amount": {"currency_code": currency, "value": format_amount(amount, currency)}}
        data = await self._request(
            "POST", f"/v2/payments/captures/{capture_id}/refund", request_id=request_id, json=body, headers=_FULL_BODY
        )
        return _parse(
            lambda d: ProviderRefund(
                refund_id=d["id"],
                status=_REFUND_STATUS.get(d.get("status", ""), "other"),
                amount=Decimal(d["amount"]["value"]) if "amount" in d else amount,
                currency=d["amount"]["currency_code"] if "amount" in d else currency,
            ),
            data,
        )

    # ---- webhooks -----------------------------------------------------------------------------

    async def verify_webhook(self, *, headers: dict[str, str], event: dict[str, Any]) -> bool:
        """Ask PayPal to verify a webhook's signature. A missing header or non-SUCCESS answer is False."""
        if not self._webhook_id:
            raise ProviderError("PAYPAL_WEBHOOK_ID is not configured")
        lowered = {k.lower(): v for k, v in headers.items()}
        try:
            body = {
                "auth_algo": lowered["paypal-auth-algo"],
                "cert_url": lowered["paypal-cert-url"],
                "transmission_id": lowered["paypal-transmission-id"],
                "transmission_sig": lowered["paypal-transmission-sig"],
                "transmission_time": lowered["paypal-transmission-time"],
                "webhook_id": self._webhook_id,
                "webhook_event": event,
            }
        except KeyError:
            return False  # a missing signature header is an unsigned webhook
        data = await self._request("POST", "/v1/notifications/verify-webhook-signature", json=body)
        return data.get("verification_status") == "SUCCESS"

    # ---- transport ----------------------------------------------------------------------------

    async def _access_token(self) -> str:
        if self._token and time.monotonic() < self._token_expiry:
            return self._token
        response = await self._send(
            "POST", "/v1/oauth2/token", auth=(self._client_id, self._secret.get_secret_value()),
            data={"grant_type": "client_credentials"},
        )
        payload = _json(response)
        try:
            token = str(payload["access_token"])
            expires_in = float(payload.get("expires_in", 0))
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderError("PayPal token response was malformed") from exc
        self._token, self._token_expiry = token, time.monotonic() + expires_in - _TOKEN_SKEW_SECONDS
        return token

    async def _request(
        self,
        method: str,
        path: str,
        *,
        request_id: str | None = None,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        headers = {**(headers or {}), "Authorization": f"Bearer {await self._access_token()}"}
        if request_id:
            headers["PayPal-Request-Id"] = request_id
        response = await self._send(method, path, headers=headers, json=json)
        return _json(response) if response.content else {}

    async def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = await self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:  # timeout / connection: the request may or may not have been applied
            raise ProviderError(f"PayPal {method} {path} failed: {type(exc).__name__}", uncertain=True) from exc
        if response.status_code >= 500:
            raise ProviderError(f"PayPal {method} {path} returned {response.status_code}", uncertain=True)
        if response.status_code >= 400:
            # Body is not echoed: it can contain payer details. The debug id is enough to look it up.
            debug_id = response.headers.get("PayPal-Debug-Id", "")
            logger.warning("paypal refused request", extra={"status": response.status_code, "debug_id": debug_id})
            raise ProviderError(f"PayPal {method} {path} refused with {response.status_code} (debug id {debug_id})")
        return response


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError as exc:
        raise ProviderError("PayPal returned a non-JSON body", uncertain=True) from exc
    if not isinstance(data, dict):
        raise ProviderError("PayPal returned an unexpected body", uncertain=True)
    return data


def _parse[T](build: Callable[[dict[str, Any]], T], data: dict[str, Any]) -> T:
    """Map a 2xx body to a model. A malformed body means the call may have been applied: uncertain."""
    try:
        return build(data)
    except (KeyError, IndexError, TypeError, ValueError, InvalidOperation, ValidationError) as exc:
        raise ProviderError(f"PayPal response was malformed ({type(exc).__name__})", uncertain=True) from exc


def _order(data: dict[str, Any]) -> ProviderOrder:
    unit = (data.get("purchase_units") or [{}])[0]
    amount = unit.get("amount") or {}
    authorizations = (unit.get("payments") or {}).get("authorizations") or []
    links = {link.get("rel"): link.get("href") for link in data.get("links", [])}
    return ProviderOrder(
        order_id=data["id"],
        status=_ORDER_STATUS.get(data.get("status", ""), "other"),
        amount=Decimal(amount.get("value", "0")),
        currency=amount.get("currency_code", ""),
        payee_id=(unit.get("payee") or {}).get("merchant_id"),
        reference=unit.get("reference_id"),
        approval_url=links.get("payer-action") or links.get("approve"),
        authorization_id=authorizations[0]["id"] if authorizations else None,
    )
