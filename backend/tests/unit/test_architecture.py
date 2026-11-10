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


# Agents never call each other: all routing goes through the Router / Coordinator (rule 2). An agent
# package may import its own modules and shared deterministic code (guard, router, schemas, ...), but not
# another agent package. Known legacy edges are listed so the check still catches every NEW one.
AGENTS = ORCHESTRATOR / "agents"
LEGACY_AGENT_IMPORTS = {
    ("adapt", "evaluation"),  # pure constraint-evaluation helpers, shared by diagnosis and relaxation
    ("adapt", "intent"),  # resolve_decision calls patch_intent directly: a real agent-to-agent call
}
_AGENT_IMPORT = re.compile(r"^\s*(?:from|import)\s+app\.orchestrator\.agents\.(\w+)", re.MULTILINE)


def _agent_edges() -> set[tuple[str, str]]:
    edges: set[tuple[str, str]] = set()
    for package in (p for p in AGENTS.iterdir() if p.is_dir() and not p.name.startswith("__")):
        for module in package.rglob("*.py"):
            edges |= {(package.name, m) for m in _AGENT_IMPORT.findall(module.read_text(encoding="utf-8"))}
    return {(a, b) for a, b in edges if a != b}


def test_agents_do_not_import_other_agents():
    assert _agent_edges() - LEGACY_AGENT_IMPORTS == set()


def test_legacy_agent_import_list_has_no_stale_entries():
    assert LEGACY_AGENT_IMPORTS <= _agent_edges()


def test_discovery_agent_is_independent_of_every_other_agent():
    assert [edge for edge in _agent_edges() if "discovery" in edge] == []
