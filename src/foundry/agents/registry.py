"""Provider registry - resolve a ``CodingAgentProvider`` by name.

Cursor / Claude Code / OpenAI adapters register here as they are implemented.
The MVP ships only the manual and fake providers; the registry exists so the
rest of the system never imports a concrete provider directly.
"""

from __future__ import annotations

from .manual import InMemoryFakeProvider, ManualProvider
from .provider import CodingAgentProvider

_REGISTRY: dict[str, type[CodingAgentProvider]] = {
    ManualProvider.name: ManualProvider,
    InMemoryFakeProvider.name: InMemoryFakeProvider,
}


def register(provider_cls: type[CodingAgentProvider]) -> type[CodingAgentProvider]:
    """Register a provider class. Usable as a decorator."""
    _REGISTRY[provider_cls.name] = provider_cls
    return provider_cls


def available_providers() -> list[str]:
    return sorted(_REGISTRY)


def get_provider(name: str, **kwargs) -> CodingAgentProvider:
    try:
        provider_cls = _REGISTRY[name]
    except KeyError as exc:
        raise ValueError(
            f"unknown coding-agent provider '{name}'; "
            f"available: {available_providers()}"
        ) from exc
    return provider_cls(**kwargs)
