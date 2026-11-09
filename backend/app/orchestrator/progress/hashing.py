"""Progress hash (4.7): a stable fingerprint of "what we are searching with and what we found".

``hashlib`` over canonical JSON, never ``hash()``: Python salts ``hash()`` per process, which would
break comparisons across a checkpoint resume. Scores, timestamps and prices are excluded on purpose:
only the candidate ids and the active constraints decide whether an attempt made progress.
"""

import hashlib
import json
from collections.abc import Iterable
from decimal import Decimal

from app.schemas.constraints import Constraint


def _canonical_value(value: object) -> object:
    if isinstance(value, bool):
        return value
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, list):
        return sorted((_canonical_value(v) for v in value), key=repr)
    return value


def _canonical_constraint(constraint: Constraint) -> list[object]:
    return [
        constraint.field_name,
        constraint.operator.value,
        _canonical_value(constraint.target_value),
        constraint.strength.value,
    ]


def progress_hash(candidate_ids: Iterable[str], constraints: Iterable[Constraint]) -> str:
    payload = {
        "candidates": sorted(set(candidate_ids)),
        "constraints": sorted((_canonical_constraint(c) for c in constraints if c.active), key=repr),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
