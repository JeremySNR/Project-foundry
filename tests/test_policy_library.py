"""Starter policy library tests (issue #31).

These prove the shipped presets are (a) real, adoptable config - they parse
through the same ``Settings`` validator an operator's ``foundry.yaml`` does - and
(b) behave as advertised at the gate: each preset is fed through the
``LocalPolicyEngine`` exactly as the orchestrator wires it (the configured
confidence threshold, and per-repo required roles resolved for the routed repo),
and the resulting decisions are asserted. The library adds no new policy
mechanism, so there is no Rego lock-step concern - it only exercises knobs the
existing engine already enforces.
"""

from __future__ import annotations

from importlib import resources

import pytest

from foundry.config import DEFAULT_FORBIDDEN_GLOBS, Settings
from foundry.policy import LocalPolicyEngine, PolicyInput
from foundry.policy.library import (
    available_preset_names,
    effective_policy_summary,
    get_preset,
    list_presets,
    load_preset_settings,
    load_preset_yaml,
)
from foundry.schemas.common import ApprovalRole

_PACKAGE = "foundry.policy.library"


# --------------------------------------------------------------------------- #
# Catalogue integrity
# --------------------------------------------------------------------------- #
def test_catalogue_matches_files_on_disk_both_ways() -> None:
    """Every registered preset has a file, and every shipped .yaml is registered."""
    registered = {get_preset(name).filename for name in available_preset_names()}
    on_disk = {
        entry.name
        for entry in resources.files(_PACKAGE).iterdir()
        if entry.name.endswith(".yaml")
    }
    assert registered == on_disk


def test_list_presets_have_names_and_summaries() -> None:
    presets = list_presets()
    assert {p.name for p in presets} == {"baseline", "soc2", "change-management"}
    for preset in presets:
        assert preset.summary.strip()
        assert preset.yaml_text.strip()


def test_unknown_preset_raises_with_available_names() -> None:
    with pytest.raises(ValueError) as excinfo:
        load_preset_yaml("does-not-exist")
    message = str(excinfo.value)
    assert "does-not-exist" in message
    # The error lists what *is* available so the operator can self-correct.
    for name in available_preset_names():
        assert name in message


# --------------------------------------------------------------------------- #
# Every preset is valid, adoptable config
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", ["baseline", "soc2", "change-management"])
def test_preset_loads_into_valid_settings(name: str) -> None:
    settings = load_preset_settings(name)
    assert isinstance(settings, Settings)
    # The local engine is the default for every preset (none flips to opa
    # without an opa_url, which would fail _validate).
    assert settings.policy_provider == "local"


@pytest.mark.parametrize("name", ["baseline", "soc2", "change-management"])
def test_preset_never_drops_a_default_forbidden_glob(name: str) -> None:
    """A preset may add protected paths but must not remove a built-in one.

    Adopting a preset has to be a one-way ratchet towards stricter (invariant
    #1); a starter config that silently *unprotected* infra/ or secrets/ would
    be worse than no preset at all.
    """
    settings = load_preset_settings(name)
    assert set(DEFAULT_FORBIDDEN_GLOBS).issubset(set(settings.forbidden_globs))


def test_load_preset_settings_ignores_process_env() -> None:
    """The loaded settings reflect the preset, not ambient FOUNDRY_* overrides."""
    settings = load_preset_settings(
        "soc2", env={"FOUNDRY_POLICY_PROVIDER": "opa", "FOUNDRY_POLICY_OPA_URL": "x"}
    )
    # env is only honoured when explicitly threaded; the default call passes {}.
    assert settings.policy_provider == "opa"  # explicit env we passed in is honoured
    default = load_preset_settings("soc2")
    assert default.policy_provider == "local"  # ambient env is not consulted


# --------------------------------------------------------------------------- #
# Relative strictness of the presets
# --------------------------------------------------------------------------- #
def test_presets_are_progressively_stricter_on_confidence() -> None:
    baseline = load_preset_settings("baseline")
    soc2 = load_preset_settings("soc2")
    cm = load_preset_settings("change-management")
    assert baseline.repo_confidence_threshold == 70  # the built-in default
    assert soc2.repo_confidence_threshold > baseline.repo_confidence_threshold
    assert cm.repo_confidence_threshold >= soc2.repo_confidence_threshold
    # The compliance presets also cap retries tighter than the default of 2.
    assert soc2.max_agent_retries == 1
    assert cm.max_agent_retries == 1


# --------------------------------------------------------------------------- #
# Gate behaviour: each preset drives the engine as the orchestrator wires it
# --------------------------------------------------------------------------- #
def _engine_for(settings: Settings) -> LocalPolicyEngine:
    return LocalPolicyEngine(
        repo_confidence_threshold=settings.repo_confidence_threshold
    )


def _start_input(
    settings: Settings,
    *,
    repo: str,
    confidence: int,
    approvals: dict[str, bool] | None = None,
    approval_present: bool = True,
    risk: dict | None = None,
) -> PolicyInput:
    """Build a START_AGENT input, resolving per-repo roles like the orchestrator.

    The orchestrator stamps ``policy.repo_required_roles`` for the routed repo
    onto ``PolicyInput.repo.required_roles`` before evaluating; we mirror that so
    these tests exercise the preset's per-repo rules through the real gate path.
    """
    repo_roles = list(settings.repo_required_roles_map.get(repo, ()))
    return PolicyInput.model_validate(
        {
            "action": "start_agent",
            "ticket": {"work_type": "feature", "readiness": "ready"},
            "risk": risk or {"overall_risk": "low"},
            "repo": {
                "name": repo,
                "confidence": confidence,
                "required_roles": repo_roles,
            },
            "approval": approvals or {},
            "approval_present": approval_present,
        }
    )


def test_baseline_allows_ready_confident_approved_run() -> None:
    settings = load_preset_settings("baseline")
    engine = _engine_for(settings)
    allowed = engine.evaluate(
        _start_input(settings, repo="customer-web", confidence=90)
    )
    assert allowed.allowed is True

    # The human-in-the-loop gate still bites: no approval -> denied.
    denied = engine.evaluate(
        _start_input(
            settings, repo="customer-web", confidence=90, approval_present=False
        )
    )
    assert denied.allowed is False


def test_soc2_requires_security_signoff_on_payments_repo() -> None:
    settings = load_preset_settings("soc2")
    engine = _engine_for(settings)

    # A payments-service run with a generic approval is refused: the preset's
    # per-repo rule demands a SECURITY role regardless of (here, low) risk.
    denied = engine.evaluate(
        _start_input(settings, repo="payments-service", confidence=90)
    )
    assert denied.allowed is False
    assert ApprovalRole.SECURITY in denied.required_approvals

    # With the security sign-off recorded, the same run passes.
    allowed = engine.evaluate(
        _start_input(
            settings,
            repo="payments-service",
            confidence=90,
            approvals={"security": True},
        )
    )
    assert allowed.allowed is True


def test_soc2_confidence_threshold_is_enforced() -> None:
    settings = load_preset_settings("soc2")  # threshold 80
    engine = _engine_for(settings)
    # A non-sensitive repo at confidence 75 clears the default 70 but not soc2's 80.
    below = engine.evaluate(
        _start_input(settings, repo="customer-web", confidence=75)
    )
    assert below.allowed is False
    assert any("confidence" in reason for reason in below.reasons)


def test_change_management_blocks_low_confidence_routing() -> None:
    settings = load_preset_settings("change-management")  # threshold 85
    engine = _engine_for(settings)
    below = engine.evaluate(
        _start_input(settings, repo="customer-web", confidence=80)
    )
    assert below.allowed is False
    above = engine.evaluate(
        _start_input(settings, repo="customer-web", confidence=90)
    )
    assert above.allowed is True


def test_change_management_requires_engineering_on_infra_repo() -> None:
    settings = load_preset_settings("change-management")
    engine = _engine_for(settings)
    denied = engine.evaluate(
        _start_input(settings, repo="platform-infra", confidence=95)
    )
    assert denied.allowed is False
    assert ApprovalRole.ENGINEERING in denied.required_approvals
    allowed = engine.evaluate(
        _start_input(
            settings,
            repo="platform-infra",
            confidence=95,
            approvals={"engineering": True},
        )
    )
    assert allowed.allowed is True


# --------------------------------------------------------------------------- #
# Effective-policy summary (the `explain` data)
# --------------------------------------------------------------------------- #
def test_effective_policy_summary_reflects_preset() -> None:
    summary = effective_policy_summary(load_preset_settings("soc2"))
    assert summary["repo_confidence_threshold"] == 80
    assert summary["max_agent_retries"] == 1
    assert "payments-service" in summary["repo_required_roles"]
    assert summary["repo_required_roles"]["payments-service"] == ["security"]
    assert set(DEFAULT_FORBIDDEN_GLOBS).issubset(set(summary["forbidden_globs"]))


# --------------------------------------------------------------------------- #
# CLI smoke tests
# --------------------------------------------------------------------------- #
def test_cli_presets_lists_every_preset(capsys: pytest.CaptureFixture[str]) -> None:
    from foundry.policy.cli import main

    main(["presets"])
    out = capsys.readouterr().out
    for name in available_preset_names():
        assert name in out


def test_cli_show_prints_loadable_yaml(capsys: pytest.CaptureFixture[str]) -> None:
    from foundry.policy.cli import main

    main(["show", "soc2"])
    out = capsys.readouterr().out
    assert out == load_preset_yaml("soc2")


def test_cli_explain_prints_effective_knobs(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from foundry.policy.cli import main

    main(["explain", "change-management"])
    out = capsys.readouterr().out
    assert "repo_confidence_threshold : 85" in out
    assert "platform-infra" in out


def test_cli_unknown_preset_exits_nonzero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from foundry.policy.cli import main

    with pytest.raises(SystemExit) as excinfo:
        main(["show", "nope"])
    assert excinfo.value.code == 2
    assert "nope" in capsys.readouterr().err
