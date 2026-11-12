import httpx
import pytest
from pydantic import BaseModel

from app.llm.providers import LLMError, LLMTransientError, OpenAICompatProvider


class Out(BaseModel):
    a: int


def provider(handler) -> OpenAICompatProvider:
    return OpenAICompatProvider(
        "groq", "k", "m", "https://x/v1", http=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )


async def test_returns_content_and_usage():
    def ok(request):
        assert request.url.path == "/v1/chat/completions"
        return httpx.Response(200, json={
            "choices": [{"message": {"content": '{"a": 1}'}}], "usage": {"prompt_tokens": 3, "completion_tokens": 2},
        })

    res = await provider(ok).generate("hi", Out)
    assert (res.text, res.input_tokens, res.output_tokens) == ('{"a": 1}', 3, 2)


@pytest.mark.parametrize("status,exc", [(429, LLMTransientError), (503, LLMTransientError), (401, LLMError)])
async def test_http_errors_are_classified(status, exc):
    with pytest.raises(exc) as info:
        await provider(lambda r: httpx.Response(status)).generate("hi", Out)
    assert (info.type is LLMTransientError) == (exc is LLMTransientError)


async def test_malformed_response_is_transient():
    with pytest.raises(LLMTransientError):
        await provider(lambda r: httpx.Response(200, json={"choices": []})).generate("hi", Out)
