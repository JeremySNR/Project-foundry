"""Tests for the OpenAIStructuredLLM seam using an injected fake client.

The load-bearing contract (issue #12): every failure mode of the SDK call -
rate limits, timeouts, connection errors - must surface as LLMError, because
the degrade-to-floor contracts in the analyzer and risk classifiers catch
LLMError only. A raw SDK exception leaking through this seam aborts intake or
PR-event processing.
"""

from __future__ import annotations

import pytest

from foundry.engines import LLMError
from foundry.engines.llm import OpenAIStructuredLLM


class _FakeMessage:
    def __init__(self, content: str | None) -> None:
        self.content = content


class _FakeChoice:
    def __init__(self, content: str | None) -> None:
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content: str | None) -> None:
        self.choices = [_FakeChoice(content)]


class _FakeCompletions:
    """Stands in for client.chat.completions: canned content or a raised error."""

    def __init__(self, *, content: str | None = None, error: Exception | None = None):
        self._content = content
        self._error = error
        self.calls: list[dict] = []

    def create(self, **kwargs) -> _FakeResponse:
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return _FakeResponse(self._content)


class _FakeChat:
    def __init__(self, completions: _FakeCompletions) -> None:
        self.completions = completions


class _FakeClient:
    def __init__(self, completions: _FakeCompletions) -> None:
        self.chat = _FakeChat(completions)


def _generate(completions: _FakeCompletions) -> dict:
    llm = OpenAIStructuredLLM(client=_FakeClient(completions))
    return llm.generate(
        system="sys", user="usr", schema={"type": "object"}, schema_name="Thing"
    )


def test_valid_json_is_parsed_and_request_is_structured() -> None:
    completions = _FakeCompletions(content='{"answer": 42}')
    assert _generate(completions) == {"answer": 42}
    request = completions.calls[0]
    assert request["messages"][0] == {"role": "system", "content": "sys"}
    assert request["response_format"]["json_schema"]["name"] == "Thing"


def test_sdk_exception_is_wrapped_as_llm_error() -> None:
    # Simulates openai.RateLimitError / APITimeoutError / connection errors:
    # any exception from the SDK call must come out as LLMError.
    cause = RuntimeError("429 Too Many Requests")
    completions = _FakeCompletions(error=cause)
    with pytest.raises(LLMError) as excinfo:
        _generate(completions)
    assert excinfo.value.__cause__ is cause


def test_empty_response_raises_llm_error() -> None:
    with pytest.raises(LLMError, match="empty"):
        _generate(_FakeCompletions(content=""))


def test_non_json_response_raises_llm_error() -> None:
    with pytest.raises(LLMError, match="not valid JSON"):
        _generate(_FakeCompletions(content="certainly! here is the JSON:"))
