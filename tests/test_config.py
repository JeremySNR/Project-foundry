"""Tests for environment-driven settings."""

from __future__ import annotations

from foundry.config import Settings


def test_defaults_when_env_empty() -> None:
    s = Settings.from_env({})
    assert s.database_url.startswith("sqlite")
    assert s.openai_model == "gpt-5.5"
    assert s.use_openai_analyzer is False
    assert s.github_webhook_secret is None
    assert s.api_token is None
    assert s.task_queue == "foundry-ticket-to-pr"
    assert s.approvers == ()
    # Built-in diff-risk globs exist out of the box.
    assert "auth" in s.sensitive_globs_map


def test_reads_env() -> None:
    s = Settings.from_env(
        {
            "FOUNDRY_DATABASE_URL": "postgresql+psycopg://u@h/db",
            "FOUNDRY_LINEAR_WEBHOOK_SECRET": "lw",
            "FOUNDRY_GITHUB_WEBHOOK_SECRET": "gw",
            "FOUNDRY_LINEAR_API_TOKEN": "lt",
            "FOUNDRY_GITHUB_API_TOKEN": "gt",
            "FOUNDRY_USE_OPENAI_ANALYZER": "true",
            "FOUNDRY_OPENAI_MODEL": "gpt-4o-2026-04-23",
            "TEMPORAL_ADDRESS": "temporal:7233",
        }
    )
    assert s.database_url.startswith("postgresql")
    assert s.linear_webhook_secret == "lw"
    assert s.github_webhook_secret == "gw"
    assert s.linear_api_token == "lt"
    assert s.github_api_token == "gt"
    assert s.use_openai_analyzer is True
    assert s.openai_model == "gpt-4o-2026-04-23"
    assert s.temporal_address == "temporal:7233"


def test_bool_parsing_variants() -> None:
    assert Settings.from_env({"FOUNDRY_USE_OPENAI_ANALYZER": "1"}).use_openai_analyzer
    assert Settings.from_env({"FOUNDRY_USE_OPENAI_ANALYZER": "YES"}).use_openai_analyzer
    assert not Settings.from_env({"FOUNDRY_USE_OPENAI_ANALYZER": "0"}).use_openai_analyzer
    assert not Settings.from_env({"FOUNDRY_USE_OPENAI_ANALYZER": "no"}).use_openai_analyzer


_YAML = """
database:
  url: "postgresql+psycopg://u@h/db"
analyzer:
  provider: openai
  model: gpt-4o-2026-04-23
policy:
  repo_confidence_threshold: 85
  max_files_changed: 5
  forbidden_globs:
    - "infra/**"
    - "secrets/**"
  sensitive_path_globs:
    auth: ["**/iam/**"]
    payments: ["**/billing/**"]
triggers:
  label: "ai:go"
  status: "Ready for Foundry"
approval:
  approvers:
    - email: "alice@example.com"
      roles: ["engineering", "security"]
    - email: "bob@example.com"
      roles: []
temporal:
  address: "temporal.internal:7233"
  task_queue: "tq"
"""


def test_load_from_yaml(tmp_path) -> None:
    path = tmp_path / "foundry.yaml"
    path.write_text(_YAML)
    s = Settings.load(path, env={})
    assert s.database_url.startswith("postgresql")
    assert s.use_openai_analyzer is True
    assert s.openai_model == "gpt-4o-2026-04-23"
    assert s.repo_confidence_threshold == 85
    assert s.max_files_changed == 5
    assert s.forbidden_globs == ("infra/**", "secrets/**")
    assert s.trigger_label == "ai:go"
    assert s.trigger_status == "Ready for Foundry"
    assert s.approver_emails == {"alice@example.com", "bob@example.com"}
    assert s.roles_for("alice@example.com") == {"engineering", "security"}
    assert s.roles_for("bob@example.com") == set()
    assert s.roles_for("nobody@example.com") == set()
    assert s.sensitive_globs_map == {
        "auth": ("**/iam/**",),
        "payments": ("**/billing/**",),
    }
    assert s.temporal_address == "temporal.internal:7233"


def test_legacy_authorised_approvers_yaml_still_loads(tmp_path) -> None:
    path = tmp_path / "foundry.yaml"
    path.write_text(
        "approval:\n  authorised_approvers:\n    - 'lead@example.com'\n"
    )
    s = Settings.load(path, env={})
    assert s.approver_emails == {"lead@example.com"}
    assert s.roles_for("lead@example.com") == set()


def test_api_token_from_env() -> None:
    s = Settings.from_env({"FOUNDRY_API_TOKEN": "tok"})
    assert s.api_token == "tok"


def test_env_overrides_yaml(tmp_path) -> None:
    path = tmp_path / "foundry.yaml"
    path.write_text(_YAML)
    # Env wins over YAML for the keys it covers.
    s = Settings.load(
        path,
        env={
            "FOUNDRY_DATABASE_URL": "sqlite+pysqlite:///:memory:",
            "FOUNDRY_OPENAI_MODEL": "gpt-4o",
        },
    )
    assert s.database_url.startswith("sqlite")
    assert s.openai_model == "gpt-4o"
    # YAML-only knobs are untouched by env.
    assert s.repo_confidence_threshold == 85
    assert s.trigger_label == "ai:go"


def test_missing_yaml_path_is_defaults() -> None:
    s = Settings.load("/no/such/file.yaml", env={})
    assert s.repo_confidence_threshold == 70
    assert s.trigger_label == "foundry:candidate"


def test_remediation_and_budget_yaml(tmp_path) -> None:
    config = tmp_path / "foundry.yaml"
    config.write_text(
        "remediation:\n"
        "  max_agent_retries: 1\n"
        "  retry_on: [ci_failed]\n"
        "budget:\n"
        "  max_cost_per_run: 10.5\n"
    )
    settings = Settings.load(str(config), env={})
    assert settings.max_agent_retries == 1
    assert settings.retry_on == ("ci_failed",)
    assert settings.max_cost_per_run == 10.5


def test_invalid_remediation_and_budget_rejected(tmp_path) -> None:
    import pytest

    cases = [
        "budget:\n  max_cost_per_run: 0\n",
        "remediation:\n  retry_on: [nonsense]\n",
        "remediation:\n  max_agent_retries: -1\n",
    ]
    for i, content in enumerate(cases):
        config = tmp_path / f"bad-{i}.yaml"
        config.write_text(content)
        with pytest.raises(ValueError):
            Settings.load(str(config), env={})
