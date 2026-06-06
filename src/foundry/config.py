"""Centralised, environment-driven configuration.

One place to read every secret/URL Foundry needs, so nothing reaches for
``os.environ`` ad hoc. Deliberately a plain dataclass (no extra dependency) with
an explicit ``from_env`` so it is trivial to construct in tests.

Secrets are read here and handed to the transports/connectors that need them;
they never travel into agent prompts or artifacts.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

_TRUE = {"1", "true", "yes", "on"}


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in _TRUE


@dataclass(frozen=True)
class Settings:
    # Storage
    database_url: str = "sqlite+pysqlite:///:memory:"

    # Webhook signing secrets
    linear_webhook_secret: str = ""
    github_webhook_secret: str | None = None

    # API tokens for outbound calls (None => that connector is not wired live)
    linear_api_token: str | None = None
    github_api_token: str | None = None

    # Intelligence
    use_openai_analyzer: bool = False
    openai_model: str = "gpt-5.5"

    # Durable execution
    temporal_address: str = "localhost:7233"
    task_queue: str = "foundry-ticket-to-pr"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = env or os.environ
        return cls(
            database_url=env.get("FOUNDRY_DATABASE_URL", cls.database_url),
            linear_webhook_secret=env.get("FOUNDRY_LINEAR_WEBHOOK_SECRET", ""),
            github_webhook_secret=env.get("FOUNDRY_GITHUB_WEBHOOK_SECRET"),
            linear_api_token=env.get("FOUNDRY_LINEAR_API_TOKEN"),
            github_api_token=env.get("FOUNDRY_GITHUB_API_TOKEN"),
            use_openai_analyzer=_bool(env.get("FOUNDRY_USE_OPENAI_ANALYZER")),
            openai_model=env.get("FOUNDRY_OPENAI_MODEL", cls.openai_model),
            temporal_address=env.get("TEMPORAL_ADDRESS", cls.temporal_address),
            task_queue=env.get("FOUNDRY_TASK_QUEUE", cls.task_queue),
        )
