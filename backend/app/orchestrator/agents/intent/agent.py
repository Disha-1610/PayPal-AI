"""Intent agent: user text -> goal + constraints, and user updates -> constraint patches.

The LLM only *reports* what the user said, with quoted evidence. Everything that decides
behaviour is deterministic code: grounding checks, status (ready/blocked/out_of_scope),
clarifying questions (taken from the domain plug-in), conflict detection, and patch application.
"""

from collections.abc import Callable
from decimal import Decimal

from pydantic import BaseModel

from app.audit.events import AuditScope, Phase
from app.core.logging import get_logger
from app.domains.base import DomainPlugin, FieldSpec, ValueType
from app.llm.gateway import call
from app.llm.providers import LLMProvider
from app.orchestrator.prompts import render
from app.schemas.constraints import (
    RANGE_OPERATORS,
    Constraint,
    ConstraintDiff,
    ConstraintSource,
    ConstraintStrength,
    ConstraintValue,
    Operator,
    diff_constraints,
)
from app.schemas.intent import (
    AppliedChange,
    ExtractedConstraint,
    IntentExtraction,
    IntentPatch,
    IntentPatchOp,
    IntentResult,
    IntentStatus,
    LLMValue,
    PatchOp,
)

logger = get_logger(__name__)

# Fields this node may write into TaskState (checked with ``validate_state_update``).
INTENT_WRITABLE = frozenset({"goal", "constraints", "constraint_diffs", "status", "questions"})


class IntentError(Exception):
    """The model's output could not be made valid after one corrective retry."""


class _Rejected(Exception):
    """Model output failed a deterministic check; the message is fed back for one retry."""


# --------------------------------------------------------------------------- validation


def _normalise(text: str) -> str:
    return " ".join(text.lower().split())


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _typed_value(spec: FieldSpec, operator: Operator, value: LLMValue) -> ConstraintValue:
    """Check ``value`` against the field's declared type and normalise numbers to ``Decimal``."""
    items = value if isinstance(value, list) else [value]
    if operator in RANGE_OPERATORS and spec.value_type is not ValueType.NUMBER:
        raise _Rejected(f"operator {operator.value!r} needs a number field, but {spec.name!r} is {spec.value_type.value}")
    if spec.value_type is ValueType.NUMBER:
        valid = all(_is_number(i) for i in items)
    elif spec.value_type is ValueType.BOOLEAN:
        valid = all(isinstance(i, bool) for i in items)
    else:
        valid = all(isinstance(i, str) for i in items)
    if not valid:
        raise _Rejected(f"value {value!r} is not a valid {spec.value_type.value} for field {spec.name!r}")

    def convert(item: object) -> object:
        return Decimal(str(item)) if isinstance(item, float) else item

    return [convert(i) for i in items] if isinstance(value, list) else convert(value)


def _spec_for(plugin: DomainPlugin, field: str) -> FieldSpec:
    spec = plugin.field(field)
    if spec is None:
        allowed = ", ".join(f.name for f in plugin.fields)
        raise _Rejected(f"field {field!r} is not allowed; allowed fields: {allowed}")
    return spec


def _check_grounded(evidence: str, text: str) -> None:
    if _normalise(evidence) not in _normalise(text):
        raise _Rejected(f"evidence {evidence!r} does not appear in the user's message")


def _build_constraint(item: ExtractedConstraint, text: str, plugin: DomainPlugin) -> Constraint:
    spec = _spec_for(plugin, item.field)
    _check_grounded(item.evidence, text)
    value = _typed_value(spec, item.operator, item.value)
    try:
        return Constraint(
            field_name=item.field,
            operator=item.operator,
            target_value=value,
            source=ConstraintSource.USER,
            strength=item.strength,
        )
    except ValueError as exc:
        raise _Rejected(f"constraint on {item.field!r} is invalid: {exc}") from exc


# --------------------------------------------------------------------------- conflicts / status


def detect_conflicts(constraints: list[Constraint]) -> list[str]:
    """Deterministic contradictions between *hard*, active constraints on the same field."""
    by_field: dict[str, list[Constraint]] = {}
    for c in constraints:
        if c.active and c.strength is ConstraintStrength.HARD:
            by_field.setdefault(c.field_name, []).append(c)

    found: list[str] = []
    for field, group in by_field.items():
        lower = [(c.target_value, c.operator is Operator.GT) for c in group if c.operator in (Operator.GT, Operator.GTE)]
        upper = [(c.target_value, c.operator is Operator.LT) for c in group if c.operator in (Operator.LT, Operator.LTE)]
        equals = [c.target_value for c in group if c.operator is Operator.EQ]
        excluded = [
            v
            for c in group
            if c.operator in (Operator.NE, Operator.NOT_IN)
            for v in (c.target_value if isinstance(c.target_value, list) else [c.target_value])
        ]

        if lower and upper:
            low, low_strict = max(lower, key=lambda b: (b[0], b[1]))
            high, high_strict = min(upper, key=lambda b: (b[0], not b[1]))
            if low > high or (low == high and (low_strict or high_strict)):
                found.append(f"{field}: lower limit {low} is above upper limit {high}")
        if len({str(v) for v in equals}) > 1:
            found.append(f"{field}: requires several different exact values {equals}")
        for value in equals:
            if value in excluded:
                found.append(f"{field}: requires {value!r} and also excludes it")
            if isinstance(value, (int, Decimal)) and not isinstance(value, bool):
                below = any(value < b or (value == b and strict) for b, strict in lower)
                above = any(value > b or (value == b and strict) for b, strict in upper)
                if below or above:
                    found.append(f"{field}: exact value {value} violates another limit")
    return found


def evaluate(
    plugin: DomainPlugin,
    goal: str,
    constraints: list[Constraint],
    *,
    stated_conflicts: list[str] | None = None,
    out_of_scope: bool = False,
) -> IntentResult:
    """Decide ready/blocked/out_of_scope and the clarifying questions, from the plug-in's rules."""
    if out_of_scope:
        return IntentResult(status=IntentStatus.OUT_OF_SCOPE, goal=goal, constraints=tuple(constraints))

    present = {c.field_name for c in constraints if c.active}
    missing: list[str] = []
    questions: list[str] = []
    if not goal.strip():
        missing.append("goal")
        questions.append(plugin.goal_question)
    for spec in plugin.blocking_fields():
        if spec.name not in present:
            missing.append(spec.name)
            questions.append(spec.question or f"Please provide {spec.name}.")

    conflicts = [*(stated_conflicts or []), *detect_conflicts(constraints)]
    questions.extend(f"These requirements conflict: {c}. Which one should I follow?" for c in conflicts)

    blocked = bool(missing or conflicts)
    return IntentResult(
        status=IntentStatus.BLOCKED if blocked else IntentStatus.READY,
        goal=goal,
        constraints=tuple(constraints),
        missing=tuple(missing),
        conflicts=tuple(conflicts),
        questions=tuple(questions),
    )


# --------------------------------------------------------------------------- patch application


def apply_patch(ops: list[IntentPatchOp], current: list[Constraint], plugin: DomainPlugin) -> tuple[list[Constraint], list[AppliedChange]]:
    """Apply ops to ``current`` through ``Constraint.revise``; only touched constraints change.

    ``old`` values come from the current state, never from the model. The op kind is
    reconciled with reality: "update" of an absent field is an add, "add" of a present one an update.
    """
    working: dict[str, Constraint] = {c.constraint_id: c for c in current}
    changes: list[AppliedChange] = []

    def active_on(field: str, operator: Operator | None = None) -> list[Constraint]:
        rows = [c for c in working.values() if c.active and c.field_name == field]
        return [c for c in rows if operator is None or c.operator is operator]

    for op in ops:
        spec = _spec_for(plugin, op.field)
        value = _typed_value(spec, op.operator or Operator.EQ, op.value) if op.value is not None else None
        same_operator = active_on(op.field, op.operator) if op.operator else []
        on_field = active_on(op.field)

        if op.op is PatchOp.REMOVE:
            targets = same_operator or on_field
            if not targets:
                raise _Rejected(f"cannot remove {op.field!r}: there is no active constraint on it")
            for target in targets:
                working[target.constraint_id] = target.revise(
                    reason=f"user removed: {op.evidence}", changed_by=ConstraintSource.USER, active=False
                )
                changes.append(AppliedChange(op=PatchOp.REMOVE, field=op.field, old=target.target_value))
            continue

        if op.op is PatchOp.ADD:
            # An explicit add with a new operator is a new constraint (e.g. a lower bound next to an upper one).
            target = same_operator[0] if same_operator else None
        elif op.operator is None:
            target = on_field[0] if len(on_field) == 1 else None
        else:
            target = same_operator[0] if same_operator else (on_field[0] if len(on_field) == 1 else None)

        if target is None:
            if op.op is PatchOp.UPDATE and len(on_field) > 1:
                raise _Rejected(f"{op.field!r} has several constraints; name the operator to update")
            if op.operator is None or value is None:
                raise _Rejected(f"cannot add {op.field!r} without operator and value")
            try:
                created = Constraint(
                    field_name=op.field,
                    operator=op.operator,
                    target_value=value,
                    source=ConstraintSource.USER,
                    strength=op.strength or ConstraintStrength.HARD,
                )
            except ValueError as exc:
                raise _Rejected(f"constraint on {op.field!r} is invalid: {exc}") from exc
            working[created.constraint_id] = created
            changes.append(AppliedChange(op=PatchOp.ADD, field=op.field, new=created.target_value))
            continue

        try:
            revised = target.revise(
                reason=f"user update: {op.evidence}",
                changed_by=ConstraintSource.USER,
                operator=op.operator,
                target_value=value,
                strength=op.strength,
            )
        except ValueError as exc:
            raise _Rejected(f"update of {op.field!r} is invalid: {exc}") from exc
        working[target.constraint_id] = revised
        changes.append(
            AppliedChange(op=PatchOp.UPDATE, field=op.field, old=target.target_value, new=revised.target_value)
        )
    return list(working.values()), changes


# --------------------------------------------------------------------------- LLM calls


async def _call_checked[M: BaseModel, R](
    prompt: str, model: type[M], check: Callable[[M], R], provider: LLMProvider | None
) -> R:
    """Call the gateway, run ``check`` on the output, and retry once with the rejection reason."""
    current = prompt
    reason = ""
    for _ in range(2):
        output = await call(current, model, provider=provider)
        try:
            return check(output)
        except _Rejected as exc:
            reason = str(exc)
            logger.warning("intent_output_rejected", extra={"output_model": model.__name__, "reason": reason})
            current = f"{prompt}\n\nYour previous answer was rejected: {reason}. Answer again and fix this."
    raise IntentError(f"model output rejected after retry: {reason}")


def _field_lines(plugin: DomainPlugin) -> str:
    return "\n".join(f"- {f.name} ({f.value_type.value}): {f.description}" for f in plugin.fields)


def _audit_questions(audit: AuditScope, result: IntentResult) -> None:
    if result.questions:
        audit.emit(
            Phase.INTENT, "USER_CLARIFICATION_REQUESTED", node="intent", decision=result.status.value,
            metadata={"missing": list(result.missing), "conflicts": list(result.conflicts)},
        )


async def extract_intent(
    text: str, plugin: DomainPlugin, *, provider: LLMProvider | None = None, audit: AuditScope | None = None
) -> tuple[IntentResult, ConstraintDiff]:
    """Full extraction for a user's first message."""
    prompt = render(
        "intent_extract",
        domain=plugin.name,
        domain_description=plugin.description,
        fields=_field_lines(plugin),
        text=text,
    )

    def check(extraction: IntentExtraction) -> IntentResult:
        constraints = [_build_constraint(item, text, plugin) for item in extraction.constraints]
        return evaluate(
            plugin,
            extraction.goal,
            constraints,
            stated_conflicts=extraction.conflicts,
            out_of_scope=extraction.out_of_scope,
        )

    result = await _call_checked(prompt, IntentExtraction, check, provider)
    if audit:
        audit.emit(
            Phase.INTENT, "INTENT_EXTRACTED", node="intent", actor_type="agent", payload=text, decision=result.status.value,
            metadata={"domain": plugin.name, "fields": sorted(c.field_name for c in result.constraints), "missing": list(result.missing)},
        )
        _audit_questions(audit, result)
    return result, diff_constraints([], list(result.constraints))


async def patch_intent(
    text: str,
    goal: str,
    current: list[Constraint],
    plugin: DomainPlugin,
    *,
    provider: LLMProvider | None = None,
    audit: AuditScope | None = None,
) -> tuple[IntentResult, ConstraintDiff, list[AppliedChange]]:
    """Patch mode: turn a user update into operations, applied deterministically to ``current``."""
    lines = [
        f"- {c.field_name} {c.operator.value} {c.target_value} ({c.strength.value})" for c in current if c.active
    ]
    prompt = render(
        "intent_patch",
        domain=plugin.name,
        domain_description=plugin.description,
        fields=_field_lines(plugin),
        current="\n".join(lines) or "(none)",
        text=text,
    )

    def check(patch: IntentPatch) -> tuple[list[Constraint], list[AppliedChange]]:
        for op in patch.ops:
            _check_grounded(op.evidence, text)
        return apply_patch(patch.ops, current, plugin)

    updated, changes = await _call_checked(prompt, IntentPatch, check, provider)
    result = evaluate(plugin, goal, updated)
    if audit:
        audit.emit(
            Phase.INTENT, "CONSTRAINT_UPDATED", node="intent", actor_type="user", payload=text, decision=result.status.value,
            metadata={"changes": [{"op": c.op.value, "field": c.field} for c in changes]},
        )
        _audit_questions(audit, result)
    return result, diff_constraints(current, updated), changes


def to_state_update(result: IntentResult, diff: ConstraintDiff) -> dict[str, object]:
    """Partial ``TaskState`` update owned by the Intent node."""
    return {
        "goal": result.goal,
        "constraints": list(result.constraints),
        "constraint_diffs": [diff],
        "status": result.status.value,
        "questions": list(result.questions),
    }
