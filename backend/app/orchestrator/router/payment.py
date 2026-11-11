"""Deterministic routing after a refused or failed payment operation.

A timeout is not a failure: the Payment Service already GETs the provider status before retrying, so what
reaches the Router is only a definite refusal. ``PaymentNotReady`` means wait; everything else needs the user.
"""

from enum import StrEnum

from app.payments.service import PaymentNotReady, PaymentServiceError


class PaymentRoute(StrEnum):
    WAIT = "wait"  # payer/provider not ready: retry later (webhook or sweep)
    ASK_USER = "ask_user"  # refused: fresh approval, different payment method, or stop


def route_payment_error(error: PaymentServiceError) -> PaymentRoute:
    return PaymentRoute.WAIT if isinstance(error, PaymentNotReady) else PaymentRoute.ASK_USER
