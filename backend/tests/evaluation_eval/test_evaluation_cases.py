"""Fixed Evaluation (Phase 3) test set: tests/fixtures/evaluation_cases/cases.json.

``golden`` cases pin the expected ranking/outcome for a fixed pool, so weight or policy changes
that alter results are caught. ``adversarial`` cases check that hostile or low-quality data
(prompt injection in free text, fake review bursts, bait prices, stale data) cannot steer the
result. Evaluation is deterministic, so these always run offline.
"""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from app.orchestrator.agents.evaluation.pipeline import evaluate
from app.orchestrator.agents.evaluation.ranking import normalise_weights
from app.schemas.candidate import Candidate
from app.schemas.constraints import Constraint, ConstraintSource, ConstraintStrength, Operator

NOW = datetime(2026, 11, 9, tzinfo=timezone.utc)
CASES = json.loads((Path(__file__).parents[1] / "fixtures" / "evaluation_cases" / "cases.json").read_text("utf-8"))
IDS = [c["id"] for c in CASES]
EVALUATION_PACKAGE = Path(__file__).resolve().parents[2] / "app" / "orchestrator" / "agents" / "evaluation"


def _candidate(spec: dict) -> Candidate:
    def days_ago(key: str) -> datetime | None:
        return NOW - timedelta(days=spec[key]) if key in spec else None

    return Candidate(
        candidate_id=spec["id"],
        source="fixture",
        domain="contractor",
        title=spec.get("title", spec["id"]),
        total_cost=Decimal(str(spec["total_cost"])) if "total_cost" in spec else None,
        rating=spec.get("rating"),
        review_count=spec.get("review_count"),
        first_review_at=days_ago("first_review_days_ago"),
        latest_review_at=days_ago("latest_review_days_ago"),
        attributes=spec.get("attributes", {}),
        quality=spec.get("quality", {}),
        fetched_at=NOW,
    )


def _constraint(spec: dict) -> Constraint:
    return Constraint(
        constraint_id=spec.get("id") or spec["field"],
        field_name=spec["field"],
        operator=Operator(spec["operator"]),
        target_value=spec["value"],
        source=ConstraintSource.USER,
        strength=ConstraintStrength(spec["strength"]),
    )


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_evaluation_case(case):
    report = evaluate(
        [_candidate(c) for c in case["candidates"]],
        [_constraint(c) for c in case["constraints"]],
        now=NOW,
        weights=case.get("weights"),
        attempt=case.get("attempt", 0),
    )
    want, outcome = case["expect"], report.outcome

    assert outcome.kind.value == want["outcome"]
    assert outcome.selected_candidate_id == want.get("selected")
    assert [r.candidate_id for r in report.ranking.ranked] == want["ranked"]
    assert sorted(report.filtered.failed_ids) == want["failed"]
    assert sorted(report.filtered.unverifiable_ids) == want["unverifiable"]
    assert sorted(a.candidate_id for a in report.assessments if a.blocked) == want["blocked"]
    if "options" in want:
        assert sorted(outcome.options) == want["options"]
    if "relax" in want:
        assert outcome.relax_constraint_id == want["relax"]
    for caveat in want.get("caveats", []):
        assert caveat in outcome.caveats

    # The trace must never echo free text from candidates (untrusted-data boundary).
    dumped = report.model_dump_json()
    for text in case.get("forbidden_in_report", []):
        assert text not in dumped


@pytest.mark.parametrize("case", [c for c in CASES if c["id"] == "adversarial-injection-in-title-and-attribute"])
def test_injected_text_does_not_change_the_result(case):
    constraints = [_constraint(c) for c in case["constraints"]]
    hostile = [_candidate(c) for c in case["candidates"]]
    sanitised = [
        _candidate({**c, "title": c["id"], "attributes": {}}) for c in case["candidates"]
    ]
    assert evaluate(hostile, constraints, now=NOW).ranking == evaluate(sanitised, constraints, now=NOW).ranking


@pytest.mark.parametrize(
    "kwargs",
    [
        {"total_cost": Decimal("NaN")},
        {"total_cost": Decimal("Infinity")},
        {"total_cost": Decimal("-5")},
        {"rating": float("nan")},
        {"rating": 9.0},
        {"review_count": -1},
    ],
)
def test_malformed_numbers_cannot_enter_a_candidate(kwargs):
    with pytest.raises(ValueError):
        Candidate(candidate_id="x", source="s", domain="contractor", title="t", **kwargs)


def test_non_finite_attribute_is_unverifiable_not_a_crash():
    nan = Candidate(
        candidate_id="n", source="s", domain="contractor", title="t", attributes={"start_within_days": float("nan")}
    )
    start = Constraint(
        field_name="start_within_days",
        operator=Operator.LTE,
        target_value=30,
        source=ConstraintSource.USER,
    )
    report = evaluate([nan], [start], now=NOW)
    assert report.filtered.unverifiable_ids == {"n"}


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1.0])
def test_hostile_weights_are_rejected(bad):
    with pytest.raises(ValueError):
        normalise_weights({"trust": bad}, ["trust"])


def test_duplicate_candidate_ids_are_rejected():
    twin = _candidate({"id": "A", "total_cost": 1, "review_count": 100})
    with pytest.raises(ValueError, match="duplicate candidate ids"):
        evaluate([twin, twin], [], now=NOW)


def test_evaluation_package_is_deterministic_and_llm_free():
    forbidden = ("app.llm", "app.db", "app.connectors.base.models")
    offenders = [
        str(p.name)
        for p in EVALUATION_PACKAGE.glob("*.py")
        if any(f"import {m}" in p.read_text("utf-8") or f"from {m}" in p.read_text("utf-8") for m in forbidden)
    ]
    assert offenders == []
