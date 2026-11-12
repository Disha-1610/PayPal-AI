"""The optional second LLM provider for the short, high-volume agents."""

import pytest

from app.core.config import Settings
from app.llm.providers import GeminiProvider, LLMError, OpenAICompatProvider, build_light_provider, build_provider


def _settings(**kw) -> Settings:
    return Settings(_env_file=None, llm_provider="gemini", llm_model="gemini-x", llm_api_key="primary", **kw)


def test_without_a_light_provider_every_agent_uses_the_primary():
    assert build_light_provider(_settings()) is None
    assert isinstance(build_provider(_settings()), GeminiProvider)


def test_light_provider_is_built_from_its_own_settings():
    light = build_light_provider(
        _settings(llm_light_provider="groq", llm_light_model="llama-3.3-70b-versatile", llm_light_api_key="secondary")
    )
    assert isinstance(light, OpenAICompatProvider) and light.model == "llama-3.3-70b-versatile" and light.name == "groq"


@pytest.mark.parametrize("missing", [{"llm_light_model": "m"}, {"llm_light_api_key": "k"}])
def test_light_provider_needs_a_model_and_a_key(missing):
    with pytest.raises(LLMError, match="LLM_LIGHT"):
        build_light_provider(_settings(llm_light_provider="groq", **missing))


# ---- primary -> fallback while the primary's quota is used up ------------------------------------------------

from datetime import datetime  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

from pydantic import BaseModel  # noqa: E402

from app.llm.providers import (  # noqa: E402
    FallbackProvider,
    LLMResponse,
    LLMTransientError,
    QuotaExhausted,
    seconds_until_daily_reset,
)


class _Out(BaseModel):
    ok: bool


class _Scripted:
    def __init__(self, name, *errors):
        self.name, self.model, self.calls, self._errors = name, f"{name}-model", 0, list(errors)

    async def generate(self, prompt, output_model):
        self.calls += 1
        if self._errors:
            raise self._errors.pop(0)
        return LLMResponse(text='{"ok": true}', model=self.model)


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


async def test_daily_quota_switches_to_the_fallback_until_the_reset_then_back():
    clock = _Clock()
    primary, fallback = _Scripted("gemini", QuotaExhausted("429", daily=True)), _Scripted("groq")
    llm = FallbackProvider(primary, fallback, clock=clock, until_reset=lambda: 3600)
    assert llm.name == "gemini"
    assert (await llm.generate("p", _Out)).model == "groq-model"  # the refused call is answered by the fallback
    assert llm.name == "groq"
    clock.now = 3599
    await llm.generate("p", _Out)
    assert primary.calls == 1 and fallback.calls == 2  # the primary is not retried before its reset
    clock.now = 3600
    assert (await llm.generate("p", _Out)).model == "gemini-model" and llm.name == "gemini"


async def test_per_minute_limit_uses_the_fallback_only_for_the_retry_delay():
    clock = _Clock()
    primary = _Scripted("gemini", QuotaExhausted("429", daily=False, retry_after=21))
    llm = FallbackProvider(primary, _Scripted("groq"), clock=clock, until_reset=lambda: 1e9)
    await llm.generate("p", _Out)
    clock.now = 21
    assert (await llm.generate("p", _Out)).model == "gemini-model"


async def test_other_primary_errors_are_not_masked_by_the_fallback():
    fallback = _Scripted("groq")
    llm = FallbackProvider(_Scripted("gemini", LLMTransientError("Gemini API error 503")), fallback)
    with pytest.raises(LLMTransientError):
        await llm.generate("p", _Out)
    assert fallback.calls == 0 and llm.name == "gemini"


def test_daily_reset_is_the_next_midnight_pacific():
    pacific = ZoneInfo("America/Los_Angeles")
    assert seconds_until_daily_reset(datetime(2026, 11, 12, 23, 0, tzinfo=pacific)) == 3600
    assert seconds_until_daily_reset(datetime(2026, 11, 13, 7, 30, tzinfo=ZoneInfo("UTC"))) == 30 * 60  # 23:30 PST


def test_fallback_is_built_from_settings_and_reuses_the_light_key():
    llm = build_provider(_settings(
        llm_light_provider="groq", llm_light_model="q", llm_light_api_key="groq-key",
        llm_fallback_provider="groq", llm_fallback_model="openai/gpt-oss-120b",
    ))
    assert isinstance(llm, FallbackProvider) and llm.primary.name == "gemini" and llm.fallback.model == "openai/gpt-oss-120b"
    with pytest.raises(LLMError, match="LLM_FALLBACK"):
        build_provider(_settings(llm_fallback_provider="groq", llm_fallback_model="m"))  # no key anywhere
