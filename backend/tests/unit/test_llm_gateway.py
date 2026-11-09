import asyncio

import pytest
from pydantic import BaseModel

from app.llm import gateway
from app.llm.gateway import LLMGatewayError, call
from app.llm.providers import LLMError, LLMResponse, LLMTransientError


class Out(BaseModel):
    answer: int


class FakeProvider:
    name = "fake"
    model = "fake-1"

    def __init__(self, *steps):
        self.steps = list(steps)
        self.prompts: list[str] = []

    async def generate(self, prompt, output_model):
        self.prompts.append(prompt)
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        if callable(step):
            return await step()
        return LLMResponse(text=step, model=self.model, input_tokens=5, output_tokens=3)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def instant(_):
        return None

    monkeypatch.setattr(gateway.asyncio, "sleep", instant)


async def test_returns_validated_model():
    p = FakeProvider('{"answer": 42}')
    assert await call("q", Out, provider=p) == Out(answer=42)


async def test_retries_transient_error_then_succeeds():
    p = FakeProvider(LLMTransientError("429"), '{"answer": 1}')
    assert (await call("q", Out, provider=p)).answer == 1
    assert len(p.prompts) == 2


async def test_invalid_output_retried_with_correction():
    p = FakeProvider('{"answer": "x"}', '{"answer": 2}')
    assert (await call("q", Out, provider=p)).answer == 2
    assert "did not match the required schema" in p.prompts[1]
    assert p.prompts[1].startswith("q")


async def test_exhausted_retries_raise():
    p = FakeProvider("bad", "bad", "bad")
    with pytest.raises(LLMGatewayError):
        await call("q", Out, provider=p)
    assert len(p.prompts) == 3


async def test_retries_zero_means_single_attempt():
    p = FakeProvider(LLMTransientError("503"))
    with pytest.raises(LLMGatewayError):
        await call("q", Out, retries=0, provider=p)


async def test_non_transient_error_not_retried():
    p = FakeProvider(LLMError("401"), '{"answer": 1}')
    with pytest.raises(LLMGatewayError):
        await call("q", Out, provider=p)
    assert len(p.prompts) == 1


async def test_timeout_is_retried(monkeypatch):
    monkeypatch.setattr(gateway.get_settings(), "llm_timeout_seconds", 0.01)

    async def hang():
        await asyncio.Event().wait()

    p = FakeProvider(hang, '{"answer": 3}')
    assert (await call("q", Out, provider=p)).answer == 3


async def test_logs_token_usage(caplog):
    caplog.set_level("INFO", logger="app.llm.gateway")
    await call("q", Out, provider=FakeProvider('{"answer": 1}'))
    rec = next(r for r in caplog.records if r.message == "llm_call")
    assert (rec.input_tokens, rec.output_tokens, rec.model) == (5, 3, "fake-1")


async def test_missing_api_key_raises_gateway_error(monkeypatch):
    gateway._default_provider.cache_clear()
    monkeypatch.setattr(gateway.get_settings(), "llm_api_key", None)
    with pytest.raises(LLMGatewayError):
        await call("q", Out)
