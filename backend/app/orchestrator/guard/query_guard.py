"""Guard for LLM-proposed query rewrites (Discovery, step 5).

A rewrite only ever replaces the query *text*. Filters are rebuilt from the user's constraints by
code, so a rewrite cannot loosen a hard constraint; this guard keeps the text itself clean: short,
plain words, nothing already tried, and nothing the user excluded.
"""

import re
from collections.abc import Collection, Iterable

from app.schemas.constraints import Constraint, ConstraintStrength, Operator

MAX_WORDS = 8
MAX_CHARS = 80
_ALLOWED = re.compile(r"^[\w\s\-'&.,/]+$")  # no markup, brackets, quotes, colons or newlines


class QueryRejected(ValueError):
    """The proposed query failed a deterministic check."""


def normalize_query(text: str) -> str:
    return " ".join(text.lower().split())


def excluded_terms(constraints: Iterable[Constraint]) -> list[str]:
    terms: list[str] = []
    for c in constraints:
        if c.active and c.strength is ConstraintStrength.HARD and c.operator in (Operator.NE, Operator.NOT_IN):
            values = c.target_value if isinstance(c.target_value, list) else [c.target_value]
            terms.extend(normalize_query(v) for v in values if isinstance(v, str))
    return terms


def validate_rewrite(proposed: str, *, tried: Collection[str], constraints: Iterable[Constraint]) -> str:
    """Return the normalised query, or raise ``QueryRejected`` with the reason. ``tried`` holds normalised texts."""
    query = normalize_query(proposed)
    if not query:
        raise QueryRejected("empty query")
    if len(query) > MAX_CHARS or len(query.split()) > MAX_WORDS:
        raise QueryRejected(f"query is longer than {MAX_WORDS} words / {MAX_CHARS} characters")
    if not _ALLOWED.fullmatch(query):
        raise QueryRejected("query contains characters other than plain words")
    if query in tried:
        raise QueryRejected("query was already tried")
    for term in excluded_terms(constraints):
        if term and re.search(rf"\b{re.escape(term)}\b", query):
            raise QueryRejected(f"query contains {term!r}, which the user excluded")
    return query
