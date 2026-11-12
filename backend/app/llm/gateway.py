"""Single entry point for all LLM calls. Agents import ``call`` only, never an SDK."""

import asyncio
import time
from functools import lru_cache
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from app.core.config import get_settings
from app.core.logging import get_logger
from app.llm.deadline import remaining
from app.llm.providers import LLMError, LLMProvider, LLMTransientError, build_provider

T = TypeVar("T", bound=BaseModel)

logger = get_logger(__name__)

_BACKOFF_BASE_SECONDS = 0.5


class LLMGatewayError(Exception):
    """Raised when a call fails permanently or exhausts its retries."""


@lru_cache
def _default_provider() -> LLMProvider:
    return build_provider(get_settings())


async def call(
    prompt: str,
    output_model: type[T],
    *,
    retries: int = 2,
    provider: LLMProvider | None = None,
) -> T:
    """Run ``prompt`` and return a validated ``output_model`` instance.

    Transient provider errors, timeouts and schema-validation failures are retried up to
    ``retries`` times (``retries + 1`` attempts). Validation retries append the validation
    error to the prompt so the model can self-correct. Raises ``LLMGatewayError`` on failure.
    """
    if retries < 0:
        raise ValueError("retries must be >= 0")
    try:
        llm = provider or _default_provider()
    except LLMError as exc:
        raise LLMGatewayError(str(exc)) from exc

    timeout = get_settings().llm_timeout_seconds
    attempts = retries + 1
    current_prompt = prompt
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        left = remaining()
        if left is not None and left <= 0:
            logger.warning("llm_call_budget_exhausted", extra={"output_model": output_model.__name__})
            raise LLMGatewayError("run LLM time budget exhausted")
        attempt_timeout = timeout if left is None else min(timeout, left)
        started = time.perf_counter()
        meta = {
            "provider": llm.name,
            "model": llm.model,
            "output_model": output_model.__name__,
            "attempt": attempt,
            "prompt_chars": len(current_prompt),
        }
        try:
            response = await asyncio.wait_for(llm.generate(current_prompt, output_model), attempt_timeout)
            meta.update(
                latency_ms=round((time.perf_counter() - started) * 1000),
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
            )
            try:
                result = output_model.model_validate_json(response.text)
            except ValidationError as exc:
                logger.warning("llm_call_invalid_output", extra={**meta, "errors": exc.error_count()})
                last_error = exc
                current_prompt = _with_correction(prompt, exc)
                await _backoff(attempt, attempts)
                continue
            logger.info("llm_call", extra=meta)
            return result
        except asyncio.TimeoutError as exc:
            logger.warning("llm_call_timeout", extra={**meta, "timeout_seconds": timeout})
            last_error = exc
        except LLMTransientError as exc:
            logger.warning("llm_call_transient_error", extra={**meta, "error": str(exc)})
            last_error = exc
        except LLMError as exc:
            logger.error("llm_call_failed", extra={**meta, "error": str(exc)})
            raise LLMGatewayError(str(exc)) from exc
        except Exception as exc:  # SDK-side bugs/schema rejections: one error contract for every agent
            logger.error("llm_call_provider_exception", extra={**meta, "error": f"{type(exc).__name__}: {exc}"})
            raise LLMGatewayError(f"{llm.name} provider failed: {type(exc).__name__}") from exc
        await _backoff(attempt, attempts)

    logger.error("llm_call_exhausted", extra={"output_model": output_model.__name__, "attempts": attempts})
    raise LLMGatewayError(
        f"LLM call for {output_model.__name__} failed after {attempts} attempts"
    ) from last_error


def _with_correction(prompt: str, exc: ValidationError) -> str:
    details = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
    return (
        f"{prompt}\n\nYour previous response did not match the required schema ({details}). "
        "Return only JSON that satisfies the schema."
    )


async def _backoff(attempt: int, attempts: int) -> None:
    if attempt < attempts:
        await asyncio.sleep(_BACKOFF_BASE_SECONDS * 2 ** (attempt - 1))
