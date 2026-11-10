"""Canonical JSON and stable hashing shared by audit records and approval terms."""

import hashlib
import json


def canonical_json(payload: object) -> str:
    """Deterministic JSON: sorted keys, no whitespace."""
    return json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))


def stable_hash(payload: object) -> str:
    """sha256 hex digest of ``canonical_json(payload)``."""
    return hashlib.sha256(canonical_json(payload).encode()).hexdigest()
