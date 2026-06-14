"""Configuration: a YAML file for behaviour, environment variables for secrets.

Foundry is meant to be highly customisable without touching code. The knobs that
shape *how it behaves* (which analyzer, the policy thresholds, the trigger label,
who can approve) live in a YAML file. The things that are *secret* (webhook
signing secrets, API tokens, the database URL) come from the environment and are
never written to YAML.

Layering, lowest priority first:

    built-in defaults  <  foundry.yaml  <  environment variables

so you can commit a sane YAML and let each deployment override the sensitive
bits (and a few operational ones) from its environment.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Mapping

_TRUE = {"1", "true", "yes", "on"}

DEFAULT_FORBIDDEN_GLOBS = ("infra/**", "migrations/**", "**/.env*", "**/secrets/**")
DEFAULT_TRIGGER_LABEL = "foundry:candidate"
DEFAULT_TRIGGER_STATUS = "Ready for AI Analysis"

# Path patterns that indicate a PR actually touched a sensitive area. Used by
# the diff-aware risk check after a PR opens/updates - the upfront (ticket-text)
# risk classification can miss work that only becomes sensitive in the diff.
DEFAULT_SENSITIVE_PATH_GLOBS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("auth", ("**/auth/**", "**/authn/**", "**/authz/**", "**/login/**",
              "**/session*/**", "**/sso/**", "**/oauth/**")),
    ("payments", ("**/payment*/**", "**/billing/**", "**/stripe/**",
                  "**/invoice*/**", "**/checkout/**")),
    ("database_migration", ("**/migrations/**", "**/migrate/**", "**/alembic/**")),
    ("infrastructure", ("infra/**", "**/terraform/**", "**/helm/**", "**/k8s/**",
                        "**/.github/workflows/**", "**/Dockerfile*")),
    ("customer_data", ("**/customers/**", "**/customer_data/**")),
)


def _bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in _TRUE


@dataclass(frozen=True)
class Settings:
    # --- storage (secret: env) ---
    database_url: str = "sqlite+pysqlite:///:memory:"

    # --- webhook signing secrets (secret: env) ---
    linear_webhook_secret: str = ""
    github_webhook_secret: str | None = None

    # --- outbound API tokens (secret: env); None => connector not wired live ---
    linear_api_token: str | None = None
    github_api_token: str | None = None

    # --- Jira tracker (base url: yaml or env; credentials + secret: env) ---
    # Jira webhooks carry no HMAC signature; the shared secret is checked as a
    # constant-time token comparison instead. None => endpoint disabled.
    jira_webhook_secret: str | None = None
    jira_base_url: str | None = None
    jira_email: str | None = None
    jira_api_token: str | None = None

    # --- GitLab SCM (secret: env); None => endpoint disabled ---
    # GitLab webhooks send the shared secret verbatim in X-Gitlab-Token.
    gitlab_webhook_secret: str | None = None
    # Outbound API token to fetch MR diffs so GitLab MRs run the same
    # file-based gates as GitHub PRs. None => MRs are diff-blind (gates skipped).
    gitlab_api_token: str | None = None
    # API root; override for self-managed GitLab (e.g. https://gitlab.example.com/api/v4).
    gitlab_api_base: str = "https://gitlab.com/api/v4"

    # --- Slack approvals (secret: env); None => /webhooks/slack disabled ---
    # Slack signs interactivity requests with this signing secret (v0 scheme);
    # approvers are then keyed by Slack user id rather than email.
    slack_signing_secret: str | None = None
    # --- Slack outbound notifications (token: env; channel: yaml or env) ---
    # Bot token (xoxb-...) Foundry posts approval messages + status updates with.
    # Fail-closed: outbound Slack is wired only when BOTH the bot token AND a
    # channel are set; either missing => no notifier (silent, like no tracker).
    slack_bot_token: str | None = None
    slack_channel: str | None = None

    # --- API auth (secret: env); None => mutating API endpoints are disabled ---
    api_token: str | None = None

    # --- issue tracker (behaviour: yaml) ---
    # "linear" (default), "github_issues" (the issue is the ticket; approvers
    # are then keyed by GitHub login instead of email), or "jira".
    tracker_provider: str = "linear"

    # --- coding agent (behaviour: yaml; tokens: env) ---
    # Which CodingAgentProvider receives approved work. See foundry.agents.
    agent_provider: str = "manual"
    cursor_api_token: str | None = None
    claude_workflow_file: str = "foundry-claude-code.yml"
    agent_webhook_url: str | None = None
    agent_webhook_secret: str | None = None

    # --- intelligence (behaviour: yaml) ---
    use_openai_analyzer: bool = False
    openai_model: str = "gpt-5.5"
    # Risk classification backend. "heuristic" is deterministic keywords/globs;
    # "llm" adds an escalate-only LLM pass with cited evidence on top of that
    # same heuristic floor (needs OPENAI_API_KEY).
    risk_provider: str = "heuristic"
    risk_model: str = "gpt-5.5"

    # --- policy / safety knobs (behaviour: yaml) ---
    repo_confidence_threshold: int = 70
    max_files_changed: int = 12
    forbidden_globs: tuple[str, ...] = DEFAULT_FORBIDDEN_GLOBS
    # area name -> path globs; a PR touching these paths is treated as touching
    # that sensitive area even when the ticket text never mentioned it.
    sensitive_path_globs: tuple[tuple[str, tuple[str, ...]], ...] = (
        DEFAULT_SENSITIVE_PATH_GLOBS
    )

    # --- remediation / feedback loop (behaviour: yaml) ---
    # When CI fails or a reviewer requests changes, Foundry can re-dispatch the
    # agent with the failure context - still through the policy gate, and never
    # more than max_agent_retries times per run.
    max_agent_retries: int = 2
    retry_on: tuple[str, ...] = ("ci_failed", "changes_requested")
    # Deny further agent retries once a run's provider-reported spend reaches
    # this many USD. None = no budget cap.
    max_cost_per_run: float | None = None

    # --- triggers (behaviour: yaml) ---
    trigger_label: str = DEFAULT_TRIGGER_LABEL
    trigger_status: str = DEFAULT_TRIGGER_STATUS

    # --- approval (behaviour: yaml) ---
    # Who may approve runs. Role grants are configured per user, never asserted
    # by the API caller: (email, (role, ...)). A user with no roles can approve
    # ordinary work but cannot satisfy sensitive-area approval requirements.
    approvers: tuple[tuple[str, tuple[str, ...]], ...] = ()

    # --- context enrichment (behaviour: yaml) ---
    context_provider: str = "static"          # "static" | "catalog" | "code"
    context_org: str | None = None            # GitHub org for foundry-catalog sync
    context_repo_keywords: tuple[tuple[str, tuple[str, ...]], ...] = ()
    context_max_catalog_age_days: int = 7
    context_sync_call_budget: int = 3000
    # Gather code facts (file tree, CODEOWNERS, manifests) during catalog sync.
    # Implied by context_provider == "code"; costs up to 9 API calls per repo
    # instead of 3.
    context_sync_code_facts: bool = False
    context_tree_max_paths: int = 2000        # stored tree paths per repo (capped)

    # --- delivery memory (behaviour: yaml) ---
    # Historical routing priors mined from finished runs ("14 of 16 of this
    # team's tickets merged in billing-service"). Only active with the catalog
    # context provider; inert until enough outcomes exist. The cap keeps
    # history below an explicit repo association on the ticket (90).
    memory_priors_enabled: bool = True
    memory_min_samples: int = 3
    memory_confidence_cap: int = 89

    # --- durable execution (behaviour: yaml; address often env) ---
    temporal_address: str = "localhost:7233"
    task_queue: str = "foundry-ticket-to-pr"

    # ----------------------------------------------------------------- loaders
    @classmethod
    def load(
        cls,
        path: str | os.PathLike[str] | None = None,
        *,
        env: Mapping[str, str] | None = None,
    ) -> "Settings":
        """Build settings from defaults, then YAML, then environment overrides."""
        settings = cls()
        if path is not None and Path(path).exists():
            settings = settings._with(_from_yaml(Path(path)))
        settings = settings._with(_from_env(env or os.environ))
        settings._validate()
        return settings

    def _validate(self) -> None:
        if not (0 <= self.repo_confidence_threshold <= 100):
            raise ValueError(
                f"repo_confidence_threshold must be 0-100, got {self.repo_confidence_threshold}"
            )
        if self.max_files_changed < 1:
            raise ValueError(
                f"max_files_changed must be >= 1, got {self.max_files_changed}"
            )
        if self.max_agent_retries < 0:
            raise ValueError(
                f"max_agent_retries must be >= 0, got {self.max_agent_retries}"
            )
        unknown = set(self.retry_on) - {"ci_failed", "changes_requested"}
        if unknown:
            raise ValueError(f"unknown retry_on triggers: {sorted(unknown)}")
        if self.max_cost_per_run is not None and self.max_cost_per_run <= 0:
            raise ValueError(
                f"max_cost_per_run must be positive, got {self.max_cost_per_run}"
            )
        if self.risk_provider not in ("heuristic", "llm"):
            raise ValueError(
                f"risk_provider must be 'heuristic' or 'llm', got {self.risk_provider!r}"
            )
        if self.context_provider not in ("static", "catalog", "code"):
            raise ValueError(
                "context_provider must be 'static', 'catalog' or 'code', "
                f"got {self.context_provider!r}"
            )
        if self.context_max_catalog_age_days < 1:
            raise ValueError(
                f"context_max_catalog_age_days must be >= 1, got {self.context_max_catalog_age_days}"
            )
        if self.context_sync_call_budget < 1:
            raise ValueError(
                f"context_sync_call_budget must be >= 1, got {self.context_sync_call_budget}"
            )
        if self.context_tree_max_paths < 100:
            raise ValueError(
                f"context_tree_max_paths must be >= 100, got {self.context_tree_max_paths}"
            )
        if self.memory_min_samples < 1:
            raise ValueError(
                f"memory_min_samples must be >= 1, got {self.memory_min_samples}"
            )
        if not (0 <= self.memory_confidence_cap <= 100):
            raise ValueError(
                f"memory_confidence_cap must be 0-100, got {self.memory_confidence_cap}"
            )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        """Defaults overlaid with environment only (no YAML)."""
        return cls().load(env=env)

    def _with(self, overrides: Mapping[str, Any]) -> "Settings":
        valid = {k: v for k, v in overrides.items() if k in asdict(self)}
        return replace(self, **valid)

    # ------------------------------------------------------------- accessors
    @property
    def approver_emails(self) -> frozenset[str]:
        return frozenset(email for email, _roles in self.approvers)

    def roles_for(self, user: str) -> frozenset[str]:
        """Roles configured for ``user``; empty when unknown or role-less."""
        for email, roles in self.approvers:
            if email == user:
                return frozenset(roles)
        return frozenset()

    @property
    def sensitive_globs_map(self) -> dict[str, tuple[str, ...]]:
        return {area: globs for area, globs in self.sensitive_path_globs}


def _from_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - PyYAML is a core dependency
        raise RuntimeError("PyYAML is required to read a YAML config file") from exc

    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML mapping at the top level")

    out: dict[str, Any] = {}
    storage = data.get("database", {}) or {}
    if "url" in storage:
        out["database_url"] = storage["url"]

    analyzer = data.get("analyzer", {}) or {}
    if "provider" in analyzer:
        out["use_openai_analyzer"] = analyzer["provider"] == "openai"
    if "model" in analyzer:
        out["openai_model"] = analyzer["model"]

    risk = data.get("risk", {}) or {}
    if "provider" in risk:
        out["risk_provider"] = risk["provider"]
    if "model" in risk:
        out["risk_model"] = risk["model"]

    agent = data.get("agent", {}) or {}
    if "provider" in agent:
        out["agent_provider"] = agent["provider"]
    if "claude_workflow_file" in agent:
        out["claude_workflow_file"] = agent["claude_workflow_file"]

    tracker = data.get("tracker", {}) or {}
    if "provider" in tracker:
        out["tracker_provider"] = tracker["provider"]
    if "jira_base_url" in tracker:
        out["jira_base_url"] = tracker["jira_base_url"]

    policy = data.get("policy", {}) or {}
    if "repo_confidence_threshold" in policy:
        out["repo_confidence_threshold"] = int(policy["repo_confidence_threshold"])
    if "max_files_changed" in policy:
        out["max_files_changed"] = int(policy["max_files_changed"])
    if "forbidden_globs" in policy:
        out["forbidden_globs"] = tuple(policy["forbidden_globs"])
    if "sensitive_path_globs" in policy:
        out["sensitive_path_globs"] = tuple(
            (str(area), tuple(globs))
            for area, globs in (policy["sensitive_path_globs"] or {}).items()
        )

    remediation = data.get("remediation", {}) or {}
    if "max_agent_retries" in remediation:
        out["max_agent_retries"] = int(remediation["max_agent_retries"])
    if "retry_on" in remediation:
        out["retry_on"] = tuple(remediation["retry_on"] or [])

    budget = data.get("budget", {}) or {}
    if "max_cost_per_run" in budget:
        raw_cap = budget["max_cost_per_run"]
        out["max_cost_per_run"] = None if raw_cap is None else float(raw_cap)

    triggers = data.get("triggers", {}) or {}
    if "label" in triggers:
        out["trigger_label"] = triggers["label"]
    if "status" in triggers:
        out["trigger_status"] = triggers["status"]

    approval = data.get("approval", {}) or {}
    if "approvers" in approval:
        out["approvers"] = tuple(
            (entry["email"], tuple(entry.get("roles", []) or []))
            for entry in (approval["approvers"] or [])
        )
    elif "authorised_approvers" in approval:
        # Legacy form: a flat list of emails, no role grants.
        out["approvers"] = tuple(
            (email, ()) for email in approval["authorised_approvers"]
        )

    memory = data.get("memory", {}) or {}
    if "priors_enabled" in memory:
        out["memory_priors_enabled"] = _bool(memory["priors_enabled"], default=True)
    if "min_samples" in memory:
        out["memory_min_samples"] = int(memory["min_samples"])
    if "confidence_cap" in memory:
        out["memory_confidence_cap"] = int(memory["confidence_cap"])

    notifications = data.get("notifications", {}) or {}
    if "slack_channel" in notifications:
        out["slack_channel"] = notifications["slack_channel"]

    temporal = data.get("temporal", {}) or {}
    if "address" in temporal:
        out["temporal_address"] = temporal["address"]
    if "task_queue" in temporal:
        out["task_queue"] = temporal["task_queue"]

    context = data.get("context", {}) or {}
    if "provider" in context:
        out["context_provider"] = context["provider"]
    if "org" in context:
        out["context_org"] = context["org"]
    if "max_catalog_age_days" in context:
        out["context_max_catalog_age_days"] = int(context["max_catalog_age_days"])
    if "sync_call_budget" in context:
        out["context_sync_call_budget"] = int(context["sync_call_budget"])
    if "sync_code_facts" in context:
        out["context_sync_code_facts"] = _bool(context["sync_code_facts"])
    if "tree_max_paths" in context:
        out["context_tree_max_paths"] = int(context["tree_max_paths"])
    if "repo_keywords" in context:
        out["context_repo_keywords"] = tuple(
            (str(repo), tuple(kws))
            for repo, kws in (context["repo_keywords"] or {}).items()
        )

    return out


def _from_env(env: Mapping[str, str]) -> dict[str, Any]:
    """Only keys actually present in the environment, so we never clobber YAML."""
    out: dict[str, Any] = {}
    mapping = {
        "FOUNDRY_DATABASE_URL": "database_url",
        "FOUNDRY_LINEAR_WEBHOOK_SECRET": "linear_webhook_secret",
        "FOUNDRY_GITHUB_WEBHOOK_SECRET": "github_webhook_secret",
        "FOUNDRY_LINEAR_API_TOKEN": "linear_api_token",
        "FOUNDRY_GITHUB_API_TOKEN": "github_api_token",
        "FOUNDRY_JIRA_WEBHOOK_SECRET": "jira_webhook_secret",
        "FOUNDRY_JIRA_BASE_URL": "jira_base_url",
        "FOUNDRY_JIRA_EMAIL": "jira_email",
        "FOUNDRY_JIRA_API_TOKEN": "jira_api_token",
        "FOUNDRY_GITLAB_WEBHOOK_SECRET": "gitlab_webhook_secret",
        "FOUNDRY_GITLAB_API_TOKEN": "gitlab_api_token",
        "FOUNDRY_GITLAB_API_BASE": "gitlab_api_base",
        "FOUNDRY_SLACK_SIGNING_SECRET": "slack_signing_secret",
        "FOUNDRY_SLACK_BOT_TOKEN": "slack_bot_token",
        "FOUNDRY_SLACK_CHANNEL": "slack_channel",
        "FOUNDRY_API_TOKEN": "api_token",
        "FOUNDRY_AGENT_PROVIDER": "agent_provider",
        "FOUNDRY_TRACKER_PROVIDER": "tracker_provider",
        "FOUNDRY_CURSOR_API_TOKEN": "cursor_api_token",
        "FOUNDRY_AGENT_WEBHOOK_URL": "agent_webhook_url",
        "FOUNDRY_AGENT_WEBHOOK_SECRET": "agent_webhook_secret",
        "FOUNDRY_OPENAI_MODEL": "openai_model",
        "FOUNDRY_RISK_PROVIDER": "risk_provider",
        "FOUNDRY_RISK_MODEL": "risk_model",
        "TEMPORAL_ADDRESS": "temporal_address",
        "FOUNDRY_TASK_QUEUE": "task_queue",
        "FOUNDRY_CONTEXT_PROVIDER": "context_provider",
        "FOUNDRY_CONTEXT_ORG": "context_org",
    }
    for env_key, field_name in mapping.items():
        if env_key in env:
            out[field_name] = env[env_key]
    if "FOUNDRY_USE_OPENAI_ANALYZER" in env:
        out["use_openai_analyzer"] = _bool(env["FOUNDRY_USE_OPENAI_ANALYZER"])
    return out
