"""Tests for live user-loadable policy bundles (issue #154).

The bundle is merged on top of the resolved config as a strictly-additive
overlay: the base config + built-in gate rules are a non-overridable floor, so a
bundle can only ever make the gate *stricter*. These tests prove that property
per knob, prove a hostile bundle cannot weaken the floor on *any* knob, and
exercise the end-to-end ``Settings.load`` wiring.
"""

from __future__ import annotations

import tempfile
import textwrap
from pathlib import Path

import pytest

from foundry.config import Settings
from foundry.policy.freeze import window_key
from foundry.policy.library import compare_policy_strictness
from foundry.policy.overlay import (
    POLICY_OVERLAY_FIELDS,
    apply_policy_bundle,
    load_policy_bundle,
    overlay_policy_values,
)


def _base() -> Settings:
    """A deliberately strict base config to overlay against."""
    return Settings(
        repo_confidence_threshold=80,
        max_files_changed=8,
        min_approvals=2,
        max_agent_retries=1,
        max_cost_per_run=10.0,
        forbidden_globs=("infra/**", "**/secrets/**"),
        retry_on=("ci_failed",),
        repo_required_roles=(("payments-service", ("security",)),),
        repo_forbidden_globs=(("payments-service", ("**/ledger/**",)),),
        path_required_roles=(("**/billing/**", ("security",)),),
        repo_min_approvals=(("payments-service", 3),),
    )


# --------------------------------------------------------------------------- #
# Per-knob merge direction
# --------------------------------------------------------------------------- #
def test_scalar_knobs_merge_to_the_stricter_value() -> None:
    base = _base()
    merged = overlay_policy_values(
        base,
        {
            "repo_confidence_threshold": 95,  # higher is stricter -> wins
            "max_files_changed": 4,  # lower is stricter -> wins
            "min_approvals": 3,  # higher is stricter -> wins
            "max_agent_retries": 0,  # lower is stricter -> wins
            "max_cost_per_run": 5.0,  # lower cap is stricter -> wins
        },
    )
    assert merged["repo_confidence_threshold"] == 95
    assert merged["max_files_changed"] == 4
    assert merged["min_approvals"] == 3
    assert merged["max_agent_retries"] == 0
    assert merged["max_cost_per_run"] == 5.0


def test_scalar_weakenings_are_ignored_floor_wins() -> None:
    base = _base()
    merged = overlay_policy_values(
        base,
        {
            "repo_confidence_threshold": 10,  # lower would be weaker -> ignored
            "max_files_changed": 50,  # higher would be weaker -> ignored
            "min_approvals": 1,  # lower would be weaker -> ignored
            "max_agent_retries": 9,  # higher would be weaker -> ignored
            "max_cost_per_run": 999.0,  # looser cap -> ignored
        },
    )
    assert merged["repo_confidence_threshold"] == 80
    assert merged["max_files_changed"] == 8
    assert merged["min_approvals"] == 2
    assert merged["max_agent_retries"] == 1
    assert merged["max_cost_per_run"] == 10.0


def test_cap_none_in_bundle_keeps_base_cap() -> None:
    # The bundle dropping the cap (None = no cap = weakest) cannot loosen one the
    # base already set.
    merged = overlay_policy_values(_base(), {"max_cost_per_run": None})
    assert merged["max_cost_per_run"] == 10.0


def test_cap_set_by_bundle_when_base_has_none() -> None:
    base = Settings(max_cost_per_run=None)
    merged = overlay_policy_values(base, {"max_cost_per_run": 25.0})
    assert merged["max_cost_per_run"] == 25.0


def test_forbidden_globs_union_keeps_base_and_adds_extras() -> None:
    base = _base()
    merged = overlay_policy_values(
        base, {"forbidden_globs": ("**/keys/**", "infra/**")}
    )
    # Base globs preserved (can't be dropped), bundle's new one appended once.
    assert merged["forbidden_globs"] == ("infra/**", "**/secrets/**", "**/keys/**")


def test_map_knobs_union_per_key() -> None:
    base = _base()
    merged = overlay_policy_values(
        base,
        {
            "repo_required_roles": (
                ("payments-service", ("engineering",)),  # adds to existing repo
                ("identity-service", ("security",)),  # new repo
            ),
            "path_required_roles": (("services/identity/**", ("engineering",)),),
        },
    )
    roles = dict(merged["repo_required_roles"])
    assert set(roles["payments-service"]) == {"security", "engineering"}
    assert roles["identity-service"] == ("security",)
    paths = dict(merged["path_required_roles"])
    # Base path rule preserved, bundle path rule added.
    assert paths["**/billing/**"] == ("security",)
    assert paths["services/identity/**"] == ("engineering",)


def test_repo_min_approvals_takes_per_repo_max() -> None:
    base = _base()  # payments-service: 3
    merged = overlay_policy_values(
        base,
        {
            "repo_min_approvals": (
                ("payments-service", 2),  # lower -> base 3 wins
                ("identity-service", 4),  # new repo
            )
        },
    )
    counts = dict(merged["repo_min_approvals"])
    assert counts["payments-service"] == 3
    assert counts["identity-service"] == 4


def test_retry_on_intersection_can_only_narrow() -> None:
    base = _base()  # ("ci_failed",)
    # Bundle naming changes_requested too cannot ADD an autonomous-retry trigger.
    merged = overlay_policy_values(
        base, {"retry_on": ("ci_failed", "changes_requested")}
    )
    assert merged["retry_on"] == ("ci_failed",)
    # And it can narrow further (to none).
    merged_none = overlay_policy_values(_base(), {"retry_on": ()})
    assert merged_none["retry_on"] == ()


def test_change_freeze_windows_union_by_canonical_key() -> None:
    base = Settings.load(
        env={},
        path=_write(
            tmp_yaml(
                """
                policy:
                  change_freeze_windows:
                    - reason: "Weekend"
                      weekdays: ["sat", "sun"]
                      start: "00:00"
                      end: "23:59"
                      tz: "UTC"
                """
            )
        ),
    )
    (existing,) = base.change_freeze_windows
    bundle_windows = load_policy_bundle(
        _write(
            tmp_yaml(
                """
                policy:
                  change_freeze_windows:
                    - reason: "Weekend (renamed, same time)"
                      weekdays: ["sat", "sun"]
                      start: "00:00"
                      end: "23:59"
                      tz: "UTC"
                    - reason: "Year-end"
                      starts_at: "2026-12-20T00:00:00"
                      ends_at: "2027-01-02T00:00:00"
                      tz: "UTC"
                """
            )
        )
    )["change_freeze_windows"]
    merged = overlay_policy_values(
        base, {"change_freeze_windows": bundle_windows}
    )["change_freeze_windows"]
    # The re-worded duplicate (same canonical key) does not double up; the new
    # window is added. So two windows total, not three.
    assert len(merged) == 2
    keys = {window_key(w) for w in merged}
    assert window_key(existing) in keys


# --------------------------------------------------------------------------- #
# The headline guarantee: built-ins remain a non-overridable floor
# --------------------------------------------------------------------------- #
def test_hostile_bundle_cannot_weaken_any_knob() -> None:
    """A bundle that tries to weaken *every* knob leaves the gate >= the floor."""
    base = _base()
    hostile = {
        "repo_confidence_threshold": 0,
        "max_files_changed": 9999,
        "min_approvals": 1,
        "max_agent_retries": 99,
        "max_cost_per_run": None,
        "forbidden_globs": (),  # try to clear protected paths
        "retry_on": ("ci_failed", "changes_requested"),  # try to add a trigger
        "repo_required_roles": (),  # try to drop the per-repo role
        "repo_forbidden_globs": (),
        "path_required_roles": (),
        "repo_min_approvals": (("payments-service", 1),),  # try to lower
    }
    merged = base._with(overlay_policy_values(base, hostile))
    comparison = compare_policy_strictness(merged, base)
    assert comparison.ok, comparison.weaknesses
    # And concretely: every floor value survived.
    assert merged.repo_confidence_threshold == 80
    assert merged.max_files_changed == 8
    assert merged.min_approvals == 2
    assert merged.max_agent_retries == 1
    assert merged.max_cost_per_run == 10.0
    assert set(merged.forbidden_globs) == {"infra/**", "**/secrets/**"}
    assert merged.retry_on == ("ci_failed",)
    assert dict(merged.repo_required_roles)["payments-service"] == ("security",)
    assert dict(merged.repo_min_approvals)["payments-service"] == 3


# --------------------------------------------------------------------------- #
# Fail-closed guards
# --------------------------------------------------------------------------- #
def test_non_policy_key_is_rejected() -> None:
    with pytest.raises(ValueError, match="only set policy knobs"):
        overlay_policy_values(_base(), {"database_url": "sqlite://"})


def test_overlay_fields_are_a_subset_of_settings_fields() -> None:
    from dataclasses import fields

    names = {f.name for f in fields(Settings)}
    assert POLICY_OVERLAY_FIELDS <= names


def test_missing_bundle_file_raises() -> None:
    with pytest.raises(ValueError, match="does not exist"):
        load_policy_bundle("/nonexistent/bundle.yaml")


def test_apply_rejects_bundle_referencing_another_bundle(tmp_path: Path) -> None:
    # bundle_path is not an overlay-eligible knob, so a bundle that tries to set
    # it (recursion / smuggling) is rejected.
    bundle = tmp_path / "bundle.yaml"
    bundle.write_text("policy:\n  bundle_path: other.yaml\n")
    base = Settings(policy_bundle_path=str(bundle))
    with pytest.raises(ValueError, match="only set policy knobs"):
        apply_policy_bundle(base)


# --------------------------------------------------------------------------- #
# End-to-end Settings.load wiring
# --------------------------------------------------------------------------- #
def test_settings_load_applies_bundle_overlay(tmp_path: Path) -> None:
    bundle = tmp_path / "security-bundle.yaml"
    bundle.write_text(
        """
policy:
  repo_confidence_threshold: 90
  forbidden_globs:
    - "**/keys/**"
  min_approvals: 2
  repo_required_roles:
    identity-service: ["security"]
"""
    )
    base_body = """
policy:
  repo_confidence_threshold: 70
  forbidden_globs:
    - "infra/**"
  min_approvals: 1
"""
    base_config = tmp_path / "base.yaml"
    base_config.write_text(base_body)
    config = tmp_path / "foundry.yaml"
    config.write_text(base_body + f"  bundle_path: {bundle}\n")

    settings = Settings.load(config, env={})
    # Stricter values from the bundle win; base protected paths are preserved.
    assert settings.repo_confidence_threshold == 90
    assert settings.min_approvals == 2
    assert set(settings.forbidden_globs) == {"infra/**", "**/keys/**"}
    assert dict(settings.repo_required_roles)["identity-service"] == ("security",)
    # The overlaid config is provably >= the same config loaded WITHOUT the
    # bundle (its non-overridable floor): the bundle only ever tightened the gate.
    pre_overlay = Settings.load(base_config, env={})
    assert compare_policy_strictness(settings, pre_overlay).ok


def test_settings_load_without_bundle_is_unchanged(tmp_path: Path) -> None:
    config = tmp_path / "foundry.yaml"
    config.write_text("policy:\n  min_approvals: 2\n")
    settings = Settings.load(config, env={})
    assert settings.policy_bundle_path is None
    assert settings.min_approvals == 2


def test_settings_load_missing_bundle_fails_loud(tmp_path: Path) -> None:
    config = tmp_path / "foundry.yaml"
    config.write_text("policy:\n  bundle_path: /nope/missing.yaml\n")
    with pytest.raises(ValueError, match="does not exist"):
        Settings.load(config, env={})


def test_bundle_path_via_env(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle.yaml"
    bundle.write_text("policy:\n  min_approvals: 3\n")
    settings = Settings.load(env={"FOUNDRY_POLICY_BUNDLE_PATH": str(bundle)})
    assert settings.min_approvals == 3


# --------------------------------------------------------------------------- #
# Tiny helpers for the YAML-on-disk tests above
# --------------------------------------------------------------------------- #
def tmp_yaml(body: str) -> str:
    return textwrap.dedent(body)


def _write(text: str) -> str:
    handle = tempfile.NamedTemporaryFile(
        "w", suffix=".yaml", delete=False, encoding="utf-8"
    )
    handle.write(text)
    handle.close()
    return handle.name
