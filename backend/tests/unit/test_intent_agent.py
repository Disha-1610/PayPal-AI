import json
from decimal import Decimal

import pytest

from app.domains import get_plugin
from app.orchestrator.agents.intent.agent import (
    INTENT_WRITABLE,
    IntentError,
    apply_patch,
    detect_conflicts,
    extract_intent,
    patch_intent,
    to_state_update,
)
from app.schemas.constraints import Constraint, ConstraintSource, ConstraintStrength, Operator
from app.schemas.intent import IntentPatchOp
from app.schemas.state import validate_state_update
from conftest import FakeProvider

SHOP = get_plugin("shopping")


def _c(field, operator, value, strength=ConstraintStrength.HARD) -> Constraint:
    return Constraint(
        field_name=field, operator=operator, target_value=value, source=ConstraintSource.USER, strength=strength
    )


def _extraction(*constraints, goal="shoes") -> str:
    return json.dumps({"goal": goal, "constraints": list(constraints)})


GOOD = {"field": "budget", "operator": "lt", "value": 5000, "strength": "hard", "evidence": "under 5000"}


async def test_ungrounded_evidence_is_rejected_then_corrected():
    bad = {**GOOD, "evidence": "somewhere else entirely"}
    provider = FakeProvider(_extraction(bad), _extraction(GOOD))
    result, _ = await extract_intent("shoes under 5000", SHOP, provider=provider)
    assert result.constraints[0].target_value == Decimal(5000)
    assert "does not appear in the user's message" in provider.prompts[1]


async def test_persistent_bad_output_raises_instead_of_hallucinating():
    bad = {**GOOD, "evidence": "invented"}
    with pytest.raises(IntentError):
        await extract_intent("shoes under 5000", SHOP, provider=FakeProvider(_extraction(bad), _extraction(bad)))


async def test_unknown_field_is_rejected():
    bad = {**GOOD, "field": "shoe_size_eu"}
    provider = FakeProvider(_extraction(bad), _extraction(GOOD))
    await extract_intent("shoes under 5000", SHOP, provider=provider)
    assert "allowed fields" in provider.prompts[1]


async def test_wrong_value_type_is_rejected():
    bad = {**GOOD, "value": "five thousand"}
    provider = FakeProvider(_extraction(bad), _extraction(GOOD))
    await extract_intent("shoes under 5000", SHOP, provider=provider)
    assert "not a valid number" in provider.prompts[1]


async def test_prompt_lists_plugin_fields_and_quotes_user_text():
    provider = FakeProvider(_extraction(GOOD))
    await extract_intent("shoes under 5000", SHOP, provider=provider)
    prompt = provider.prompts[0]
    assert "- budget (number)" in prompt and "<user_message>\nshoes under 5000\n</user_message>" in prompt


def test_detect_conflicts_only_considers_hard_constraints():
    soft = _c("budget", Operator.GT, 9000, ConstraintStrength.SOFT)
    assert detect_conflicts([_c("budget", Operator.LT, 100), soft]) == []
    assert detect_conflicts([_c("budget", Operator.LT, 100), _c("budget", Operator.GT, 9000)])
    assert detect_conflicts([_c("budget", Operator.LTE, 100), _c("budget", Operator.GTE, 100)]) == []
    assert detect_conflicts([_c("budget", Operator.LT, 100), _c("budget", Operator.GTE, 100)])
    assert detect_conflicts([_c("brand", Operator.EQ, "Nike"), _c("brand", Operator.NE, "Nike")])
    assert detect_conflicts([_c("budget", Operator.EQ, 500), _c("budget", Operator.LT, 100)])


def test_apply_patch_reconciles_op_kind_with_state():
    current = [_c("budget", Operator.LT, 5000)]
    update_absent = IntentPatchOp(op="update", field="color", operator="==", value="red", evidence="red")
    updated, changes = apply_patch([update_absent], current, SHOP)
    assert changes[0].op.value == "add" and len(updated) == 2

    add_present = IntentPatchOp(op="add", field="budget", operator="lt", value=6000, evidence="6000")
    updated, changes = apply_patch([add_present], current, SHOP)
    assert changes[0].op.value == "update" and changes[0].old == Decimal(5000)
    assert updated[0].revision == 2 and updated[0].history[0].target_value == Decimal(5000)


def test_apply_patch_leaves_untouched_constraints_identical():
    keep, change = _c("brand", Operator.EQ, "Nike"), _c("budget", Operator.LT, 5000)
    op = IntentPatchOp(op="update", field="budget", value=5500, evidence="5500")
    updated, _ = apply_patch([op], [keep, change], SHOP)
    assert updated[0] is keep and updated[1].target_value == Decimal(5500)


def test_apply_patch_rejects_ambiguous_and_missing_targets():
    both = [_c("budget", Operator.GT, 10), _c("budget", Operator.LT, 100)]
    with pytest.raises(Exception, match="several constraints"):
        apply_patch([IntentPatchOp(op="update", field="budget", value=50, evidence="x")], both, SHOP)
    with pytest.raises(Exception, match="no active constraint"):
        apply_patch([IntentPatchOp(op="remove", field="brand", evidence="x")], both, SHOP)


def test_patch_op_shape_validation():
    with pytest.raises(ValueError, match="add requires"):
        IntentPatchOp(op="add", field="budget", evidence="x")
    with pytest.raises(ValueError, match="update requires"):
        IntentPatchOp(op="update", field="budget", evidence="x")


async def test_patch_cannot_ground_evidence_outside_user_text():
    current = [_c("budget", Operator.LT, 5000)]
    bad = json.dumps({"ops": [{"op": "update", "field": "budget", "value": 1, "evidence": "not said"}]})
    with pytest.raises(IntentError):
        await patch_intent("hello", "shoes", current, SHOP, provider=FakeProvider(bad, bad))


async def test_state_update_respects_node_ownership():
    result, diff = await extract_intent("shoes under 5000", SHOP, provider=FakeProvider(_extraction(GOOD)))
    update = to_state_update(result, diff)
    validate_state_update("intent", update, INTENT_WRITABLE)
    assert update["status"] == "ready" and len(diff.added_constraints) == 1
    assert "authorization" not in update
