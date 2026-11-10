import json
from decimal import Decimal

import httpx
import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.payments.paypal.client import PayPalClient, format_amount
from app.payments.provider import ProviderError

ORDER = {
    "id": "5O190127TN364715T",
    "status": "CREATED",
    "purchase_units": [
        {"reference_id": "pay-1", "amount": {"currency_code": "INR", "value": "5200.00"}, "payee": {"merchant_id": "M1"}}
    ],
    "links": [{"rel": "payer-action", "href": "https://paypal.test/approve"}],
}


def client(handler) -> tuple[PayPalClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        return handler(request)

    settings = Settings(paypal_client_id="id", paypal_client_secret=SecretStr("secret"))
    http = httpx.AsyncClient(transport=httpx.MockTransport(wrapped), base_url="https://paypal.test")
    return PayPalClient(settings, http), seen


def test_requires_credentials():
    with pytest.raises(ProviderError, match="credentials"):
        PayPalClient(Settings(paypal_client_id=None, paypal_client_secret=None))


def test_amount_format_follows_currency_exponent():
    assert format_amount(Decimal(5200), "INR") == "5200.00"
    assert format_amount(Decimal(500), "JPY") == "500"


async def test_create_order_sends_authorize_intent_idempotency_key_and_payee():
    paypal, seen = client(lambda r: httpx.Response(201, json=ORDER))
    order = await paypal.create_order(request_id="k1", amount=Decimal(5200), currency="INR", payee_id="M1", reference="pay-1")
    request = seen[-1]
    body = json.loads(request.content)
    assert request.headers["PayPal-Request-Id"] == "k1" and request.headers["Authorization"] == "Bearer tok"
    assert body["intent"] == "AUTHORIZE"
    assert body["purchase_units"][0]["amount"] == {"currency_code": "INR", "value": "5200.00"}
    assert body["purchase_units"][0]["payee"] == {"merchant_id": "M1"}
    assert (order.status, order.amount, order.payee_id, order.approval_url) == (
        "created", Decimal("5200.00"), "M1", "https://paypal.test/approve",
    )


async def test_token_is_cached():
    paypal, seen = client(lambda r: httpx.Response(200, json=ORDER))
    await paypal.get_order("o1")
    await paypal.get_order("o1")
    assert sum(r.url.path == "/v1/oauth2/token" for r in seen) == 1


async def test_authorized_order_exposes_authorization_id():
    body = {**ORDER, "status": "COMPLETED"}
    body["purchase_units"] = [{**ORDER["purchase_units"][0], "payments": {"authorizations": [{"id": "AUTH-1"}]}}]
    paypal, _ = client(lambda r: httpx.Response(201, json=body))
    order = await paypal.authorize_order(request_id="k2", order_id="o1")
    assert (order.status, order.authorization_id) == ("authorized", "AUTH-1")


async def test_capture_is_final_and_void_handles_empty_body():
    paypal, seen = client(lambda r: httpx.Response(204) if r.url.path.endswith("/void") else httpx.Response(201, json={"id": "CAP-1", "status": "COMPLETED"}))
    capture = await paypal.capture(request_id="k3", authorization_id="AUTH-1", amount=Decimal(5000), currency="INR", final_capture=True)
    assert (capture.status, capture.amount) == ("completed", Decimal(5000))
    assert json.loads(seen[-1].content) == {"amount": {"currency_code": "INR", "value": "5000.00"}, "final_capture": True}
    await paypal.void(request_id="k4", authorization_id="AUTH-1")
    assert seen[-1].headers["PayPal-Request-Id"] == "k4"


async def test_5xx_and_network_errors_are_uncertain_4xx_are_definite():
    paypal, _ = client(lambda r: httpx.Response(503))
    with pytest.raises(ProviderError) as info:
        await paypal.get_order("o1")
    assert info.value.uncertain

    def boom(request):
        raise httpx.ConnectTimeout("slow")

    paypal, _ = client(boom)
    with pytest.raises(ProviderError) as info:
        await paypal.get_order("o1")
    assert info.value.uncertain

    paypal, _ = client(lambda r: httpx.Response(422, json={"details": "payer@example.com"}, headers={"PayPal-Debug-Id": "dbg"}))
    with pytest.raises(ProviderError) as info:
        await paypal.get_order("o1")
    assert not info.value.uncertain and "dbg" in str(info.value) and "payer@example.com" not in str(info.value)


async def test_create_and_authorize_ask_for_the_full_body():
    paypal, seen = client(lambda r: httpx.Response(201, json=ORDER))
    await paypal.create_order(request_id="k1", amount=Decimal(5200), currency="INR", payee_id="M1", reference="pay-1")
    assert seen[-1].headers["Prefer"] == "return=representation"
    await paypal.authorize_order(request_id="k2", order_id="o1")
    assert seen[-1].headers["Prefer"] == "return=representation"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={"status": "CREATED"}),  # no id
        httpx.Response(200, json=[1, 2]),  # not an object
        httpx.Response(200, content=b"<html>oops</html>"),  # not JSON
        httpx.Response(200, json={"id": "o", "purchase_units": [{"amount": {"value": "abc", "currency_code": "INR"}}]}),
    ],
)
async def test_malformed_success_bodies_become_uncertain_provider_errors(response):
    paypal, _ = client(lambda r: response)
    with pytest.raises(ProviderError) as info:
        await paypal.get_order("o1")
    assert info.value.uncertain


async def test_capture_reports_the_amount_paypal_returned():
    paypal, _ = client(
        lambda r: httpx.Response(201, json={"id": "CAP", "status": "COMPLETED", "amount": {"value": "4999.00", "currency_code": "INR"}})
    )
    capture = await paypal.capture(request_id="k", authorization_id="A", amount=Decimal(5000), currency="INR", final_capture=True)
    assert capture.amount == Decimal("4999.00")


async def test_reauthorize_posts_amount_with_idempotency_key_and_returns_the_new_id():
    paypal, seen = client(lambda r: httpx.Response(201, json={"id": "AUTH-2", "status": "CREATED"}))
    renewed = await paypal.reauthorize(request_id="k5", authorization_id="AUTH-1", amount=Decimal(5200), currency="INR")
    request = seen[-1]
    assert request.url.path == "/v2/payments/authorizations/AUTH-1/reauthorize"
    assert request.headers["PayPal-Request-Id"] == "k5"
    assert json.loads(request.content) == {"amount": {"currency_code": "INR", "value": "5200.00"}}
    assert (renewed.authorization_id, renewed.status, renewed.amount) == ("AUTH-2", "created", Decimal(5200))
