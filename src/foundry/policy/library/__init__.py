"""Starter policy library: vetted, copy-to-adopt policy presets (issue #31).

A regulated buyer's first question is "where do I start?" - the policy knobs
exist (``policy.forbidden_globs``, ``policy.repo_forbidden_globs``,
``policy.repo_required_roles``, ``repo_confidence_threshold``, the
retry/budget caps), but a blank ``foundry.yaml`` is a guessing game. This module
ships a small library of **committed, tested preset configs** an operator can
read, copy into their own config, and adapt.

Two things to be clear about, because they are the whole safety story:

- **The presets only *use* knobs that already exist and are already gated.** They
  add no new policy mechanism, touch neither ``policy/engine.py`` nor
  ``foundry.rego``, and write no audit rows - so there is no Python/Rego
  lock-step concern here (invariant #2) and nothing in the gate changes.
- **They are copy-to-adopt, not auto-applied.** Loading the library never alters
  a running deployment's policy; an operator opts in by copying a preset's YAML
  into ``foundry.yaml`` (or pointing ``FOUNDRY_CONFIG`` at it). So the library
  cannot silently *weaken* a gate (invariant #1) - it is inert until adopted, and
  every preset is itself a strict-or-stricter starting point relative to the
  built-in defaults.

The loader is pure and offline: presets are packaged YAML read through
``importlib.resources`` (no network, no DB), and ``load_preset_settings`` proves
each one parses into a valid :class:`~foundry.config.Settings`.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from importlib import resources
from typing import TYPE_CHECKING, Any, Mapping

if TYPE_CHECKING:  # pragma: no cover - typing only
    from foundry.config import Settings

_PACKAGE = "foundry.policy.library"

# name (CLI-friendly) -> (filename, one-line summary). The display registry is
# explicit (not scraped from comments) so the catalogue is stable and testable;
# a test asserts it agrees with the YAML files actually on disk in both
# directions (every registered file exists, every shipped file is registered).
_PRESETS: tuple[tuple[str, str, str], ...] = (
    (
        "baseline",
        "baseline.yaml",
        "Conservative, broadly-applicable safe floor: the built-in protected "
        "paths made explicit, a confident-routing threshold, capped retries and "
        "a per-run spend cap.",
    ),
    (
        "soc2",
        "soc2.yaml",
        "SOC 2 change-management starting point: stricter routing confidence, an "
        "expanded protected-path list (infra/CI/secrets), per-repo security "
        "sign-off on sensitive services, tight retry and budget caps.",
    ),
    (
        "change-management",
        "change_management.yaml",
        "ITIL-style formal change management: high routing confidence, "
        "release/deploy paths kept off-limits to the agent, engineering sign-off "
        "on infrastructure repos, single-retry and a conservative budget cap.",
    ),
)


@dataclass(frozen=True)
class PolicyPreset:
    """A single starter preset: its name, summary and raw YAML body."""

    name: str
    summary: str
    filename: str
    yaml_text: str


def available_preset_names() -> list[str]:
    """The preset names the library ships, in catalogue order."""
    return [name for name, _filename, _summary in _PRESETS]


def _registry_entry(name: str) -> tuple[str, str, str]:
    for entry in _PRESETS:
        if entry[0] == name:
            return entry
    available = ", ".join(available_preset_names())
    raise ValueError(
        f"unknown policy preset {name!r}; available presets: {available}"
    )


def load_preset_yaml(name: str) -> str:
    """Return the raw YAML body of preset ``name`` (for ``show`` / copy-to-adopt)."""
    _name, filename, _summary = _registry_entry(name)
    return resources.files(_PACKAGE).joinpath(filename).read_text(encoding="utf-8")


def get_preset(name: str) -> PolicyPreset:
    """Return the full :class:`PolicyPreset` (metadata + YAML body)."""
    canonical, filename, summary = _registry_entry(name)
    return PolicyPreset(
        name=canonical,
        summary=summary,
        filename=filename,
        yaml_text=load_preset_yaml(canonical),
    )


def list_presets() -> list[PolicyPreset]:
    """Every shipped preset, in catalogue order."""
    return [get_preset(name) for name in available_preset_names()]


def load_preset_settings(
    name: str, *, env: Mapping[str, str] | None = None
) -> "Settings":
    """Load preset ``name`` into a validated :class:`Settings`.

    This is how a test (or ``explain``) proves a preset is real, adoptable config
    and not just a comment block: it goes through the *same* ``Settings.load``
    path an operator's ``foundry.yaml`` does, so a typo'd knob or an unknown
    approval role fails here exactly as it would at deploy time.

    ``env`` defaults to an **empty** mapping (not ``os.environ``) so the loaded
    settings reflect the preset itself, not whatever secrets/overrides happen to
    be set in the calling process.
    """
    from foundry.config import Settings

    text = load_preset_yaml(name)
    # Settings.load reads a path; write the packaged body to a temp file so we
    # reuse the real loader/validator rather than re-implementing YAML parsing.
    handle = tempfile.NamedTemporaryFile(
        "w", suffix=".yaml", delete=False, encoding="utf-8"
    )
    try:
        handle.write(text)
        handle.close()
        return Settings.load(handle.name, env=env or {})
    finally:
        os.unlink(handle.name)


def effective_policy_summary(settings: "Settings") -> dict[str, Any]:
    """The gate-relevant knobs a preset (or any config) resolves to.

    Decision-support only: a flat, printable view of *what the gate will
    enforce* under this config - the threshold, protected paths, per-repo
    overrides and the retry/budget caps - so an operator can see a preset's
    effect without standing up a run.
    """
    return {
        "repo_confidence_threshold": settings.repo_confidence_threshold,
        "max_files_changed": settings.max_files_changed,
        "forbidden_globs": list(settings.forbidden_globs),
        "repo_forbidden_globs": {
            repo: list(globs) for repo, globs in settings.repo_forbidden_globs
        },
        "repo_required_roles": {
            repo: list(roles) for repo, roles in settings.repo_required_roles
        },
        "max_agent_retries": settings.max_agent_retries,
        "retry_on": list(settings.retry_on),
        "max_cost_per_run": settings.max_cost_per_run,
        "estimated_cost_per_dispatch": settings.estimated_cost_per_dispatch,
        "approver_count": len(settings.approvers),
    }


__all__ = [
    "PolicyPreset",
    "available_preset_names",
    "get_preset",
    "list_presets",
    "load_preset_yaml",
    "load_preset_settings",
    "effective_policy_summary",
]
