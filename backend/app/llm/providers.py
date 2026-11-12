"""Provider adapters behind the gateway. Only this module may import an LLM SDK."""

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

import httpx

from pydantic import BaseModel

from app.core.config import Settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class LLMError(Exception):
    """Non-retryable provider failure (auth, bad request, misconfiguration)."""


class LLMTransientError(LLMError):
    """Retryable provider failure (rate limit, 5xx, network)."""


class QuotaExhausted(LLMTransientError):
    """The provider refused for quota: ``daily`` until its daily reset, else for about ``retry_after`` seconds."""

    def __init__(self, message: str, *, daily: bool, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.daily = daily
        self.retry_after = retry_after


@dataclass(frozen=True)
class LLMResponse:
    text: str
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None


class LLMProvider(Protocol):
    name: str
    model: str

    async def generate(self, prompt: str, output_model: type[BaseModel]) -> LLMResponse:
        """Return the raw JSON text produced for ``output_model``; never validate here."""
        ...


_TRANSIENT_HTTP_CODES = {408, 429, 500, 502, 503, 504}
_RETRY_DELAY = re.compile(r"retryDelay\W+(\d+(?:\.\d+)?)s")  # e.g. 'retryDelay': '21s'


class GeminiProvider:
    name = "gemini"

    def __init__(self, api_key: str, model: str, *, thinking_budget: int = 0, temperature: float = 0.0) -> None:
        from google import genai  # imported lazily so other providers don't need the SDK

        self.model = model
        self._thinking_budget = thinking_budget
        self._temperature = temperature
        self._client = genai.Client(api_key=api_key)
        # Output models the SDK's ``response_schema`` converter rejects (e.g. ``dict[str, str]`` fields produce
        # ``additionalProperties``, unsupported in Developer API mode); these are sent as ``response_json_schema``.
        self._json_schema_models: set[type[BaseModel]] = set()

    def _config(self, output_model: type[BaseModel]) -> object:
        from google.genai import types

        schema: dict[str, object] = (
            {"response_json_schema": output_model.model_json_schema()}
            if output_model in self._json_schema_models
            else {"response_schema": output_model}
        )
        return types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=self._temperature,
            thinking_config=types.ThinkingConfig(thinking_budget=self._thinking_budget),
            **schema,
        )

    async def generate(self, prompt: str, output_model: type[BaseModel]) -> LLMResponse:
        from google.genai import errors

        try:
            try:
                result = await self._client.aio.models.generate_content(
                    model=self.model, contents=prompt, config=self._config(output_model)
                )
            except ValueError:
                if output_model in self._json_schema_models:
                    raise
                # Client-side schema conversion failed before any request was sent: retry once with plain JSON Schema.
                # The gateway still validates the answer against ``output_model``.
                self._json_schema_models.add(output_model)
                result = await self._client.aio.models.generate_content(
                    model=self.model, contents=prompt, config=self._config(output_model)
                )
        except errors.APIError as exc:
            if exc.code == 429:
                detail = str(exc)
                delay = _RETRY_DELAY.search(detail)
                raise QuotaExhausted(
                    "Gemini API error 429", daily="PerDay" in detail, retry_after=float(delay.group(1)) if delay else None
                ) from exc
            if exc.code in _TRANSIENT_HTTP_CODES:
                raise LLMTransientError(f"Gemini API error {exc.code}") from exc
            if "API_KEY_INVALID" in str(exc):
                raise LLMError("Gemini API key is invalid; set a valid LLM_API_KEY in backend/.env") from exc
            raise LLMError(f"Gemini API error {exc.code}") from exc
        except OSError as exc:  # connection resets / DNS failures
            raise LLMTransientError("Gemini network error") from exc

        usage = result.usage_metadata
        return LLMResponse(
            text=result.text or "",
            model=self.model,
            input_tokens=usage.prompt_token_count if usage else None,
            output_tokens=usage.candidates_token_count if usage else None,
        )


class OpenAICompatProvider:
    """Any OpenAI-compatible chat-completions endpoint (Groq, Cerebras, OpenRouter, Ollama...)."""

    def __init__(
        self,
        name: str,
        api_key: str,
        model: str,
        base_url: str,
        *,
        temperature: float = 0.0,
        timeout: float = 12.0,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.name = name
        self.model = model
        self._temperature = temperature
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._headers = {"Authorization": f"Bearer {api_key}"}
        # httpx defaults to 5s, which would cut every answer shorter than the gateway's per-attempt budget.
        # The gateway's ``wait_for`` remains the outer bound; this only keeps the transport from expiring first.
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=min(timeout, 5.0)))

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def generate(self, prompt: str, output_model: type[BaseModel]) -> LLMResponse:
        schema = json.dumps(output_model.model_json_schema())
        body = {
            "model": self.model,
            "temperature": self._temperature,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": f"Reply with a single JSON object that matches this JSON schema: {schema}"},
                {"role": "user", "content": prompt},
            ],
        }
        try:
            response = await self._http.post(self._url, json=body, headers=self._headers)
        except httpx.HTTPError as exc:  # timeouts, connection resets, DNS failures
            raise LLMTransientError(f"{self.name} network error") from exc
        if response.status_code != 200:
            error = f"{self.name} API error {response.status_code}"
            raise (LLMTransientError if response.status_code in _TRANSIENT_HTTP_CODES else LLMError)(error)
        try:
            data = response.json()
            text = data["choices"][0]["message"]["content"] or ""
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMTransientError(f"{self.name} returned a malformed response") from exc
        usage = data.get("usage") or {}
        return LLMResponse(
            text=text, model=self.model, input_tokens=usage.get("prompt_tokens"), output_tokens=usage.get("completion_tokens")
        )


_GEMINI_QUOTA_ZONE = ZoneInfo("America/Los_Angeles")  # Gemini's daily quotas reset at midnight Pacific time
_DEFAULT_COOLDOWN_SECONDS = 60.0


def seconds_until_daily_reset(now: datetime | None = None) -> float:
    """Seconds until the next midnight Pacific time (when Gemini's per-day quota resets)."""
    local = (now or datetime.now(_GEMINI_QUOTA_ZONE)).astimezone(_GEMINI_QUOTA_ZONE)
    midnight = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max((midnight - local).total_seconds(), 1.0)


class FallbackProvider:
    """Use ``primary`` until it reports its quota is used up, then ``fallback`` until that quota resets.

    A daily quota sends calls to the fallback until the primary's daily reset; a per-minute one only until the
    retry delay the primary asked for. Any other primary error is raised as usual (the gateway retries it).
    """

    def __init__(
        self,
        primary: LLMProvider,
        fallback: LLMProvider,
        *,
        clock: Callable[[], float] = time.monotonic,
        until_reset: Callable[[], float] = seconds_until_daily_reset,
    ) -> None:
        self.primary, self.fallback = primary, fallback
        self._clock, self._until_reset = clock, until_reset
        self._resume_at = 0.0

    def _active(self) -> LLMProvider:
        return self.primary if self._clock() >= self._resume_at else self.fallback

    @property
    def name(self) -> str:  # the gateway logs the provider actually in use
        return self._active().name

    @property
    def model(self) -> str:
        return self._active().model

    async def generate(self, prompt: str, output_model: type[BaseModel]) -> LLMResponse:
        if self._active() is self.primary:
            try:
                return await self.primary.generate(prompt, output_model)
            except QuotaExhausted as exc:
                pause = self._until_reset() if exc.daily else (exc.retry_after or _DEFAULT_COOLDOWN_SECONDS)
                self._resume_at = self._clock() + pause
                logger.warning(
                    "llm_primary_quota_exhausted",
                    extra={
                        "primary": self.primary.name, "fallback": self.fallback.name, "daily": exc.daily,
                        "pause_seconds": round(pause),
                    },
                )
        return await self.fallback.generate(prompt, output_model)

    async def aclose(self) -> None:
        for llm in (self.primary, self.fallback):
            close = getattr(llm, "aclose", None)
            if close is not None:
                await close()


_OPENAI_COMPAT_BASE_URLS = {
    "groq": "https://api.groq.com/openai/v1",
    "cerebras": "https://api.cerebras.ai/v1",
    "openrouter": "https://openrouter.ai/api/v1",
}


def _build(settings: Settings, *, provider: str, model: str, key: str, base_url: str | None, env: str) -> LLMProvider:
    if provider == "gemini":
        return GeminiProvider(
            api_key=key, model=model, thinking_budget=settings.llm_thinking_budget, temperature=settings.llm_temperature,
        )
    if provider in _OPENAI_COMPAT_BASE_URLS or provider == "openai_compat":
        url = base_url or _OPENAI_COMPAT_BASE_URLS.get(provider)
        if not url:
            raise LLMError(f"{env}_BASE_URL is required for the openai_compat provider")
        return OpenAICompatProvider(
            provider, key, model, url, temperature=settings.llm_temperature, timeout=settings.llm_timeout_seconds,
        )
    raise LLMError(f"Unsupported LLM provider: {provider!r}")


def build_provider(settings: Settings) -> LLMProvider:
    """The primary provider; with ``LLM_FALLBACK_PROVIDER`` set, it hands over to the fallback while its quota is used up."""
    if settings.llm_api_key is None:
        raise LLMError("LLM_API_KEY is not configured")
    primary = _build(
        settings, provider=settings.llm_provider, model=settings.llm_model, key=settings.llm_api_key.get_secret_value(),
        base_url=settings.llm_base_url, env="LLM",
    )
    if settings.llm_fallback_provider is None:
        return primary
    key = settings.llm_fallback_api_key or (
        settings.llm_light_api_key if settings.llm_light_provider == settings.llm_fallback_provider else None
    )
    if key is None or not settings.llm_fallback_model:
        raise LLMError(
            "LLM_FALLBACK_PROVIDER needs LLM_FALLBACK_MODEL and LLM_FALLBACK_API_KEY (or a matching LLM_LIGHT_API_KEY)"
        )
    fallback = _build(
        settings, provider=settings.llm_fallback_provider, model=settings.llm_fallback_model, key=key.get_secret_value(),
        base_url=settings.llm_fallback_base_url, env="LLM_FALLBACK",
    )
    return FallbackProvider(primary, fallback)


def build_light_provider(settings: Settings) -> LLMProvider | None:
    """The provider for the short, high-volume agents, or None to use the primary one for everything."""
    if settings.llm_light_provider is None:
        return None
    if settings.llm_light_api_key is None or not settings.llm_light_model:
        raise LLMError("LLM_LIGHT_PROVIDER needs LLM_LIGHT_MODEL and LLM_LIGHT_API_KEY")
    return _build(
        settings, provider=settings.llm_light_provider, model=settings.llm_light_model,
        key=settings.llm_light_api_key.get_secret_value(), base_url=settings.llm_light_base_url, env="LLM_LIGHT",
    )
