"""Provider adapters behind the gateway. Only this module may import an LLM SDK."""

from dataclasses import dataclass
from typing import Protocol

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

    def __init__(self, api_key: str, model: str) -> None:
        from google import genai  # imported lazily so other providers don't need the SDK

        self.model = model
        self._client = genai.Client(api_key=api_key)

    async def generate(self, prompt: str, output_model: type[BaseModel]) -> LLMResponse:
        from google.genai import errors, types

        try:
            result = await self._client.aio.models.generate_content(
                model=self.model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=output_model,
                ),
            )
        except errors.APIError as exc:
            if exc.code in _TRANSIENT_HTTP_CODES:
                raise LLMTransientError(f"Gemini API error {exc.code}") from exc
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


def build_provider(settings: Settings) -> LLMProvider:
    if settings.llm_api_key is None:
        raise LLMError("LLM_API_KEY is not configured")
    key = settings.llm_api_key.get_secret_value()
    if settings.llm_provider == "gemini":
        return GeminiProvider(api_key=key, model=settings.llm_model)
    raise LLMError(f"Unsupported LLM provider: {settings.llm_provider!r}")
