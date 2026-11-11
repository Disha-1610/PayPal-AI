import pytest

from app.orchestrator.router.executor import ExecutorRoute, route_execution
from app.orchestrator.router.intent import IntentRoute, route_intent
from app.orchestrator.router.payment import PaymentRoute, route_payment_error
from app.payments.service import PaymentMismatch, PaymentNotAuthorized, PaymentNotReady
from app.schemas.intent import IntentResult, IntentStatus
from app.schemas.recheck import MismatchAction, MismatchReport


@pytest.mark.parametrize(
    ("status", "route"),
    [
        (IntentStatus.READY, IntentRoute.DISCOVERY),
        (IntentStatus.BLOCKED, IntentRoute.ASK_USER),
        (IntentStatus.OUT_OF_SCOPE, IntentRoute.REPORT),
    ],
)
def test_intent_routes(status, route):
    assert route_intent(IntentResult(status=status, goal="g", constraints=())) is route


@pytest.mark.parametrize(
    ("action", "route"),
    [
        (MismatchAction.PROCEED, ExecutorRoute.AUTHORIZE),
        (MismatchAction.INSUFFICIENT_DATA, ExecutorRoute.ASK_USER),
        (MismatchAction.DEADLINE_MISSED, ExecutorRoute.ADAPT),
        (MismatchAction.REAPPROVE, ExecutorRoute.ASK_USER),
        (MismatchAction.UNAVAILABLE, ExecutorRoute.EVALUATE),
    ],
)
def test_executor_routes_cover_every_mismatch_action(action, route):
    assert route_execution(MismatchReport(action=action)) is route
    assert set(MismatchAction) == {a for a in MismatchAction if route_execution(MismatchReport(action=a))}


def test_payment_routes():
    assert route_payment_error(PaymentNotReady("wait")) is PaymentRoute.WAIT
    assert route_payment_error(PaymentNotAuthorized("no", requires_approval=True)) is PaymentRoute.ASK_USER
    assert route_payment_error(PaymentMismatch("differs")) is PaymentRoute.ASK_USER
