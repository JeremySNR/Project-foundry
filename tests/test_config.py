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
  repo_forbidden_globs:
    payments-service: ["**/ledger/**", "**/reconciliation/**"]
    platform-monorepo: ["services/billing/**"]
  repo_required_roles:
    payments-service: ["security"]
    platform-monorepo: ["engineering", "security"]
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
    assert s.repo_forbidden_map == {
        "payments-service": ("**/ledger/**", "**/reconciliation/**"),
        "platform-monorepo": ("services/billing/**",),
    }
    assert s.repo_required_roles_map == {
        "payments-service": ("security",),
        "platform-monorepo": ("engineering", "security"),
    }
    assert s.temporal_address == "temporal.internal:7233"


def test_repo_forbidden_globs_default_empty() -> None:
    """No config => no per-repo forbidden globs (global list unchanged)."""
    assert Settings.from_env({}).repo_forbidden_map == {}


def test_repo_required_roles_default_empty() -> None:
    """No config => no per-repo approval roles (risk-derived roles unchanged)."""
    assert Settings.from_env({}).repo_required_roles_map == {}


def test_repo_required_roles_rejects_unknown_role(tmp_path) -> None:
    """An unknown approval role is a deploy-time error, not a silently-dropped
    rule that would leave a repo less protected than intended (issue #31)."""
    import pytest

    path = tmp_path / "foundry.yaml"
    path.write_text(
        "policy:\n  repo_required_roles:\n"
        "    payments-service: ['securty']\n"  # typo
    )
    with pytest.raises(ValueError, match="unknown approval roles"):
        Settings.load(path, env={})


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


def test_epics_auto_decompose_defaults_off() -> None:
    assert Settings.from_env({}).epics_auto_decompose is False


def test_epics_auto_decompose_from_yaml_and_env(tmp_path) -> None:
    path = tmp_path / "foundry.yaml"
    path.write_text("epics:\n  auto_decompose: true\n")
    assert Settings.load(path, env={}).epics_auto_decompose is True
    # Env overrides YAML.
    s = Settings.load(path, env={"FOUNDRY_EPICS_AUTO_DECOMPOSE": "false"})
    assert s.epics_auto_decompose is False
    # Env alone also flips it on.
    assert Settings.from_env({"FOUNDRY_EPICS_AUTO_DECOMPOSE": "1"}).epics_auto_decompose is True


def test_rate_limit_defaults_on() -> None:
    s = Settings.from_env({})
    assert s.rate_limit_enabled is True
    assert s.rate_limit_webhook_per_minute == 120
    assert s.rate_limit_api_per_minute == 60


def test_rate_limit_from_yaml_and_env(tmp_path) -> None:
    path = tmp_path / "foundry.yaml"
    path.write_text(
        "rate_limit:\n  enabled: true\n  webhook_per_minute: 300\n  api_per_minute: 30\n"
    )
    s = Settings.load(path, env={})
    assert s.rate_limit_enabled is True
    assert s.rate_limit_webhook_per_minute == 300
    assert s.rate_limit_api_per_minute == 30
    # Env overrides YAML (operational knob).
    s2 = Settings.load(
        path,
        env={
            "FOUNDRY_RATE_LIMIT_ENABLED": "false",
            "FOUNDRY_RATE_LIMIT_API_PER_MINUTE": "10",
        },
    )
    assert s2.rate_limit_enabled is False
    assert s2.rate_limit_api_per_minute == 10


def test_rate_limit_invalid_values_rejected() -> None:
    import pytest

    with pytest.raises(ValueError):
        Settings.from_env({"FOUNDRY_RATE_LIMIT_API_PER_MINUTE": "0"})
    with pytest.raises(ValueError):
        Settings.from_env({"FOUNDRY_RATE_LIMIT_WEBHOOK_PER_MINUTE": "0"})


def test_slack_notifications_from_env_and_yaml(tmp_path) -> None:
    # Bot token is env-only; the channel may come from YAML or env (env wins).
    s = Settings.from_env(
        {"FOUNDRY_SLACK_BOT_TOKEN": "xoxb-1", "FOUNDRY_SLACK_CHANNEL": "C-env"}
    )
    assert s.slack_bot_token == "xoxb-1"
    assert s.slack_channel == "C-env"

    path = tmp_path / "foundry.yaml"
    path.write_text("notifications:\n  slack_channel: C-yaml\n")
    assert Settings.load(path, env={}).slack_channel == "C-yaml"
    # Env overrides the YAML channel.
    assert (
        Settings.load(path, env={"FOUNDRY_SLACK_CHANNEL": "C-env"}).slack_channel
        == "C-env"
    )
    # Default: outbound Slack unconfigured.
    assert Settings.from_env({}).slack_bot_token is None
    assert Settings.from_env({}).slack_channel is None


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
        "  estimated_cost_per_dispatch: 2.5\n"
    )
    settings = Settings.load(str(config), env={})
    assert settings.max_agent_retries == 1
    assert settings.retry_on == ("ci_failed",)
    assert settings.max_cost_per_run == 10.5
    assert settings.estimated_cost_per_dispatch == 2.5


def test_invalid_remediation_and_budget_rejected(tmp_path) -> None:
    import pytest

    cases = [
        "budget:\n  max_cost_per_run: 0\n",
        "budget:\n  estimated_cost_per_dispatch: -1\n",
        "remediation:\n  retry_on: [nonsense]\n",
        "remediation:\n  max_agent_retries: -1\n",
    ]
    for i, content in enumerate(cases):
        config = tmp_path / f"bad-{i}.yaml"
        config.write_text(content)
        with pytest.raises(ValueError):
            Settings.load(str(config), env={})


def test_webhook_yaml_parsing(tmp_path) -> None:
    config = tmp_path / "foundry.yaml"
    config.write_text(
        "webhook:\n"
        "  dedup_ttl_seconds: 3600\n"
        "  replay_max_age_seconds: 300\n"
    )
    settings = Settings.load(str(config), env={})
    assert settings.webhook_dedup_ttl_seconds == 3600
    assert settings.webhook_replay_max_age_seconds == 300


def test_webhook_defaults_when_block_absent() -> None:
    s = Settings.from_env({})
    assert s.webhook_dedup_ttl_seconds == 86_400
    assert s.webhook_replay_max_age_seconds is None


def test_webhook_null_disables(tmp_path) -> None:
    config = tmp_path / "foundry.yaml"
    config.write_text(
        "webhook:\n"
        "  dedup_ttl_seconds: null\n"
        "  replay_max_age_seconds: null\n"
    )
    settings = Settings.load(str(config), env={})
    assert settings.webhook_dedup_ttl_seconds is None
    assert settings.webhook_replay_max_age_seconds is None


def test_invalid_webhook_config_rejected(tmp_path) -> None:
    import pytest

    cases = [
        # replay window wider than the dedup TTL: a delivery could age out of
        # the dedup table while still inside the replay window.
        "webhook:\n  dedup_ttl_seconds: 100\n  replay_max_age_seconds: 200\n",
        "webhook:\n  dedup_ttl_seconds: 0\n",
        "webhook:\n  replay_max_age_seconds: 0\n",
    ]
    for i, content in enumerate(cases):
        config = tmp_path / f"bad-wh-{i}.yaml"
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


def test_planner_provider_defaults_to_template() -> None:
    s = Settings.from_env({})
    assert s.planner_provider == "template"
    assert s.planner_model == "gpt-5.5"


def test_planner_provider_from_yaml(tmp_path) -> None:
    path = tmp_path / "foundry.yaml"
    path.write_text("planner:\n  provider: llm\n  model: gpt-4o-2026-04-23\n")
    s = Settings.load(path, env={})
    assert s.planner_provider == "llm"
    assert s.planner_model == "gpt-4o-2026-04-23"


def test_planner_provider_env_overrides_yaml(tmp_path) -> None:
    path = tmp_path / "foundry.yaml"
    path.write_text("planner:\n  provider: template\n")
    s = Settings.load(
        path,
        env={
            "FOUNDRY_PLANNER_PROVIDER": "llm",
            "FOUNDRY_PLANNER_MODEL": "gpt-5.5-mini",
        },
    )
    assert s.planner_provider == "llm"
    assert s.planner_model == "gpt-5.5-mini"


def test_invalid_planner_provider_raises() -> None:
    import pytest

    with pytest.raises(ValueError, match="planner_provider"):
        Settings.from_env({"FOUNDRY_PLANNER_PROVIDER": "bogus"})


def test_decomposition_provider_defaults_to_heuristic() -> None:
    s = Settings.from_env({})
    assert s.decomposition_provider == "heuristic"
    assert s.decomposition_model == "gpt-5.5"


def test_decomposition_provider_from_yaml(tmp_path) -> None:
    path = tmp_path / "foundry.yaml"
    path.write_text("decomposition:\n  provider: llm\n  model: gpt-4o-2026-04-23\n")
    s = Settings.load(path, env={})
    assert s.decomposition_provider == "llm"
    assert s.decomposition_model == "gpt-4o-2026-04-23"


def test_decomposition_provider_env_overrides_yaml(tmp_path) -> None:
    path = tmp_path / "foundry.yaml"
    path.write_text("decomposition:\n  provider: heuristic\n")
    s = Settings.load(
        path,
        env={
            "FOUNDRY_DECOMPOSITION_PROVIDER": "llm",
            "FOUNDRY_DECOMPOSITION_MODEL": "gpt-5.5-mini",
        },
    )
    assert s.decomposition_provider == "llm"
    assert s.decomposition_model == "gpt-5.5-mini"


def test_invalid_decomposition_provider_raises() -> None:
    import pytest

    with pytest.raises(ValueError, match="decomposition_provider"):
        Settings.from_env({"FOUNDRY_DECOMPOSITION_PROVIDER": "bogus"})


def test_policy_provider_defaults_to_local() -> None:
    s = Settings.from_env({})
    assert s.policy_provider == "local"
    assert s.policy_opa_url is None


def test_policy_provider_opa_from_yaml(tmp_path) -> None:
    path = tmp_path / "foundry.yaml"
    path.write_text("policy:\n  provider: opa\n  opa_url: http://opa:8181\n")
    s = Settings.load(path, env={})
    assert s.policy_provider == "opa"
    assert s.policy_opa_url == "http://opa:8181"


def test_policy_provider_env_overrides_yaml(tmp_path) -> None:
    path = tmp_path / "foundry.yaml"
    path.write_text("policy:\n  provider: local\n")
    s = Settings.load(
        path,
        env={
            "FOUNDRY_POLICY_PROVIDER": "opa",
            "FOUNDRY_POLICY_OPA_URL": "http://opa.internal:8181",
        },
    )
    assert s.policy_provider == "opa"
    assert s.policy_opa_url == "http://opa.internal:8181"


def test_invalid_policy_provider_raises() -> None:
    import pytest

    with pytest.raises(ValueError, match="policy_provider"):
        Settings.from_env({"FOUNDRY_POLICY_PROVIDER": "bogus"})


def test_opa_provider_without_url_raises() -> None:
    import pytest

    with pytest.raises(ValueError, match="policy_opa_url is required"):
        Settings.from_env({"FOUNDRY_POLICY_PROVIDER": "opa"})


# -- learned dispatch config (agent.provider: auto, issue #33) ------------------


def test_agent_auto_defaults() -> None:
    s = Settings.from_env({})
    assert s.agent_auto_candidates == ()
    assert s.agent_auto_fallback == "manual"
    assert s.agent_auto_min_samples == 3


def test_agent_auto_from_yaml(tmp_path) -> None:
    path = tmp_path / "foundry.yaml"
    path.write_text(
        "agent:\n"
        "  provider: auto\n"
        "  auto_candidates: [claude_code, cursor_cloud]\n"
        "  auto_fallback: claude_code\n"
        "  auto_min_samples: 5\n"
    )
    s = Settings.load(path, env={})
    assert s.agent_provider == "auto"
    assert s.agent_auto_candidates == ("claude_code", "cursor_cloud")
    assert s.agent_auto_fallback == "claude_code"
    assert s.agent_auto_min_samples == 5


def test_agent_auto_env_overrides(tmp_path) -> None:
    s = Settings.from_env(
        {
            "FOUNDRY_AGENT_PROVIDER": "auto",
            "FOUNDRY_AGENT_AUTO_CANDIDATES": "claude_code, cursor_cloud",
            "FOUNDRY_AGENT_AUTO_FALLBACK": "cursor_cloud",
            "FOUNDRY_AGENT_AUTO_MIN_SAMPLES": "4",
        }
    )
    assert s.agent_auto_candidates == ("claude_code", "cursor_cloud")
    assert s.agent_auto_fallback == "cursor_cloud"
    assert s.agent_auto_min_samples == 4


def test_agent_provider_auto_requires_candidates() -> None:
    import pytest

    with pytest.raises(ValueError, match="auto_candidates"):
        Settings.from_env({"FOUNDRY_AGENT_PROVIDER": "auto"})


def test_agent_auto_min_samples_must_be_positive() -> None:
    import pytest

    with pytest.raises(ValueError, match="auto_min_samples"):
        Settings.from_env({"FOUNDRY_AGENT_AUTO_MIN_SAMPLES": "0"})
