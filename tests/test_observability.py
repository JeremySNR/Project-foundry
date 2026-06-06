"""Observability helpers must be safe with OpenTelemetry absent (no-op)."""

from __future__ import annotations

from foundry.observability import span, traced, tracing_enabled


def test_span_is_noop_without_otel() -> None:
    with span("foundry.test", attr="value") as s:
        # Without the otel extra installed, the span is a no-op yielding None.
        if not tracing_enabled():
            assert s is None


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
