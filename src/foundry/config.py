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
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping

_TRUE = {"1", "true", "yes", "on"}

DEFAULT_FORBIDDEN_GLOBS = ("infra/**", "migrations/**", "**/.env*", "**/secrets/**")
DEFAULT_TRIGGER_LABEL = "foundry:candidate"
DEFAULT_TRIGGER_STATUS = "Ready for AI Analysis"


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

    # --- intelligence (behaviour: yaml) ---
    use_openai_analyzer: bool = False
    openai_model: str = "gpt-4o"

    # --- policy / safety knobs (behaviour: yaml) ---
    repo_confidence_threshold: int = 70
    max_files_changed: int = 12
    forbidden_globs: tuple[str, ...] = DEFAULT_FORBIDDEN_GLOBS

    # --- triggers (behaviour: yaml) ---
    trigger_label: str = DEFAULT_TRIGGER_LABEL
    trigger_status: str = DEFAULT_TRIGGER_STATUS

    # --- approval (behaviour: yaml) ---
    authorised_approvers: tuple[str, ...] = ()

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

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        """Defaults overlaid with environment only (no YAML)."""
        return cls().load(env=env)

    def _with(self, overrides: Mapping[str, Any]) -> "Settings":
        valid = {k: v for k, v in overrides.items() if k in asdict(self)}
        return replace(self, **valid)


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

    policy = data.get("policy", {}) or {}
    if "repo_confidence_threshold" in policy:
        out["repo_confidence_threshold"] = int(policy["repo_confidence_threshold"])
    if "max_files_changed" in policy:
        out["max_files_changed"] = int(policy["max_files_changed"])
    if "forbidden_globs" in policy:
        out["forbidden_globs"] = tuple(policy["forbidden_globs"])

    triggers = data.get("triggers", {}) or {}
    if "label" in triggers:
        out["trigger_label"] = triggers["label"]
    if "status" in triggers:
        out["trigger_status"] = triggers["status"]

    approval = data.get("approval", {}) or {}
    if "authorised_approvers" in approval:
        out["authorised_approvers"] = tuple(approval["authorised_approvers"])

    temporal = data.get("temporal", {}) or {}
    if "address" in temporal:
        out["temporal_address"] = temporal["address"]
    if "task_queue" in temporal:
        out["task_queue"] = temporal["task_queue"]

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
        "FOUNDRY_OPENAI_MODEL": "openai_model",
        "TEMPORAL_ADDRESS": "temporal_address",
        "FOUNDRY_TASK_QUEUE": "task_queue",
    }
    for env_key, field_name in mapping.items():
        if env_key in env:
            out[field_name] = env[env_key]
    if "FOUNDRY_USE_OPENAI_ANALYZER" in env:
        out["use_openai_analyzer"] = _bool(env["FOUNDRY_USE_OPENAI_ANALYZER"])
    return out
