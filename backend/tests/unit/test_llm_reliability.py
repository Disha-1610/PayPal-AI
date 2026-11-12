"""Phase 2.1: one gateway error contract, Gemini schema fallback, transport timeouts, no retry multiplication."""

import json
from types import SimpleNamespace

import httpx
import pytest
from conftest import FakeProvider

from app.domains.shopping import SHOPPING
from app.llm import gateway
from app.llm.gateway import LLMGatewayError
from app.llm.providers import GeminiProvider, LLMError, OpenAICompatProvider
from app.orchestrator.agents.intent.agent import IntentError, _check_grounded, extract_intent
from app.schemas.intent import IntentExtraction
from app.schemas.negotiator import NegotiatorDecision


async def test_unexpected_provider_exception_becomes_gateway_error():
    provider = FakeProvider(ValueError("schema conversion failed"))
    with pytest.raises(LLMGatewayError, match="provider failed: ValueError"):
        await gateway.call("x", IntentExtraction, provider=provider)
    assert len(provider.prompts) == 1  # not retried: it is not transient


async def test_invalid_credentials_are_not_retried():
    provider = FakeProvider(LLMError("Gemini API error 400"), "{}")
    with pytest.raises(LLMGatewayError):
        await gateway.call("x", IntentExtraction, provider=provider)
    assert len(provider.prompts) == 1


async def test_provider_timeout_is_retried_then_reported():
    async def hang():
        import asyncio

        await asyncio.Event().wait()  # never answers (conftest makes asyncio.sleep instant)

    provider = FakeProvider(hang, hang)
    gateway.get_settings().llm_timeout_seconds, old = 0.01, gateway.get_settings().llm_timeout_seconds
    try:
        with pytest.raises(LLMGatewayError, match="2 attempts"):
            await gateway.call("x", IntentExtraction, provider=provider, retries=1)
    finally:
        gateway.get_settings().llm_timeout_seconds = old


async def test_invalid_structured_output_is_rejected_not_accepted():
    provider = FakeProvider('{"goal": 5}', '{"nope": true')
    with pytest.raises(LLMGatewayError):
        await gateway.call("x", IntentExtraction, provider=provider, retries=1)


async def test_gemini_falls_back_to_json_schema_when_sdk_rejects_the_model():
    provider = GeminiProvider(api_key="test-key", model="gemini-test")
    seen: list[str] = []

    async def generate_content(*, model, contents, config):
        if config.response_schema is not None:
            seen.append("response_schema")
            raise ValueError("additionalProperties is only supported in ... mode")
        seen.append("response_json_schema")
        assert config.response_json_schema == NegotiatorDecision.model_json_schema()
        return SimpleNamespace(text='{"x": 1}', usage_metadata=None)

    provider._client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate_content)))
    await provider.generate("p", NegotiatorDecision)
    await provider.generate("p", NegotiatorDecision)  # remembered: straight to JSON Schema
    assert seen == ["response_schema", "response_json_schema", "response_json_schema"]


def test_real_gemini_config_accepts_the_negotiator_schema_as_json_schema():
    provider = GeminiProvider(api_key="test-key", model="gemini-test")
    provider._json_schema_models.add(NegotiatorDecision)
    config = provider._config(NegotiatorDecision)
    assert config.response_schema is None and "properties" in config.response_json_schema


async def test_openai_compat_transport_timeout_matches_gateway_and_closes():
    provider = OpenAICompatProvider("groq", "k", "m", "https://example.test/v1", timeout=12.0)
    assert provider._http.timeout.read == 12.0
    await provider.aclose()
    assert provider._http.is_closed
    shared = httpx.AsyncClient()
    borrowed = OpenAICompatProvider("groq", "k", "m", "https://example.test/v1", http=shared)
    await borrowed.aclose()
    assert not shared.is_closed  # never closes a client it does not own
    await shared.aclose()


async def test_intent_rejections_cost_two_calls_not_four():
    bad = json.dumps({"goal": "shoes", "constraints": [
        {"field": "budget", "operator": "lte", "value": 50, "strength": "hard", "evidence": "never said"}
    ]})
    provider = FakeProvider(bad, bad, bad, bad)
    with pytest.raises(IntentError):
        await extract_intent("running shoes under 50", SHOPPING, provider=provider)
    assert len(provider.prompts) == 2


def test_evidence_grounding_ignores_case_and_edge_punctuation_only():
    _check_grounded("budget 5000.00 usd", "Running shoes. Budget: 5000.00 USD.")
    with pytest.raises(Exception):
        _check_grounded("budget 6000", "Running shoes. Budget: 5000.00 USD.")
