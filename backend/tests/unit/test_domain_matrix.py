"""Domain switching and regression matrix: one contract, run against shopping, contractor and A2A.

Everything here is parametrized over the registered plug-ins, so a new domain (or a change that
breaks one) fails the same checks as the others. Domain-specific behaviour has its own tests; this
file pins what must be *identical* across domains: plan safety, intent field scoping, monitor rules,
payment gating, audit and discovery scope.
"""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.audit.events import AuditScope
from app.domains import get_plugin
from app.domains.base import PAYMENT_RULES, DomainPlugin
from app.orchestrator.agents.coordinator.plan import build_plan
from app.orchestrator.agents.intent.agent import IntentError, extract_intent
from app.orchestrator.executor.executor import ExecutionBlocked, Executor
from app.orchestrator.executor.state import ExecutionState
from app.orchestrator.guard.approval_guard import issue_approval, signed_approval_verifier
from app.orchestrator.router.monitor import route_monitor_result
from app.payments.service import PaymentPermit, PaymentService
from app.schemas.approval_terms import ApprovalTerms
from app.schemas.authorization import Action, Approval, TaskAuthorization
from app.schemas.intent import IntentStatus
from app.schemas.monitor import EventOutcome, Route, TaskView
from app.schemas.plan import Plan
from app.schemas.recheck import MismatchAction, ObservedTerms
from app.schemas.verification import Evidence, VerificationSpec
from conftest import FakeProvider as FakeLLM
from tests.unit.payment_fakes import FakeProvider
from tests.unit.test_connectors import SHOES, make_auth, make_registry, store  # noqa: F401  (fixture)
from tests.unit.test_monitor import (  # noqa: F401  (fixtures + helpers)
    NOW,
    capture_payment,
    constraint,
    event,
    factory,
    run,
)

DOMAINS = ["shopping", "contractor", "a2a"]
PLUGINS = [get_plugin(name) for name in DOMAINS]
KEY = b"k" * 32
DEADLINE = datetime(2026, 11, 20, tzinfo=UTC)

# Each domain's monitored entity and a task view matching its rules (deadline constraint where it has one).
VIEWS = {
    "shopping": (TaskView(run_id="r1", domain="shopping", constraints=(constraint("delivery_days", 5),),
                          anchor_at=datetime(2026, 11, 10, tzinfo=UTC)), "order-1"),
    "contractor": (TaskView(run_id="r1", domain="contractor", expected_entities=frozenset({"m1"})), "m1"),
    "a2a": (TaskView(run_id="r1", domain="a2a", constraints=(constraint("deadline", "2026-11-15T00:00:00Z"),)), "t1"),
}


def by_name(plugin: DomainPlugin) -> str:
    return plugin.name


# ---- plug-in contract -------------------------------------------------------------------------


def test_the_registry_serves_exactly_the_three_domains_and_rejects_others():
    assert [get_plugin(n).name for n in DOMAINS] == DOMAINS
    with pytest.raises(ValueError, match="unknown domain"):
        get_plugin("travel")


@pytest.mark.parametrize("plugin", PLUGINS, ids=by_name)
def test_plugin_declarations_are_internally_consistent(plugin):
    names = [f.name for f in plugin.fields]
    assert len(names) == len(set(names))
    assert all(f.question for f in plugin.blocking_fields())  # a blocking field must be askable
    assert set(plugin.source_fields) <= set(names)
    monitor = plugin.monitor
    assert monitor.completed_event_types, "a domain must say what 'done' means"
    assert all(monitor.rule(t) for t in monitor.completed_event_types)
    assert monitor.deadline_field is None or plugin.field(monitor.deadline_field)


@pytest.mark.parametrize("plugin", PLUGINS, ids=by_name)
def test_payment_events_mean_the_same_in_every_domain(plugin):
    assert [plugin.monitor.rule(r.event_type) for r in PAYMENT_RULES] == list(PAYMENT_RULES)


# ---- plan safety ------------------------------------------------------------------------------


def _ancestors(plan: Plan, step_id: str) -> set[str]:
    seen: set[str] = set()
    pending = [step_id]
    while pending:
        for dep in plan.step(pending.pop()).depends_on:
            if dep not in seen:
                seen.add(dep)
                pending.append(dep)
    return seen


@pytest.mark.parametrize("plugin", PLUGINS, ids=by_name)
def test_money_never_moves_before_the_user_approves(plugin):
    plan = plugin.plan_template()
    assert plan.steps[0].step_id == "intent"
    assert "approval" in {s.step_id for s in plan.steps}
    last = plan.steps[-1].step_id  # order / engage / settle
    assert {"approval", "verify"} <= _ancestors(plan, last)


@pytest.mark.parametrize("plugin", PLUGINS, ids=by_name)
def test_building_a_plan_is_pure_and_audited_per_domain(plugin):
    class Sink:
        events: list = []

        def __init__(self) -> None:
            self.events = []

        def enqueue(self, event) -> None:
            self.events.append(event)

    sink = Sink()
    assert build_plan(plugin, audit=AuditScope(sink, "r1")) == build_plan(plugin)
    loaded = next(e for e in sink.events if e.action == "DOMAIN_LOADED")
    assert (loaded.phase, loaded.resource_id) == ("domain", plugin.name)


def test_switching_domains_never_leaks_steps_or_plan_state():
    plans = {name: build_plan(get_plugin(name))["plan"] for name in DOMAINS}
    assert len({json.dumps(p, sort_keys=True) for p in plans.values()}) == 3
    assert build_plan(get_plugin("shopping"))["plan"] == plans["shopping"]  # unchanged after visiting the others


# ---- intent scoping ---------------------------------------------------------------------------

# One field that only this domain declares, with a message grounding it.
OWN_FIELD = {
    "shopping": ("brand", "nike", "shoes from nike"),
    "contractor": ("location", "pune", "a developer in pune"),
    "a2a": ("counterparty", "acme", "negotiate with acme"),
}


def _reply(field: str, value: str, evidence: str, goal: str = "do it") -> str:
    item = {"field": field, "operator": "==", "value": value, "strength": "hard", "evidence": evidence}
    return json.dumps({"goal": goal, "constraints": [item]})


@pytest.mark.parametrize("plugin", PLUGINS, ids=by_name)
async def test_a_domains_own_field_is_extracted_and_missing_blockers_are_asked(plugin):
    field, value, message = OWN_FIELD[plugin.name]
    result, _ = await extract_intent(message, plugin, provider=FakeLLM(_reply(field, value, value)))
    assert any(c.field_name == field for c in result.constraints)
    expected = tuple(f.name for f in plugin.blocking_fields() if f.name != field)
    assert result.missing == expected
    assert (result.status is IntentStatus.BLOCKED) == bool(expected)
    assert all(f.question in result.questions for f in plugin.blocking_fields() if f.name != field)


@pytest.mark.parametrize(
    ("domain", "owner"), [(d, o) for d in DOMAINS for o in DOMAINS if d != o], ids=lambda v: v
)
async def test_a_field_from_another_domain_is_rejected(domain, owner):
    plugin = get_plugin(domain)
    field, value, message = OWN_FIELD[owner]
    bad = _reply(field, value, value)
    with pytest.raises(IntentError):  # rejected, corrected once, rejected again
        await extract_intent(message, plugin, provider=FakeLLM(bad, bad))


# ---- monitor ----------------------------------------------------------------------------------


@pytest.mark.parametrize("plugin", PLUGINS, ids=by_name)
async def test_every_declared_event_type_is_accepted_by_the_shared_engine(factory, plugin):
    view, entity = VIEWS[plugin.name]
    for rule in plugin.monitor.rules:
        payload = {rule.date_key: "2026-11-12T00:00:00Z"} if rule.date_key else {}
        (result,) = await run(factory, view, event(view, rule.event_type, NOW, entity=entity, **payload))
        assert result.outcome is EventOutcome.APPLIED, rule.event_type


@pytest.mark.parametrize("plugin", PLUGINS, ids=by_name)
async def test_a_task_never_closes_before_its_payment_is_settled(factory, plugin):
    view, entity = VIEWS[plugin.name]
    done = sorted(plugin.monitor.completed_event_types)[0]
    (result,) = await run(factory, view, event(view, done, NOW, entity=entity))
    assert not result.completed and route_monitor_result(result) is Route.CONTINUE_MONITORING


@pytest.mark.parametrize("plugin", PLUGINS, ids=by_name)
async def test_a_task_closes_once_the_work_is_done_and_the_payment_is_settled(factory, plugin):
    view, entity = VIEWS[plugin.name]
    await capture_payment(factory)
    done = sorted(plugin.monitor.completed_event_types)[0]
    (result,) = await run(factory, view, event(view, done, NOW, entity=entity))
    assert result.completed and route_monitor_result(result) is Route.CLOSE


# ---- payment gating (shared Executor, any domain) ---------------------------------------------


def _terms(domain: str) -> ApprovalTerms:
    return ApprovalTerms(
        subject_id=f"{domain}-1", parties={"payee_id": "p1"}, currency="INR", max_total_minor=550000,
        expected_total_minor=520000, latest_acceptable_at=DEADLINE,
    )


def _observed(terms: ApprovalTerms, **kw) -> ObservedTerms:
    base = {
        "available": True, "subject_id": terms.subject_id, "parties": terms.parties, "currency": "INR",
        "total_minor": 520000, "available_by": DEADLINE - timedelta(days=3),
    }
    return ObservedTerms(**{**base, **kw})


class Observer:
    def __init__(self, terms: ApprovalTerms) -> None:
        self.terms, self.changes = terms, {}

    async def observe(self, terms):
        return _observed(self.terms, **self.changes)


class Source:
    def __init__(self, terms: ApprovalTerms) -> None:
        self.terms, self.confirmed = terms, True

    async def collect(self, terms, order_id):
        return Evidence(source="counterparty", observed=_observed(self.terms), confirmed=self.confirmed)


MONEY = (Action.CREATE_ORDER, Action.AUTHORIZE_PAYMENT, Action.CAPTURE_PAYMENT)


def _world(domain: str):
    terms = _terms(domain)
    auth = TaskAuthorization(
        authorization_id="a1", run_id="r1", max_spend=Decimal(6000), permitted_platforms=frozenset({"plat"}),
        permitted_actions=frozenset({*MONEY, Action.VOID_PAYMENT}),
    )
    approvals = {
        a: issue_approval(
            terms=terms, run_id="r1", authorization_id="a1", action=a, candidate_id=terms.subject_id, platform="plat",
            approved_by="u1", session_id="s1", key=KEY, key_id="v1", ttl=timedelta(minutes=10),
        )
        for a in MONEY
    }
    verifier = signed_approval_verifier(current_terms=terms, keys={"v1": KEY}, run_id="r1", session_id="s1", approved_by="u1")
    provider, observer, source = FakeProvider(), Observer(terms), Source(terms)
    return {
        "terms": terms, "provider": provider, "observer": observer, "source": source, "approvals": approvals,
        "permit": PaymentPermit(auth, "plat", tuple(approvals.values()), verifier),
        "executor": Executor(PaymentService(provider), observer, source, VerificationSpec(max_attempts=2)),
    }


async def _place(factory, w, *, approval: Approval | None = None):
    async with factory() as s, s.begin():
        return await w["executor"].place_order(
            s, permit=w["permit"], approval=approval or w["approvals"][Action.CREATE_ORDER], terms=w["terms"],
            order_id="o1", payee_id="p1",
        )


async def _settle(factory, w, payment_id):
    w["provider"].order_status = "payer_approved"
    async with factory() as s, s.begin():
        return await w["executor"].settle(s, payment_id, permit=w["permit"], terms=w["terms"], order_id="o1")


def _captured(w) -> bool:
    return any(name == "capture" for name, _ in w["provider"].calls)


@pytest.mark.parametrize("domain", DOMAINS)
async def test_verified_delivery_is_captured_in_every_domain(factory, domain):
    w = _world(domain)
    placed = await _place(factory, w)
    result = await _settle(factory, w, placed.created.payment.id)
    assert result.state is ExecutionState.SETTLED and result.captured == Decimal(5200) and _captured(w)


@pytest.mark.parametrize("domain", DOMAINS)
async def test_rejected_delivery_is_voided_not_captured_in_every_domain(factory, domain):
    w = _world(domain)
    placed = await _place(factory, w)
    w["source"].confirmed = False
    result = await _settle(factory, w, placed.created.payment.id)
    assert result.state is ExecutionState.VOIDED and not _captured(w)


@pytest.mark.parametrize("domain", DOMAINS)
async def test_a_price_rise_blocks_the_order_in_every_domain(factory, domain):
    w = _world(domain)
    w["observer"].changes = {"total_minor": 560000}
    with pytest.raises(ExecutionBlocked) as info:
        await _place(factory, w)
    assert info.value.report.action is MismatchAction.REAPPROVE and not w["provider"].calls


@pytest.mark.parametrize("domain", DOMAINS)
async def test_an_unsigned_approval_cannot_place_an_order_in_every_domain(factory, domain):
    w = _world(domain)
    forged = w["approvals"][Action.CREATE_ORDER].model_copy(update={"signature": "0" * 64})
    with pytest.raises(ExecutionBlocked):
        await _place(factory, w, approval=forged)
    assert not w["provider"].calls


@pytest.mark.parametrize("domain", DOMAINS)
async def test_an_approval_for_another_domains_terms_is_refused(factory, domain):
    other = next(d for d in DOMAINS if d != domain)
    w, foreign = _world(domain), _world(other)
    with pytest.raises(ExecutionBlocked):
        await _place(factory, w, approval=foreign["approvals"][Action.CREATE_ORDER])
    assert not w["provider"].calls


# ---- discovery scope --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("domain", "source"), [("shopping", {"mock_amazon", "mock_flipkart"}), ("contractor", {"mock_contractors"})]
)
async def test_discovery_stays_inside_the_requested_domain(store, domain, source):
    registry = make_registry(store)
    auth = make_auth({"mock_amazon", "mock_flipkart", "mock_contractors"})
    request = SHOES.model_copy(update={"domain": domain, "query": "dev" if domain == "contractor" else "running shoes"})
    result = await registry.search_resilient(auth, request)
    assert result.candidates and {c.source for c in result.candidates} <= source
    assert {c.domain for c in result.candidates} == {domain}
