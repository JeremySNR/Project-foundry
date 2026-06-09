"""Tests for the Claude Code and generic webhook providers + registry/config wiring."""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from foundry.agents import (
    ClaudeCodeProvider,
    SecretLeakError,
    WebhookProvider,
    available_providers,
    get_provider,
)
from foundry.api.app import build_provider
from foundry.config import Settings
from foundry.schemas.agent import CodingAgentJobInput
from foundry.schemas.common import AgentJobStatus


def _job_input(**overrides) -> CodingAgentJobInput:
    base = {
        "run_id": "run-1",
        "repo": "org/customer-web",
        "branch_name": "foundry/lin-123-favourites",
        "ticket_url": "https://linear.app/issue/LIN-123",
        "delivery_plan": {"goal": "Add favourites"},
        "agent_instructions": "Implement favourites per the plan.",
        "constraints": {
            "do_not_modify": ["migrations/**"],
            "required_tests": ["pytest"],
        },
    }
    base.update(overrides)
    return CodingAgentJobInput.model_validate(base)


# -- ClaudeCodeProvider --------------------------------------------------------


def test_claude_code_fires_workflow_dispatch() -> None:
    calls: list[tuple[str, dict, dict]] = []

    def http_post(url, body, headers):
        calls.append((url, body, headers))
        return None  # GitHub returns 204

    provider = ClaudeCodeProvider(http_post=http_post)
    job = provider.create_job(_job_input())

    assert job.provider == "claude_code"
    assert job.status is AgentJobStatus.RUNNING
    assert job.job_id == "claude-gha:org/customer-web:foundry/lin-123-favourites"

    url, body, _ = calls[0]
    assert url == (
        "https://api.github.com/repos/org/customer-web/actions/workflows/"
        "foundry-claude-code.yml/dispatches"
    )
    assert body["ref"] == "main"
    inputs = body["inputs"]
    assert inputs["run_id"] == "run-1"
    assert inputs["branch_name"] == "foundry/lin-123-favourites"
    assert "Implement favourites" in inputs["instructions"]
    assert inputs["do_not_modify"] == "migrations/**"
    assert inputs["required_tests"] == "pytest"
    # workflow_dispatch inputs must all be strings
    assert all(isinstance(v, str) for v in inputs.values())


def test_claude_code_custom_workflow_file() -> None:
    calls = []
    provider = ClaudeCodeProvider(
        http_post=lambda u, b, h: calls.append(u), workflow_file="my-runner.yml"
    )
    provider.create_job(_job_input())
    assert calls[0].endswith("/actions/workflows/my-runner.yml/dispatches")


def test_claude_code_secret_guard_blocks_dispatch() -> None:
    calls = []
    provider = ClaudeCodeProvider(http_post=lambda u, b, h: calls.append(u))
    with pytest.raises(SecretLeakError):
        provider.create_job(
            _job_input(agent_instructions="api_key=abcdef1234567890 do it")
        )
    assert calls == []


# -- WebhookProvider -----------------------------------------------------------


def test_webhook_provider_posts_signed_job() -> None:
    calls: list[tuple[str, bytes, dict]] = []

    def http_post(url, body, headers):
        calls.append((url, body, headers))
        return {"job_id": "agent-77"}

    provider = WebhookProvider(
        url="https://agents.internal/run", http_post=http_post, signing_secret="s3cret"
    )
    job = provider.create_job(_job_input())
    assert job.job_id == "agent-77"
    assert job.provider == "webhook"

    url, body, headers = calls[0]
    assert url == "https://agents.internal/run"
    payload = json.loads(body)
    assert payload["run_id"] == "run-1"
    assert payload["branch_name"] == "foundry/lin-123-favourites"
    # Receiver can verify the HMAC over the exact bytes it received.
    expected = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert headers["X-Foundry-Signature"] == expected


def test_webhook_provider_synthesises_job_id_and_skips_signature() -> None:
    def http_post(url, body, headers):
        assert "X-Foundry-Signature" not in headers
        return None

    provider = WebhookProvider(url="https://x/run", http_post=http_post)
    job = provider.create_job(_job_input())
    assert job.job_id == "webhook:run-1"


# -- registry + config wiring --------------------------------------------------


def test_registry_knows_all_providers() -> None:
    assert {
        "manual",
        "fake",
        "cursor_via_linear",
        "cursor_cloud",
        "claude_code",
        "webhook",
    } <= set(available_providers())
    assert get_provider("manual").name == "manual"


def test_build_provider_defaults_to_manual() -> None:
    provider = build_provider(Settings())
    assert provider.name == "manual"


def test_build_provider_fails_closed_on_missing_credentials() -> None:
    with pytest.raises(ValueError, match="FOUNDRY_CURSOR_API_TOKEN"):
        build_provider(Settings(agent_provider="cursor_cloud"))
    with pytest.raises(ValueError, match="FOUNDRY_GITHUB_API_TOKEN"):
        build_provider(Settings(agent_provider="claude_code"))
    with pytest.raises(ValueError, match="FOUNDRY_AGENT_WEBHOOK_URL"):
        build_provider(Settings(agent_provider="webhook"))
    with pytest.raises(ValueError, match="LINEAR"):
        build_provider(Settings(agent_provider="cursor_via_linear"))
    with pytest.raises(ValueError, match="unknown"):
        build_provider(Settings(agent_provider="skynet"))


def test_build_provider_constructs_configured_providers() -> None:
    claude = build_provider(
        Settings(agent_provider="claude_code", github_api_token="ghp_x")
    )
    assert claude.name == "claude_code"
    hook = build_provider(
        Settings(
            agent_provider="webhook",
            agent_webhook_url="https://x/run",
            agent_webhook_secret="s",
        )
    )
    assert hook.name == "webhook"


def test_settings_load_agent_provider_from_yaml_and_env(tmp_path) -> None:
    config = tmp_path / "foundry.yaml"
    config.write_text("agent:\n  provider: claude_code\n  claude_workflow_file: r.yml\n")
    settings = Settings.load(str(config), env={})
    assert settings.agent_provider == "claude_code"
    assert settings.claude_workflow_file == "r.yml"
    # env override wins
    settings = Settings.load(str(config), env={"FOUNDRY_AGENT_PROVIDER": "webhook"})
    assert settings.agent_provider == "webhook"
