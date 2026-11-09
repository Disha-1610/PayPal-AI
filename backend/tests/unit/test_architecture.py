"""Payment integrity: LLM-facing orchestrator code must not touch the payment ledger."""

import re
from pathlib import Path

ORCHESTRATOR = Path(__file__).resolve().parents[2] / "app" / "orchestrator"
FORBIDDEN = re.compile(r"^\s*(from|import)\s+app\.(db\.models\.payment|payments\.ledger)\b", re.MULTILINE)


def test_orchestrator_does_not_import_payment_ledger():
    offenders = [str(p) for p in ORCHESTRATOR.rglob("*.py") if FORBIDDEN.search(p.read_text(encoding="utf-8"))]
    assert offenders == []
