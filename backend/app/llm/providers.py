"""Provider adapters behind the gateway. Only this module may import an LLM SDK."""

import json
from dataclasses import dataclass
from typing import Protocol

import httpx

from pydantic import BaseModel

from app.core.config import Settings


class LLMError(Exception):
    """Non-retryable provider failure (auth, bad request, misconfiguration)."""


class LLMTransientError(LLMError):
    """Retryable provider failure (rate limit, 5xx, network)."""


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


_OPENAI_COMPAT_BASE_URLS = {
    "groq": "https://api.groq.com/openai/v1",
    "cerebras": "https://api.cerebras.ai/v1",
    "openrouter": "https://openrouter.ai/api/v1",
}


def build_provider(settings: Settings) -> LLMProvider:
    if settings.llm_api_key is None:
        raise LLMError("LLM_API_KEY is not configured")
    key = settings.llm_api_key.get_secret_value()
    if settings.llm_provider == "gemini":
        return GeminiProvider(
            api_key=key,
            model=settings.llm_model,
            thinking_budget=settings.llm_thinking_budget,
            temperature=settings.llm_temperature,
        )
    if settings.llm_provider in _OPENAI_COMPAT_BASE_URLS or settings.llm_provider == "openai_compat":
        base_url = settings.llm_base_url or _OPENAI_COMPAT_BASE_URLS.get(settings.llm_provider)
        if not base_url:
            raise LLMError("LLM_BASE_URL is required for the openai_compat provider")
        return OpenAICompatProvider(
            settings.llm_provider, key, settings.llm_model, base_url, temperature=settings.llm_temperature,
            timeout=settings.llm_timeout_seconds,
        )
    raise LLMError(f"Unsupported LLM provider: {settings.llm_provider!r}")
