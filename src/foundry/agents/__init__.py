"""Coding-agent provider abstraction and adapters."""

from __future__ import annotations

from .claude_code import ClaudeCodeProvider
from .cursor import CursorCloudAgentProvider, CursorViaLinearProvider
from .manual import InMemoryFakeProvider, ManualProvider
from .provider import (
    CodingAgentProvider,
    SecretLeakError,
    assert_no_secrets,
)
from .registry import available_providers, get_provider, register
from .webhook import WebhookProvider

__all__ = [
    "CodingAgentProvider",
    "SecretLeakError",
    "assert_no_secrets",
    "ManualProvider",
    "InMemoryFakeProvider",
    "CursorViaLinearProvider",
    "CursorCloudAgentProvider",
    "ClaudeCodeProvider",
    "WebhookProvider",
    "register",
    "get_provider",
    "available_providers",
]
