"""Coding-agent provider abstraction and adapters."""

from __future__ import annotations

from .manual import InMemoryFakeProvider, ManualProvider
from .provider import (
    CodingAgentProvider,
    SecretLeakError,
    assert_no_secrets,
)
from .registry import available_providers, get_provider, register

__all__ = [
    "CodingAgentProvider",
    "SecretLeakError",
    "assert_no_secrets",
    "ManualProvider",
    "InMemoryFakeProvider",
    "register",
    "get_provider",
    "available_providers",
]
