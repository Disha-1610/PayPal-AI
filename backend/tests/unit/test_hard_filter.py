from decimal import Decimal

from app.orchestrator.agents.evaluation.hard_filter import hard_filter
from app.schemas.candidate import Candidate, DataQuality
from app.schemas.constraints import Constraint, ConstraintSource, ConstraintStrength, Operator
from app.schemas.evaluation import RejectionKind


def _c(field, op, value, strength=ConstraintStrength.HARD, **kw):
    return Constraint(
        field_name=field, operator=op, target_value=value, source=ConstraintSource.USER, strength=strength, **kw
    )


def _cand(cid, **kw):
    return Candidate(candidate_id=cid, source="t", domain="contractor", title=cid, **kw)


BUDGET = _c("budget", Operator.LTE, 8000)
LICENSED = _c("licensed", Operator.EQ, True)
START = _c("start_within_days", Operator.LTE, 30)


def _pool():
    return [
        _cand("A", total_cost=Decimal("7200"), attributes={"licensed": True, "start_within_days": 14}),
        _cand("B", total_cost=Decimal("7900"), attributes={"licensed": True, "start_within_days": 21}),
        _cand("C", total_cost=Decimal("9500"), attributes={"licensed": True, "start_within_days": 10}),
        _cand("D", total_cost=Decimal("6000"), attributes={"licensed": False, "start_within_days": 7}),
        _cand("E", total_cost=Decimal("7500"), attributes={"licensed": True, "start_within_days": 60}),
        _cand(
            "F",
            total_cost=Decimal("7000"),
            attributes={"start_within_days": 20},
            quality={"licensed": DataQuality.MISSING},
        ),
    ]


def test_contractor_example_filters_failed_and_unverifiable():
    result = hard_filter(_pool(), [BUDGET, LICENSED, START])
    assert [c.candidate_id for c in result.survivors] == ["A", "B"]
    assert result.failed_ids == {"C", "D", "E"}
    assert result.unverifiable_ids == {"F"}
    by_cand = {r.candidate_id: r for r in result.rejections}
    assert by_cand["C"].field_name == "budget" and by_cand["C"].kind is RejectionKind.FAILED
    assert by_cand["F"].kind is RejectionKind.UNVERIFIABLE


def test_soft_and_inactive_constraints_do_not_filter():
    soft = _c("rating", Operator.GTE, 4.5, ConstraintStrength.SOFT)
    off = _c("budget", Operator.LTE, 1, active=False)
    result = hard_filter(_pool(), [soft, off])
    assert len(result.survivors) == 6 and result.rejections == ()


def test_candidate_failing_several_constraints_gets_every_rejection():
    cand = _cand("X", total_cost=Decimal("9000"), attributes={"licensed": False})
    result = hard_filter([cand], [BUDGET, LICENSED])
    assert {r.field_name for r in result.rejections} == {"budget", "licensed"}
    assert result.eliminations_by_constraint() == {BUDGET.constraint_id: 1, LICENSED.constraint_id: 1}


def test_missing_value_without_quality_flag_is_unverifiable():
    result = hard_filter([_cand("X", attributes={})], [LICENSED])
    assert result.unverifiable_ids == {"X"} and result.survivors == ()


def test_unreliable_flag_makes_present_value_unverifiable():
    cand = _cand("X", rating=4.9, quality={"rating": DataQuality.UNRELIABLE})
    result = hard_filter([cand], [_c("rating", Operator.GTE, 4.5)])
    assert result.unverifiable_ids == {"X"}


def test_stale_value_is_still_evaluated():
    cand = _cand("X", total_cost=Decimal("100"), quality={"total_cost": DataQuality.STALE})
    assert hard_filter([cand], [BUDGET]).survivors == (cand,)


def test_range_boundary_is_inclusive_for_lte_and_exclusive_for_lt():
    cand = _cand("X", total_cost=Decimal("8000"))
    assert hard_filter([cand], [BUDGET]).survivors == (cand,)
    assert hard_filter([cand], [_c("budget", Operator.LT, 8000)]).survivors == ()


def test_wrong_type_is_unverifiable_not_a_crash():
    cand = _cand("X", attributes={"start_within_days": "soon"})
    assert hard_filter([cand], [START]).unverifiable_ids == {"X"}
    boolean = _cand("Y", attributes={"start_within_days": True})
    assert hard_filter([boolean], [START]).unverifiable_ids == {"Y"}


def test_text_equality_ignores_case_and_membership_operators():
    cand = _cand("X", attributes={"location": " Pune ", "skills": ["Python", "SQL", "Go"]})
    assert hard_filter([cand], [_c("location", Operator.EQ, "pune")]).survivors == (cand,)
    assert hard_filter([cand], [_c("location", Operator.IN, ["pune", "mumbai"])]).survivors == (cand,)
    assert hard_filter([cand], [_c("location", Operator.NOT_IN, ["pune"])]).survivors == ()
    assert hard_filter([cand], [_c("skills", Operator.IN, ["python", "sql"])]).survivors == (cand,)
    assert hard_filter([cand], [_c("skills", Operator.IN, ["python", "rust"])]).survivors == ()
    assert hard_filter([cand], [_c("skills", Operator.NOT_IN, ["rust"])]).survivors == (cand,)


def test_no_constraints_passes_everything_and_empty_pool_is_fine():
    assert len(hard_filter(_pool(), []).survivors) == 6
    assert hard_filter([], [BUDGET]).survivors == ()
