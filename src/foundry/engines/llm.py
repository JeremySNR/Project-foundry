"""Structured-LLM abstraction for the intelligence engines.

The LLM-backed engines depend only on :class:`StructuredLLM` - "given a system
prompt, a user prompt and a JSON schema, return a parsed JSON object". This keeps
the engines testable with :class:`FakeStructuredLLM` (no key, no network) and
isolates every OpenAI SDK detail in :class:`OpenAIStructuredLLM`, the one place
to update if the SDK or model changes.
"""

from __future__ import annotations

import json
from typing import Any, Protocol


class LLMError(RuntimeError):
    """Raised when the LLM call or its response cannot be used."""


class StructuredLLM(Protocol):
    def generate(
        self, *, system: str, user: str, schema: dict[str, Any], schema_name: str
    ) -> dict[str, Any]: ...


class FakeStructuredLLM:
    """Test double that returns canned responses and records the prompts it saw."""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def generate(
        self, *, system: str, user: str, schema: dict[str, Any], schema_name: str
    ) -> dict[str, Any]:
        self.calls.append({"system": system, "user": user, "schema_name": schema_name})
        if not self._responses:
            raise LLMError("FakeStructuredLLM ran out of canned responses")
        return self._responses.pop(0)


class OpenAIStructuredLLM:
    """OpenAI (GPT-5.5) structured-output backend via the Chat Completions API.

    The client is injected so this stays unit-testable; when omitted it is created
    lazily from the environment (``OPENAI_API_KEY``), which is why ``openai`` is an
    optional dependency rather than a hard one.
    """

    DEFAULT_MODEL = "gpt-5.5"

    def __init__(
        self,
        *,
        client: Any | None = None,
        model: str = DEFAULT_MODEL,
        strict: bool = False,
    ) -> None:
        self._client = client
        self._model = model
        # strict=True asks OpenAI to enforce the schema; leave off until the
        # Pydantic-derived schema has been tuned for strict mode.
        self._strict = strict

    def _ensure_client(self) -> Any:
        if self._client is None:  # pragma: no cover - requires the SDK + a key
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise LLMError(
                    "openai is not installed; install the 'llm' extra to use "
                    "OpenAIStructuredLLM"
                ) from exc
            self._client = OpenAI()
        return self._client

    def generate(
        self, *, system: str, user: str, schema: dict[str, Any], schema_name: str
    ) -> dict[str, Any]:
        client = self._ensure_client()
        try:
            response = client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": schema_name,
                        "schema": schema,
                        "strict": self._strict,
                    },
                },
            )
        except Exception as exc:
            # Callers' degrade-to-floor contracts catch LLMError only; a raw
            # RateLimitError/APITimeoutError/connection error must not leak
            # past this seam (it would abort intake or PR-event processing).
            raise LLMError(f"OpenAI API call failed: {exc}") from exc
        content = response.choices[0].message.content
        if not content:
            raise LLMError("OpenAI returned an empty response")
        try:
            return json.loads(content)
        except json.JSONDecodeError as exc:
            raise LLMError(f"OpenAI response was not valid JSON: {exc}") from exc
