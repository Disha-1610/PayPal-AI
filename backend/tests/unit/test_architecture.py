"""Payment integrity: LLM-facing orchestrator code must not touch the payment ledger."""

import re
from pathlib import Path

ORCHESTRATOR = Path(__file__).resolve().parents[2] / "app" / "orchestrator"
FORBIDDEN = re.compile(r"^\s*(from|import)\s+app\.(db\.models\.payment|payments\.ledger)\b", re.MULTILINE)

# Untrusted connector text (rule 2.5) must never reach LLM-facing code: only ids and aggregates may.
UNTRUSTED_TEXT = re.compile(
    r"^\s*(from|import)\s+app\.(db\.models\.connector_text|db\.repositories\.connector_text|connectors\.base\.models)\b",
    re.MULTILINE,
)


def test_orchestrator_does_not_import_payment_ledger():
    offenders = [str(p) for p in ORCHESTRATOR.rglob("*.py") if FORBIDDEN.search(p.read_text(encoding="utf-8"))]
    assert offenders == []


def test_orchestrator_does_not_import_untrusted_connector_text():
    offenders = [str(p) for p in ORCHESTRATOR.rglob("*.py") if UNTRUSTED_TEXT.search(p.read_text(encoding="utf-8"))]
    assert offenders == []


def test_candidate_state_model_holds_no_free_text():
    from app.schemas.candidate import Candidate

    assert not {"description", "reviews", "review_text", "body"} & set(Candidate.model_fields)
    assert Candidate.model_fields["text_refs"].annotation == tuple[str, ...]
