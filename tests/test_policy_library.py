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
    compare_policy_strictness,
    effective_policy_summary,
    get_preset,
    list_presets,
    load_preset_settings,
    load_preset_yaml,
    resolve_settings,
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
    assert {p.name for p in presets} == {
        "baseline",
        "soc2",
        "change-management",
        "pci-dss",
    }
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
@pytest.mark.parametrize("name", ["baseline", "soc2", "change-management", "pci-dss"])
def test_preset_loads_into_valid_settings(name: str) -> None:
    settings = load_preset_settings(name)
    assert isinstance(settings, Settings)
    # The local engine is the default for every preset (none flips to opa
    # without an opa_url, which would fail _validate).
    assert settings.policy_provider == "local"


@pytest.mark.parametrize("name", ["baseline", "soc2", "change-management", "pci-dss"])
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


def test_pci_dss_requires_security_signoff_on_cardholder_repo() -> None:
    settings = load_preset_settings("pci-dss")
    engine = _engine_for(settings)

    # A cardholder-data-service run with only a generic approval is refused: the
    # preset's per-repo rule demands a SECURITY role regardless of (here, low)
    # risk - the engine path is identical to soc2's per-repo rule.
    denied = engine.evaluate(
        _start_input(settings, repo="cardholder-data-service", confidence=95)
    )
    assert denied.allowed is False
    assert ApprovalRole.SECURITY in denied.required_approvals

    # With the security sign-off recorded, the same run clears the gate.
    allowed = engine.evaluate(
        _start_input(
            settings,
            repo="cardholder-data-service",
            confidence=95,
            approvals={"security": True},
        )
    )
    assert allowed.allowed is True


def test_pci_dss_configures_the_modern_policy_knobs() -> None:
    """The N-of-M and per-path knobs (enforced in the orchestrator lifecycle,
    not the engine) parse as configured - the preset is real, adoptable config.
    """
    settings = load_preset_settings("pci-dss")
    # Separation of duties: a two-person rule org-wide, raised to three for the
    # key-management repo (effective minimum is max(global, per-repo)).
    assert settings.min_approvals == 2
    assert settings.repo_min_approvals_map["key-management-service"] == 3
    # Per-path security sign-off over cardholder / cryptographic subtrees.
    path_roles = settings.path_required_roles_map
    assert path_roles["**/cardholder/**"] == ("security",)
    assert path_roles["**/crypto/**"] == ("security",)
    # The crypto/key material is also a sticky forbidden-path block.
    assert "**/keys/**" in settings.forbidden_globs


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


def test_effective_policy_summary_surfaces_n_of_m_and_path_roles() -> None:
    """`explain` must reflect the whole gate, including the knobs that landed
    after the library (N-of-M approvals and per-path roles) - otherwise it
    under-reports what a config will enforce."""
    summary = effective_policy_summary(load_preset_settings("pci-dss"))
    assert summary["min_approvals"] == 2
    assert summary["repo_min_approvals"] == {"key-management-service": 3}
    assert summary["path_required_roles"]["**/cardholder/**"] == ["security"]
    assert summary["path_required_roles"]["**/crypto/**"] == ["security"]


def test_effective_policy_summary_defaults_when_knobs_unset() -> None:
    """A preset that sets none of the newer knobs reports the inert defaults,
    not a missing key (so `explain` never KeyErrors on an older config)."""
    summary = effective_policy_summary(load_preset_settings("baseline"))
    assert summary["min_approvals"] == 1
    assert summary["repo_min_approvals"] == {}
    assert summary["path_required_roles"] == {}


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


def test_cli_explain_surfaces_n_of_m_and_path_roles(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from foundry.policy.cli import main

    main(["explain", "pci-dss"])
    out = capsys.readouterr().out
    assert "min_approvals             : 2" in out
    assert "key-management-service: 3" in out
    assert "**/cardholder/**: security" in out


def test_cli_unknown_preset_exits_nonzero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from foundry.policy.cli import main

    with pytest.raises(SystemExit) as excinfo:
        main(["show", "nope"])
    assert excinfo.value.code == 2
    assert "nope" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# `check`: verify a config meets a baseline's strictness floor
# --------------------------------------------------------------------------- #
def _write_config(directory, text: str) -> str:
    """Write a config YAML into ``directory`` (created if needed); return its path."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "foundry.yaml"
    path.write_text(text, encoding="utf-8")
    return str(path)


def _settings(directory, text: str) -> Settings:
    return Settings.load(_write_config(directory, text), env={})


# A baseline exercising every comparable knob.
_BASELINE_YAML = """
policy:
  repo_confidence_threshold: 80
  max_files_changed: 10
  min_approvals: 2
  forbidden_globs:
    - "infra/**"
    - "**/secrets/**"
  repo_required_roles:
    payments-service: ["security"]
  repo_min_approvals:
    payments-service: 3
  path_required_roles:
    "**/billing/**": ["security"]
remediation:
  max_agent_retries: 1
budget:
  max_cost_per_run: 8.0
"""


def _finding(comparison, knob):
    return next(f for f in comparison.findings if f.knob == knob)


def test_check_identical_preset_passes() -> None:
    """A preset is always at least as strict as itself - the trivial floor."""
    for name in available_preset_names():
        settings = load_preset_settings(name)
        comparison = compare_policy_strictness(settings, settings)
        assert comparison.ok, f"{name} should satisfy its own floor"
        assert comparison.weaknesses == ()


def test_check_stricter_subject_passes(tmp_path) -> None:
    baseline = _settings(tmp_path / "b", _BASELINE_YAML)
    stricter = _settings(
        tmp_path / "s",
        """
policy:
  repo_confidence_threshold: 90
  max_files_changed: 5
  min_approvals: 3
  forbidden_globs:
    - "infra/**"
    - "**/secrets/**"
    - "**/keys/**"
  repo_required_roles:
    payments-service: ["security", "engineering"]
  repo_min_approvals:
    payments-service: 4
  path_required_roles:
    "**/billing/**": ["security"]
remediation:
  max_agent_retries: 0
budget:
  max_cost_per_run: 4.0
""",
    )
    comparison = compare_policy_strictness(stricter, baseline)
    assert comparison.ok
    assert comparison.weaknesses == ()


def test_check_weaker_scalars_fail(tmp_path) -> None:
    baseline = _settings(tmp_path / "b", _BASELINE_YAML)
    weaker = _settings(
        tmp_path / "s",
        """
policy:
  repo_confidence_threshold: 70
  max_files_changed: 20
  min_approvals: 1
  forbidden_globs:
    - "infra/**"
    - "**/secrets/**"
  repo_required_roles:
    payments-service: ["security"]
  repo_min_approvals:
    payments-service: 3
  path_required_roles:
    "**/billing/**": ["security"]
remediation:
  max_agent_retries: 5
budget:
  max_cost_per_run: 50.0
""",
    )
    comparison = compare_policy_strictness(weaker, baseline)
    assert comparison.ok is False
    weak_knobs = {f.knob for f in comparison.weaknesses}
    assert weak_knobs == {
        "repo_confidence_threshold",
        "max_files_changed",
        "min_approvals",
        "max_agent_retries",
        "max_cost_per_run",
    }


def test_check_forbidden_globs_must_be_superset(tmp_path) -> None:
    baseline = _settings(tmp_path / "b", _BASELINE_YAML)
    missing = _settings(
        tmp_path / "s",
        """
policy:
  forbidden_globs:
    - "infra/**"
""",
    )
    finding = _finding(compare_policy_strictness(missing, baseline), "forbidden_globs")
    assert finding.ok is False
    assert "**/secrets/**" in finding.detail


def test_check_per_repo_forbidden_globs_covered_by_global(tmp_path) -> None:
    """A baseline's per-repo glob is satisfied by the subject's *global* set.

    The orchestrator protects a repo with the global globs PLUS the per-repo
    extras, so the comparison must merge them - a config that protects a path
    globally needn't repeat it per-repo to satisfy a per-repo baseline rule.
    """
    baseline = _settings(
        tmp_path / "b",
        """
policy:
  repo_forbidden_globs:
    payments-service: ["**/cardholder/**"]
""",
    )
    subject = _settings(
        tmp_path / "s",
        """
policy:
  forbidden_globs:
    - "**/cardholder/**"
""",
    )
    finding = _finding(
        compare_policy_strictness(subject, baseline), "repo_forbidden_globs"
    )
    assert finding.ok is True


def test_check_repo_required_roles_must_be_superset(tmp_path) -> None:
    baseline = _settings(tmp_path / "b", _BASELINE_YAML)
    # Subject requires a different role on the same repo - missing 'security'.
    subject = _settings(
        tmp_path / "s",
        """
policy:
  repo_required_roles:
    payments-service: ["engineering"]
""",
    )
    finding = _finding(
        compare_policy_strictness(subject, baseline), "repo_required_roles"
    )
    assert finding.ok is False
    assert "payments-service" in finding.detail
    assert "security" in finding.detail


def test_check_repo_min_approvals_uses_effective_value(tmp_path) -> None:
    """A high global ``min_approvals`` can satisfy a baseline's per-repo rule."""
    baseline = _settings(
        tmp_path / "b",
        """
policy:
  min_approvals: 2
  repo_min_approvals:
    payments-service: 3
""",
    )
    # Subject sets no per-repo override but a global of 3 - effective 3 >= 3.
    covered = _settings(tmp_path / "s1", "policy:\n  min_approvals: 3\n")
    assert _finding(
        compare_policy_strictness(covered, baseline), "repo_min_approvals"
    ).ok
    # Global 2, no override -> effective 2 < baseline effective 3 -> weaker.
    short = _settings(tmp_path / "s2", "policy:\n  min_approvals: 2\n")
    short_finding = _finding(
        compare_policy_strictness(short, baseline), "repo_min_approvals"
    )
    assert short_finding.ok is False
    assert "payments-service" in short_finding.detail


def test_check_path_required_roles_must_be_superset(tmp_path) -> None:
    baseline = _settings(tmp_path / "b", _BASELINE_YAML)
    subject = _settings(tmp_path / "s", "policy:\n  repo_confidence_threshold: 80\n")
    finding = _finding(
        compare_policy_strictness(subject, baseline), "path_required_roles"
    )
    assert finding.ok is False
    assert "**/billing/**" in finding.detail


def test_check_cost_cap_none_is_weaker_than_a_cap(tmp_path) -> None:
    baseline = _settings(tmp_path / "b", "budget:\n  max_cost_per_run: 8.0\n")
    # No cap at all is weaker than any cap.
    uncapped = _settings(tmp_path / "s", "policy:\n  repo_confidence_threshold: 80\n")
    assert _finding(
        compare_policy_strictness(uncapped, baseline), "max_cost_per_run"
    ).ok is False


def test_check_baseline_without_cap_accepts_any(tmp_path) -> None:
    baseline = _settings(tmp_path / "b", "policy:\n  repo_confidence_threshold: 70\n")
    capped = _settings(tmp_path / "s", "budget:\n  max_cost_per_run: 100.0\n")
    assert _finding(
        compare_policy_strictness(capped, baseline), "max_cost_per_run"
    ).ok is True


# --------------------------------------------------------------------------- #
# resolve_settings: preset name OR file path
# --------------------------------------------------------------------------- #
def test_resolve_settings_accepts_preset_name() -> None:
    settings = resolve_settings("soc2")
    assert settings.repo_confidence_threshold == 80


def test_resolve_settings_accepts_file_path(tmp_path) -> None:
    path = _write_config(tmp_path, "policy:\n  repo_confidence_threshold: 95\n")
    settings = resolve_settings(path)
    assert settings.repo_confidence_threshold == 95


def test_resolve_settings_rejects_unknown_ref() -> None:
    with pytest.raises(ValueError) as excinfo:
        resolve_settings("not-a-preset-or-path")
    message = str(excinfo.value)
    assert "not-a-preset-or-path" in message
    # Lists the presets so a typo is self-correcting.
    for name in available_preset_names():
        assert name in message


# --------------------------------------------------------------------------- #
# `foundry-policy check` CLI
# --------------------------------------------------------------------------- #
def test_cli_check_passes_against_matching_preset(
    tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    from foundry.policy.cli import main

    # A config copied straight from the soc2 preset meets the soc2 floor.
    config = _write_config(tmp_path, load_preset_yaml("soc2"))
    main(["check", "--config", config, "--against", "soc2"])  # no SystemExit
    out = capsys.readouterr().out
    assert "RESULT: PASS" in out
    assert "PASS  repo_confidence_threshold" in out


def test_cli_check_fails_and_exits_one_when_weaker(
    tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    from foundry.policy.cli import main

    config = _write_config(
        tmp_path, "policy:\n  repo_confidence_threshold: 50\n"
    )
    with pytest.raises(SystemExit) as excinfo:
        main(["check", "--config", config, "--against", "soc2"])
    assert excinfo.value.code == 1
    out = capsys.readouterr().out
    assert "FAIL  repo_confidence_threshold" in out
    assert "RESULT: FAIL" in out


def test_cli_check_against_a_file_path(
    tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    from foundry.policy.cli import main

    baseline = _write_config(
        tmp_path, "policy:\n  repo_confidence_threshold: 95\n"
    )
    # A second file for the subject (weaker than the baseline file).
    subject = tmp_path / "subject.yaml"
    subject.write_text("policy:\n  repo_confidence_threshold: 80\n", encoding="utf-8")
    with pytest.raises(SystemExit) as excinfo:
        main(["check", "--config", str(subject), "--against", baseline])
    assert excinfo.value.code == 1


def test_cli_check_errors_when_no_config_source(
    monkeypatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from foundry.policy.cli import main

    monkeypatch.delenv("FOUNDRY_CONFIG", raising=False)
    with pytest.raises(SystemExit) as excinfo:
        main(["check", "--against", "soc2"])
    assert excinfo.value.code == 2
    assert "no config" in capsys.readouterr().err.lower()


def test_cli_check_errors_on_unknown_baseline(
    tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    from foundry.policy.cli import main

    config = _write_config(tmp_path, "policy:\n  repo_confidence_threshold: 90\n")
    with pytest.raises(SystemExit) as excinfo:
        main(["check", "--config", config, "--against", "nope-not-real"])
    assert excinfo.value.code == 2
    assert "nope-not-real" in capsys.readouterr().err


def test_cli_check_uses_foundry_config_env(
    tmp_path, monkeypatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from foundry.policy.cli import main

    config = _write_config(tmp_path, load_preset_yaml("baseline"))
    monkeypatch.setenv("FOUNDRY_CONFIG", config)
    # baseline preset vs baseline floor -> passes, no --config needed.
    main(["check", "--against", "baseline"])
    assert "RESULT: PASS" in capsys.readouterr().out
