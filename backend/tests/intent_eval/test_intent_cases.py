"""Fixed Intent test set (tests/fixtures/intent_cases/cases.json).

Each case holds the user text, what a correct model would output (``llm``), and the expected
outcome (``expect``). Offline, the scripted output runs through the real deterministic pipeline
(grounding, status, conflicts, patch application). With ``-m live`` and ``LLM_API_KEY`` set, the
same cases run against the real model: that run is the gate before other agents are built.
"""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from app.core.config import get_settings
from app.domains import get_plugin
from app.orchestrator.agents.intent.agent import extract_intent, patch_intent
from app.schemas.constraints import Constraint, ConstraintSource, ConstraintStrength, Operator
from conftest import FakeProvider

CASES = json.loads((Path(__file__).parents[1] / "fixtures" / "intent_cases" / "cases.json").read_text("utf-8"))
IDS = [c["id"] for c in CASES]


def _norm(value):
    if isinstance(value, list):
        return sorted(_norm(v) for v in value)
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float, Decimal)):
        return format(Decimal(str(value)).normalize(), "f")
    return str(value).lower()


def _matches(expected, actual) -> bool:
    return expected == "*" or _norm(expected) == _norm(actual)


def _constraint(spec: dict) -> Constraint:
    return Constraint(
        field_name=spec["field"],
        operator=Operator(spec["operator"]),
        target_value=spec["value"],
        source=ConstraintSource.USER,
        strength=ConstraintStrength(spec["strength"]),
    )


def _assert_same_items(expected: list[dict], actual: list[dict], label: str) -> None:
    remaining = list(actual)
    for want in expected:
        hit = next((a for a in remaining if all(_matches(want[k], a[k]) for k in want)), None)
        assert hit is not None, f"{label}: expected {want} not found in {actual}"
        remaining.remove(hit)
    assert not remaining, f"{label}: unexpected extra items {remaining}"


async def _run_case(case: dict, provider):
    plugin = get_plugin(case["domain"])
    if case["kind"] == "extract":
        result, _ = await extract_intent(case["text"], plugin, provider=provider)
        return result, None
    current = [_constraint(c) for c in case["current"]]
    result, _, changes = await patch_intent(case["text"], case["goal"], current, plugin, provider=provider)
    return result, changes


def _assert_case(case: dict, result, changes) -> None:
    expect = case["expect"]
    assert result.status.value == expect["status"]
    assert sorted(result.missing) == sorted(expect["missing"])
    assert bool(result.conflicts) == expect["conflicts"]
    actual = [
        {"field": c.field_name, "operator": c.operator.value, "value": c.target_value, "strength": c.strength.value}
        for c in result.constraints
        if c.active
    ]
    _assert_same_items(expect["constraints"], actual, "constraints")
    if case["kind"] == "patch":
        got = [{"op": c.op.value, "field": c.field, "old": c.old, "new": c.new} for c in changes]
        _assert_same_items(expect["changes"], got, "changes")
        assert all(c.source is ConstraintSource.USER for c in changes)


def test_case_set_size():
    assert 15 <= len(CASES) <= 20
    assert {"extract", "patch"} == {c["kind"] for c in CASES}
    assert {"shopping", "contractor", "a2a"} == {c["domain"] for c in CASES}


@pytest.mark.parametrize("case", CASES, ids=IDS)
async def test_scripted_pipeline(case):
    provider = FakeProvider(json.dumps(case["llm"]))
    result, changes = await _run_case(case, provider)
    _assert_case(case, result, changes)


@pytest.mark.live
@pytest.mark.skipif(get_settings().llm_api_key is None, reason="LLM_API_KEY not set")
@pytest.mark.parametrize("case", CASES, ids=IDS)
async def test_live_model(case):
    result, changes = await _run_case(case, None)
    _assert_case(case, result, changes)
