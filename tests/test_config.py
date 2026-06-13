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


def test_jira_allow_query_token_defaults_off_and_parses_from_yaml(tmp_path) -> None:
    # Default posture: the Jira webhook token is header-only.
    assert Settings.load("/no/such/file.yaml", env={}).jira_allow_query_token is False
    path = tmp_path / "foundry.yaml"
    path.write_text("tracker:\n  provider: jira\n  jira_allow_query_token: true\n")
    s = Settings.load(path, env={})
    assert s.jira_allow_query_token is True


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


_CONTEXT_YAML = """
context:
  provider: catalog
  org: acme
  max_catalog_age_days: 14
  sync_call_budget: 500
  repo_keywords:
    acme/billing-service: ["invoice", "stripe"]
    acme/shipping: ["shipment", "tracking"]
"""


def test_context_yaml_parsing(tmp_path) -> None:
    path = tmp_path / "foundry.yaml"
    path.write_text(_CONTEXT_YAML)
    s = Settings.load(path, env={})

    assert s.context_provider == "catalog"
    assert s.context_org == "acme"
    assert s.context_max_catalog_age_days == 14
    assert s.context_sync_call_budget == 500
    kw = dict(s.context_repo_keywords)
    assert set(kw["acme/billing-service"]) == {"invoice", "stripe"}
    assert set(kw["acme/shipping"]) == {"shipment", "tracking"}


def test_context_defaults_when_block_absent() -> None:
    s = Settings.from_env({})
    assert s.context_provider == "static"
    assert s.context_org is None
    assert s.context_max_catalog_age_days == 7
    assert s.context_sync_call_budget == 3000
    assert s.context_repo_keywords == ()
    assert s.context_sync_code_facts is False
    assert s.context_tree_max_paths == 2000


def test_context_code_provider_yaml(tmp_path) -> None:
    path = tmp_path / "foundry.yaml"
    path.write_text(
        "context:\n"
        "  provider: code\n"
        "  sync_code_facts: true\n"
        "  tree_max_paths: 500\n"
    )
    s = Settings.load(path, env={})
    assert s.context_provider == "code"
    assert s.context_sync_code_facts is True
    assert s.context_tree_max_paths == 500


def test_context_env_overrides(tmp_path) -> None:
    path = tmp_path / "foundry.yaml"
    path.write_text(_CONTEXT_YAML)
    s = Settings.load(path, env={"FOUNDRY_CONTEXT_PROVIDER": "static", "FOUNDRY_CONTEXT_ORG": "other"})
    assert s.context_provider == "static"
    assert s.context_org == "other"


def test_context_invalid_provider_rejected(tmp_path) -> None:
    import pytest

    path = tmp_path / "bad.yaml"
    path.write_text("context:\n  provider: unknown\n")
    with pytest.raises(ValueError, match="context_provider"):
        Settings.load(path, env={})


def test_context_invalid_age_and_budget_rejected(tmp_path) -> None:
    import pytest

    for i, content in enumerate([
        "context:\n  max_catalog_age_days: 0\n",
        "context:\n  sync_call_budget: 0\n",
        "context:\n  tree_max_paths: 50\n",
    ]):
        path = tmp_path / f"bad-ctx-{i}.yaml"
        path.write_text(content)
        with pytest.raises(ValueError):
            Settings.load(path, env={})


def test_memory_defaults_and_yaml(tmp_path) -> None:
    s = Settings.from_env({})
    assert s.memory_priors_enabled is True
    assert s.memory_min_samples == 3
    assert s.memory_confidence_cap == 89

    path = tmp_path / "foundry.yaml"
    path.write_text(
        "memory:\n"
        "  priors_enabled: false\n"
        "  min_samples: 5\n"
        "  confidence_cap: 80\n"
    )
    s = Settings.load(path, env={})
    assert s.memory_priors_enabled is False
    assert s.memory_min_samples == 5
    assert s.memory_confidence_cap == 80


def test_memory_validation(tmp_path) -> None:
    import pytest

    path = tmp_path / "foundry.yaml"
    path.write_text("memory:\n  min_samples: 0\n")
    with pytest.raises(ValueError, match="memory_min_samples"):
        Settings.load(path, env={})

    path.write_text("memory:\n  confidence_cap: 101\n")
    with pytest.raises(ValueError, match="memory_confidence_cap"):
        Settings.load(path, env={})


def test_risk_provider_defaults_to_heuristic() -> None:
    s = Settings.from_env({})
    assert s.risk_provider == "heuristic"
    assert s.risk_model == "gpt-5.5"


def test_risk_provider_from_yaml(tmp_path) -> None:
    path = tmp_path / "foundry.yaml"
    path.write_text("risk:\n  provider: llm\n  model: gpt-4o-2026-04-23\n")
    s = Settings.load(path, env={})
    assert s.risk_provider == "llm"
    assert s.risk_model == "gpt-4o-2026-04-23"


def test_risk_provider_env_overrides_yaml(tmp_path) -> None:
    path = tmp_path / "foundry.yaml"
    path.write_text("risk:\n  provider: heuristic\n")
    s = Settings.load(
        path,
        env={"FOUNDRY_RISK_PROVIDER": "llm", "FOUNDRY_RISK_MODEL": "gpt-5.5-mini"},
    )
    assert s.risk_provider == "llm"
    assert s.risk_model == "gpt-5.5-mini"


def test_invalid_risk_provider_raises() -> None:
    import pytest

    with pytest.raises(ValueError, match="risk_provider"):
        Settings.from_env({"FOUNDRY_RISK_PROVIDER": "bogus"})
