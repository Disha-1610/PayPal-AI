"""Never-silently-relax guard (Phase 4.5): hard-constraint changes need a user-sourced revision.

Enforced in code at the state reducer (``merge_constraints``), not in a prompt. ``source`` is never
read from model output: ``Constraint.revise`` stamps it from ``changed_by``, which only trusted
code paths (intent patching of a user message, the resume handler) pass as ``USER``.
"""

from app.schemas.constraints import Constraint, ConstraintSource, ConstraintStrength


class HardConstraintViolation(ValueError):
    """A hard constraint was changed without user authority. The caller must route to ``interrupt()``."""


def assert_revision_allowed(existing: Constraint, incoming: Constraint) -> None:
    """Raise unless ``incoming`` (the next revision of ``existing``) may replace it.

    Covers every way of relaxing a hard constraint: changing its value or operator, deactivating
    (removing) it, and downgrading it to soft. Soft constraints may be revised by the system, except
    that an inferred revision may never override one the user stated.
    """
    if incoming.source is ConstraintSource.USER:
        return
    if existing.source is ConstraintSource.USER:
        raise HardConstraintViolation(
            f"constraint {existing.constraint_id} ({existing.field_name}): "
            "inferred revision cannot override a user constraint"
        )
    if existing.strength is ConstraintStrength.HARD:
        raise HardConstraintViolation(
            f"constraint {existing.constraint_id} ({existing.field_name}): hard constraint changes "
            f"require source='user', got {incoming.source.value!r}"
        )
