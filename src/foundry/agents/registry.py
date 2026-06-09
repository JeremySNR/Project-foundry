"""Provider registry - resolve a ``CodingAgentProvider`` by name.

The registry exists so the rest of the system never imports a concrete
provider directly; ``agent.provider`` in the YAML config selects one by name.
"""

from __future__ import annotations

from .claude_code import ClaudeCodeProvider
from .cursor import CursorCloudAgentProvider, CursorViaLinearProvider
from .manual import InMemoryFakeProvider, ManualProvider
from .provider import CodingAgentProvider
from .webhook import WebhookProvider

_REGISTRY: dict[str, type[CodingAgentProvider]] = {
    ManualProvider.name: ManualProvider,
    InMemoryFakeProvider.name: InMemoryFakeProvider,
    CursorViaLinearProvider.name: CursorViaLinearProvider,
    CursorCloudAgentProvider.name: CursorCloudAgentProvider,
    ClaudeCodeProvider.name: ClaudeCodeProvider,
    WebhookProvider.name: WebhookProvider,
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
