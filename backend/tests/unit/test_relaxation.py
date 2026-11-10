"""Relaxation proposals across unrelated domains: the logic keys off operators, never field meaning."""

from decimal import Decimal

import pytest

from app.orchestrator.agents.evaluation.diagnosis import diagnose, summarize
from app.orchestrator.agents.adapt.relaxation import ProposalError, apply_proposal, propose_relaxations
from app.schemas.adapt import AdaptPolicy, RelaxationOp, UserDecision, UserDecisionKind
from app.schemas.candidate import Candidate, DataQuality
from app.schemas.constraints import Constraint, ConstraintSource, ConstraintStrength, Operator


def _c(field, op, value, strength=ConstraintStrength.HARD):
    return Constraint(
        field_name=field, operator=op, target_value=value, source=ConstraintSource.USER, strength=strength
    )


def _cand(cid, cost=None, **attrs):
    return Candidate(
        candidate_id=cid,
        source="t",
        domain="any",
        title=cid,
        total_cost=None if cost is None else Decimal(cost),
        attributes=attrs,
    )


def _by_field(proposals):
    return {(p.field_name, p.operator, str(p.new_value)): p for p in proposals}


# ---- travel-style: budget cap + time window + vendor lock --------------------------------------

BUDGET = _c("budget", Operator.LTE, 10000)
DEPART_FROM = _c("departure_minute", Operator.GTE, 480)
DEPART_TO = _c("departure_minute", Operator.LTE, 600)
VENDOR = _c("vendor", Operator.EQ, "Air India")


def _flights():
    return [
        _cand("A", 12000, departure_minute=540, vendor="Air India"),
        _cand("B", 9500, departure_minute=390, vendor="Air India"),
        _cand("C", 9000, departure_minute=540, vendor="Indigo"),
        _cand("D", 11000, departure_minute=570, vendor="Air India"),
        _cand("E", 13000, departure_minute=300, vendor="Air India"),  # fails budget AND time
    ]


def test_diagnosis_records_every_failed_constraint_per_candidate():
    by_id = {d.candidate_id: d for d in diagnose(_flights(), [BUDGET, DEPART_FROM, DEPART_TO, VENDOR])}
    assert {v.constraint.field_name for v in by_id["E"].violations} == {"budget", "departure_minute"}
    assert by_id["E"].near_miss is None
    assert by_id["A"].near_miss.constraint is BUDGET and by_id["A"].near_miss.actual == Decimal(12000)
    assert "budget: 3 candidate(s)" in summarize(list(by_id.values()))


def test_range_proposals_are_cumulative_and_minimal():
    proposals = propose_relaxations(_flights(), [BUDGET, DEPART_FROM, DEPART_TO, VENDOR], policy=AdaptPolicy(max_proposals=10))
    table = _by_field(proposals)
    two = table[("budget", Operator.LTE, str(Decimal(12000)))]
    assert set(two.unlocked_ids) == {"A", "D"} and two.op is RelaxationOp.UPDATE
    assert proposals[0] == two  # most options unlocked comes first
    one = table[("budget", Operator.LTE, str(Decimal(11000)))]  # the smaller ask is offered too
    assert set(one.unlocked_ids) == {"D"} and one.cost < two.cost
    assert set(table[("departure_minute", Operator.GTE, str(Decimal(390)))].unlocked_ids) == {"B"}
    assert "more option(s)" in two.description and "12000" in two.description


def test_categorical_lock_becomes_allow_value_proposal():
    proposals = propose_relaxations(_flights(), [BUDGET, DEPART_FROM, DEPART_TO, VENDOR], policy=AdaptPolicy(max_proposals=10))
    vendor = next(p for p in proposals if p.field_name == "vendor")
    assert vendor.operator is Operator.IN and vendor.new_value == ["Air India", "Indigo"]
    assert vendor.unlocked_ids == ("C",)


def test_multi_failure_unverifiable_and_excluded_candidates_never_count():
    pool = [
        *_flights(),
        Candidate(
            candidate_id="U", source="t", domain="any", title="U", total_cost=Decimal(10500),
            attributes={"departure_minute": 540}, quality={"vendor": DataQuality.MISSING},
        ),
    ]
    constraints = [BUDGET, DEPART_FROM, DEPART_TO, VENDOR]
    ids = {i for p in propose_relaxations(pool, constraints) for i in p.unlocked_ids}
    assert "E" not in ids and "U" not in ids
    blocked = propose_relaxations(pool, constraints, excluded_ids=frozenset({"A", "D"}))
    assert all(p.field_name != "budget" for p in blocked)


def test_max_proposals_caps_the_menu():
    assert len(propose_relaxations(_flights(), [BUDGET, DEPART_FROM, DEPART_TO, VENDOR], policy=AdaptPolicy(max_proposals=1))) == 1


# ---- contractor / services: boolean flag, deadline, list-valued skills -------------------------


def test_boolean_requirement_is_dropped():
    pool = [_cand("P", licensed=False, rate=1), _cand("Q", licensed=False, rate=2)]
    [proposal] = propose_relaxations(pool, [_c("licensed", Operator.EQ, True)])
    assert proposal.op is RelaxationOp.REMOVE and set(proposal.unlocked_ids) == {"P", "Q"}


def test_list_valued_attribute_drops_only_the_missing_items():
    need = _c("skills", Operator.IN, ["python", "sql", "go"])
    pool = [
        _cand("P", skills=["python", "sql"]),  # misses go
        _cand("Q", skills=["python"]),  # misses sql, go
    ]
    proposals = {len(p.unlocked_ids): p for p in propose_relaxations(pool, [need])}
    assert proposals[1].new_value == ["python", "sql"] and proposals[1].operator is Operator.IN
    assert proposals[2].new_value == ["python"] and proposals[2].cost > proposals[1].cost


def test_not_in_and_ne_relaxations():
    avoid = _c("material", Operator.NOT_IN, ["asbestos", "lead"])
    [p] = propose_relaxations([_cand("P", material="Lead")], [avoid])  # match is case-insensitive
    assert p.new_value == ["asbestos"]
    [q] = propose_relaxations([_cand("Q", region="eu")], [_c("region", Operator.NE, "eu")])
    assert q.op is RelaxationOp.REMOVE


def test_scalar_in_widens_the_allowed_set():
    want = _c("language", Operator.IN, ["en", "fr"])
    pool = [_cand("P", language="de"), _cand("Q", language="es")]
    proposals = propose_relaxations(pool, [want])
    both = max(proposals, key=lambda p: len(p.unlocked_ids))
    assert set(both.unlocked_ids) == {"P", "Q"} and both.new_value == ["en", "fr", "de", "es"]


def test_soft_and_inactive_constraints_are_never_relaxed():
    soft = _c("budget", Operator.LTE, 1, ConstraintStrength.SOFT)
    assert propose_relaxations(_flights(), [soft]) == []


# ---- applying an accepted proposal --------------------------------------------------------------


def test_apply_proposal_creates_a_user_sourced_revision_with_diff():
    constraints = [BUDGET, VENDOR]
    proposals = propose_relaxations(_flights(), [BUDGET, DEPART_FROM, DEPART_TO, VENDOR])
    chosen = next(p for p in proposals if p.field_name == "budget")
    updated, diff, change = apply_proposal(
        UserDecision(kind=UserDecisionKind.ACCEPT, proposal_id=chosen.proposal_id), proposals, constraints
    )
    new_budget = next(c for c in updated if c.field_name == "budget")
    assert new_budget.target_value == Decimal(12000) and new_budget.source is ConstraintSource.USER
    assert new_budget.history[-1].target_value == Decimal(10000)
    assert diff.modified_constraints and change.old == Decimal(10000) and change.source is ConstraintSource.USER


def test_apply_removal_deactivates_the_constraint():
    licensed = _c("licensed", Operator.EQ, True)
    proposals = propose_relaxations([_cand("P", licensed=False)], [licensed])
    updated, diff, change = apply_proposal(
        UserDecision(kind=UserDecisionKind.ACCEPT, proposal_id=proposals[0].proposal_id), proposals, [licensed]
    )
    assert updated[0].active is False and diff.removed_constraints and change.new is None


def test_unknown_or_outdated_proposal_is_rejected():
    proposals = propose_relaxations(_flights(), [BUDGET, VENDOR])
    accept = UserDecision(kind=UserDecisionKind.ACCEPT, proposal_id=proposals[0].proposal_id)
    with pytest.raises(ProposalError, match="unknown"):
        apply_proposal(UserDecision(kind=UserDecisionKind.ACCEPT, proposal_id="nope"), proposals, [BUDGET])
    moved = BUDGET.revise(reason="r", changed_by=ConstraintSource.USER, target_value=11000)
    with pytest.raises(ProposalError, match="out of date"):
        apply_proposal(accept, proposals, [moved, VENDOR])
