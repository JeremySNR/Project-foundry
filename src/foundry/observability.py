"""Lightweight tracing that no-ops without OpenTelemetry.

Foundry's value rests on explainability, so the run path is instrumented with
spans. OpenTelemetry is optional (the ``otel`` extra): when it isn't installed,
:func:`span` is a zero-cost context manager, so importing and using it never
forces the dependency on anyone.
"""

from __future__ import annotations

import functools
from contextlib import contextmanager
from typing import Any, Callable, Iterator, TypeVar

try:  # pragma: no cover - depends on whether the extra is installed
    from opentelemetry import trace

    _tracer = trace.get_tracer("foundry")
    _ENABLED = True
except ImportError:
    _ENABLED = False


def tracing_enabled() -> bool:
    return _ENABLED


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[Any]:
    """Start a span if OpenTelemetry is present; otherwise do nothing."""
    if not _ENABLED:
        yield None
        return
    with _tracer.start_as_current_span(name) as current:  # pragma: no cover - needs otel
        for key, value in attributes.items():
            if value is not None:
                current.set_attribute(key, str(value))
        yield current


_F = TypeVar("_F", bound=Callable[..., Any])


def traced(name: str) -> Callable[[_F], _F]:
    """Decorator that runs the wrapped callable inside a :func:`span`."""

    def decorator(func: _F) -> _F:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with span(name):
                return func(*args, **kwargs)

        return wrapper  # type: ignore[return-value]

    return decorator
