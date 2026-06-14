"""Observability helpers must be safe with OpenTelemetry absent (no-op)."""

from __future__ import annotations

import pytest

from foundry.observability import span, traced, tracing_enabled


@pytest.mark.skipif(
    tracing_enabled(), reason="otel installed; the no-op path is not exercised"
)
def test_span_is_noop_without_otel() -> None:
    # Without the otel extra installed, the span is a zero-cost no-op yielding None.
    with span("foundry.test", attr="value") as s:
        assert s is None


@pytest.mark.skipif(
    not tracing_enabled(), reason="otel not installed; the real-span path is unavailable"
)
def test_span_yields_a_real_span_with_otel() -> None:
    # With the otel extra installed, the context manager yields the live span so
    # attributes can be set on it.
    with span("foundry.test", attr="value") as s:
        assert s is not None


def test_traced_decorator_passes_through_return_value() -> None:
    @traced("foundry.adder")
    def add(a: int, b: int) -> int:
        return a + b

    assert add(2, 3) == 5


def test_traced_preserves_metadata() -> None:
    @traced("foundry.named")
    def fn() -> None:
        """docstring."""

    assert fn.__name__ == "fn"
    assert fn.__doc__ == "docstring."
