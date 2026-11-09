import pytest

from app.llm import gateway
from app.llm.providers import LLMResponse


class FakeProvider:
    """Scripted LLM provider. Each step is a JSON string, an Exception, or an async callable."""

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
