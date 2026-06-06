"""Tests for environment-driven settings."""

from __future__ import annotations

from foundry.config import Settings


def test_defaults_when_env_empty() -> None:
    s = Settings.from_env({})
    assert s.database_url.startswith("sqlite")
    assert s.openai_model == "gpt-5.5"
    assert s.use_openai_analyzer is False
    assert s.github_webhook_secret is None
    assert s.task_queue == "foundry-ticket-to-pr"


def test_reads_env() -> None:
    s = Settings.from_env(
        {
            "FOUNDRY_DATABASE_URL": "postgresql+psycopg://u@h/db",
            "FOUNDRY_LINEAR_WEBHOOK_SECRET": "lw",
            "FOUNDRY_GITHUB_WEBHOOK_SECRET": "gw",
            "FOUNDRY_LINEAR_API_TOKEN": "lt",
            "FOUNDRY_GITHUB_API_TOKEN": "gt",
            "FOUNDRY_USE_OPENAI_ANALYZER": "true",
            "FOUNDRY_OPENAI_MODEL": "gpt-5.5-2026-04-23",
            "TEMPORAL_ADDRESS": "temporal:7233",
        }
    )
    assert s.database_url.startswith("postgresql")
    assert s.linear_webhook_secret == "lw"
    assert s.github_webhook_secret == "gw"
    assert s.linear_api_token == "lt"
    assert s.github_api_token == "gt"
    assert s.use_openai_analyzer is True
    assert s.openai_model == "gpt-5.5-2026-04-23"
    assert s.temporal_address == "temporal:7233"


def test_bool_parsing_variants() -> None:
    assert Settings.from_env({"FOUNDRY_USE_OPENAI_ANALYZER": "1"}).use_openai_analyzer
    assert Settings.from_env({"FOUNDRY_USE_OPENAI_ANALYZER": "YES"}).use_openai_analyzer
    assert not Settings.from_env({"FOUNDRY_USE_OPENAI_ANALYZER": "0"}).use_openai_analyzer
    assert not Settings.from_env({"FOUNDRY_USE_OPENAI_ANALYZER": "no"}).use_openai_analyzer
