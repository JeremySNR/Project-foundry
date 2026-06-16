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
    PolicyComparison,
    compare_policy_strictness,
    comparison_to_dict,
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


def test_cli_explain_surfaces_change_freeze_windows(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The change-management preset configures a weekend release blackout; the
    # text `explain` must render it (it previously only appeared in --format json).
    from foundry.policy.cli import main

    main(["explain", "change-management"])
    out = capsys.readouterr().out
    assert "change-freeze windows" in out
    assert "sat/sun 00:00-23:59 UTC" in out
    assert "Weekend release blackout" in out


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


def test_comparison_to_dict_shape() -> None:
    # The shared serialiser the CLI `check --format json` and the in-app
    # GET /metrics/policy/check both use, so the two verdicts can't drift.
    weaker = Settings.from_env({})  # built-in defaults
    baseline = load_preset_settings("soc2")
    comparison = compare_policy_strictness(weaker, baseline)
    payload = comparison_to_dict(comparison)
    assert set(payload) == {"ok", "findings", "weaknesses"}
    assert payload["ok"] == comparison.ok
    assert payload["ok"] is False  # defaults are weaker than soc2
    assert all(
        {
            "knob",
            "ok",
            "detail",
            "comparator",
            "subject",
            "baseline",
            "missing",
            "missing_items",
        }
        == finding.keys()
        for finding in payload["findings"]
    )
    # weaknesses is exactly the not-ok knobs, order-preserved.
    assert payload["weaknesses"] == [
        f["knob"] for f in payload["findings"] if not f["ok"]
    ]
    assert payload["weaknesses"]  # at least one control falls short


def test_comparison_to_dict_typed_scalar_fields() -> None:
    # Scalar knobs carry the numeric subject/baseline values and the gate's
    # comparison direction, so a CI step can compare them without parsing prose.
    weaker = Settings.from_env({})  # built-in defaults
    baseline = load_preset_settings("soc2")
    by_knob = {
        f["knob"]: f for f in comparison_to_dict(
            compare_policy_strictness(weaker, baseline)
        )["findings"]
    }

    # higher-is-stricter scalar
    thr = by_knob["repo_confidence_threshold"]
    assert thr["comparator"] == ">="
    assert thr["subject"] == weaker.repo_confidence_threshold
    assert thr["baseline"] == baseline.repo_confidence_threshold
    assert thr["missing"] == []  # scalars never populate the collection gap list
    assert thr["missing_items"] == []  # ...nor the structured one
    # the typed verdict agrees with the boolean verdict
    assert thr["ok"] == (thr["subject"] >= thr["baseline"])

    # lower-is-stricter scalar
    files = by_knob["max_files_changed"]
    assert files["comparator"] == "<="
    assert files["subject"] == weaker.max_files_changed
    assert files["baseline"] == baseline.max_files_changed
    assert files["ok"] == (files["subject"] <= files["baseline"])


def test_comparison_to_dict_typed_cost_nullable() -> None:
    # max_cost_per_run is nullable: None means "no cap" on either side, and the
    # typed fields surface that as a JSON null rather than a prose word.
    no_cap = Settings.from_env({})
    capped = load_preset_settings("soc2")
    finding = next(
        f
        for f in comparison_to_dict(compare_policy_strictness(no_cap, capped))[
            "findings"
        ]
        if f["knob"] == "max_cost_per_run"
    )
    assert finding["comparator"] == "<="
    assert finding["subject"] == no_cap.max_cost_per_run
    assert finding["baseline"] == capped.max_cost_per_run


def test_comparison_to_dict_typed_collection_missing() -> None:
    # Collection knobs carry "superset" plus the exact baseline items the
    # subject fails to cover, so a consumer reads the gap as a list, not prose.
    weaker = Settings.from_env({})  # no forbidden globs configured
    baseline = load_preset_settings("soc2")
    finding = next(
        f
        for f in comparison_to_dict(compare_policy_strictness(weaker, baseline))[
            "findings"
        ]
        if f["knob"] == "forbidden_globs"
    )
    assert finding["comparator"] == "superset"
    assert finding["subject"] is None  # numeric fields don't apply to collections
    assert finding["baseline"] is None
    assert not finding["ok"]
    # the missing list names exactly the baseline globs the subject doesn't cover
    # (the default config ships a built-in forbidden-glob floor, so this is the
    # baseline set minus that floor, not the whole baseline).
    expected_missing = set(baseline.forbidden_globs) - set(weaker.forbidden_globs)
    assert set(finding["missing"]) == expected_missing
    assert finding["missing"]  # non-empty: soc2 protects more than the floor
    # the structured counterpart: one {"item": <glob>} per missing flat-list item,
    # naming exactly the same globs the prose `missing` list does.
    assert {entry["item"] for entry in finding["missing_items"]} == expected_missing
    assert all(set(entry) == {"item"} for entry in finding["missing_items"])


def test_comparison_to_dict_pass_has_empty_missing() -> None:
    # When a config meets its own floor, every collection knob's gap list is
    # empty (the typed counterpart to `ok=True`).
    settings = load_preset_settings("soc2")
    payload = comparison_to_dict(compare_policy_strictness(settings, settings))
    assert payload["ok"] is True
    assert all(f["missing"] == [] for f in payload["findings"])
    assert all(f["missing_items"] == [] for f in payload["findings"])


def test_missing_items_structured_map_to_list_knobs(tmp_path) -> None:
    # The map-valued role/glob knobs expose each shortfall as a structured
    # {"key": <repo|glob>, "items": [...]} entry, so a CI step never has to parse
    # the "<key>: <items>" prose the `missing` strings (and `detail`) are built
    # from. The structured entries name exactly the same key+items.
    baseline = _settings(
        tmp_path / "b",
        "policy:\n"
        "  repo_required_roles:\n"
        "    payments-svc: [security, engineering]\n"
        "  path_required_roles:\n"
        "    'infra/**': [engineering]\n",
    )
    # Subject covers neither requirement.
    subject = _settings(tmp_path / "s", "policy:\n  repo_confidence_threshold: 80\n")
    by_knob = {
        f["knob"]: f
        for f in comparison_to_dict(compare_policy_strictness(subject, baseline))[
            "findings"
        ]
    }

    repo_roles = by_knob["repo_required_roles"]
    assert not repo_roles["ok"]
    assert repo_roles["missing_items"] == [
        {"key": "payments-svc", "items": ["security", "engineering"]}
    ]
    # the prose `missing` is derived from the same gap, so they agree
    assert repo_roles["missing"] == ["payments-svc: ['security', 'engineering']"]

    path_roles = by_knob["path_required_roles"]
    assert not path_roles["ok"]
    assert path_roles["missing_items"] == [
        {"key": "infra/**", "items": ["engineering"]}
    ]


def test_missing_items_structured_repo_min_approvals(tmp_path) -> None:
    # repo_min_approvals is numeric, not a list, so its structured entry carries
    # the effective subject/baseline counts per shortfall repo rather than items.
    baseline = _settings(
        tmp_path / "b",
        "policy:\n  min_approvals: 1\n  repo_min_approvals:\n    infra: 3\n",
    )
    subject = _settings(tmp_path / "s", "policy:\n  min_approvals: 1\n")
    finding = _finding(
        compare_policy_strictness(subject, baseline), "repo_min_approvals"
    )
    payload = comparison_to_dict(PolicyComparison(findings=(finding,)))["findings"][0]
    assert not payload["ok"]
    # effective subject = max(global 1, no override) = 1; baseline = max(1, 3) = 3
    assert payload["missing_items"] == [
        {"key": "infra", "subject": 1, "baseline": 3}
    ]
    assert payload["missing"] == ["infra: 1 < 3"]  # same gap, prose form


def test_comparison_to_dict_all_pass() -> None:
    settings = load_preset_settings("soc2")
    payload = comparison_to_dict(compare_policy_strictness(settings, settings))
    assert payload["ok"] is True
    assert payload["weaknesses"] == []


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


# --------------------------------------------------------------------------- #
# `foundry-policy check --format json` (machine-readable output for CI)
# --------------------------------------------------------------------------- #
def test_cli_check_json_passes_with_structured_output(
    tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    from foundry.policy.cli import main

    config = _write_config(tmp_path, load_preset_yaml("soc2"))
    main(["check", "--config", config, "--against", "soc2", "--format", "json"])

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["config"] == config
    assert payload["baseline"] == "soc2"
    assert payload["weaknesses"] == []
    # One finding per compared knob, each carrying its verdict + detail.
    knobs = {finding["knob"] for finding in payload["findings"]}
    assert "repo_confidence_threshold" in knobs
    assert all(finding["ok"] for finding in payload["findings"])
    assert all(
        {"knob", "ok", "detail"} <= finding.keys() for finding in payload["findings"]
    )


def test_cli_check_json_fails_exits_one_and_names_weaknesses(
    tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    from foundry.policy.cli import main

    config = _write_config(tmp_path, "policy:\n  repo_confidence_threshold: 50\n")
    with pytest.raises(SystemExit) as excinfo:
        main(["check", "--config", config, "--against", "soc2", "--format", "json"])
    assert excinfo.value.code == 1

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert "repo_confidence_threshold" in payload["weaknesses"]
    # The weak knob's finding is marked not-ok in the findings list too.
    weak = next(
        f for f in payload["findings"] if f["knob"] == "repo_confidence_threshold"
    )
    assert weak["ok"] is False


def test_cli_check_json_carries_typed_finding_values(
    tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The CLI json output exposes the typed comparator/subject/baseline/missing
    # per finding, so a CI step can compare values directly instead of scraping
    # the prose `detail`.
    import json

    from foundry.policy.cli import main

    config = _write_config(tmp_path, "policy:\n  repo_confidence_threshold: 50\n")
    with pytest.raises(SystemExit):
        main(["check", "--config", config, "--against", "soc2", "--format", "json"])

    payload = json.loads(capsys.readouterr().out)
    by_knob = {f["knob"]: f for f in payload["findings"]}

    # the weak scalar knob carries its numeric values and the gate's direction
    thr = by_knob["repo_confidence_threshold"]
    assert thr["comparator"] == ">="
    assert thr["subject"] == 50
    assert thr["baseline"] > 50  # soc2 demands more, hence the FAIL
    assert thr["ok"] is False

    # a collection knob carries "superset" and a (possibly empty) missing list
    globs = by_knob["forbidden_globs"]
    assert globs["comparator"] == "superset"
    assert isinstance(globs["missing"], list)


def test_cli_check_json_emits_structured_error_on_unknown_baseline(
    tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    from foundry.policy.cli import main

    config = _write_config(tmp_path, "policy:\n  repo_confidence_threshold: 90\n")
    with pytest.raises(SystemExit) as excinfo:
        main(["check", "--config", config, "--against", "nope", "--format", "json"])
    # Usage / config errors still exit 2 (distinct from a "weaker" verdict),
    # but in json mode the error is a structured object on stderr.
    assert excinfo.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    error = json.loads(captured.err)
    assert "nope" in error["error"]


def test_cli_check_defaults_to_text_format(
    tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    from foundry.policy.cli import main

    # No --format flag -> the human report, byte-for-byte as before.
    config = _write_config(tmp_path, load_preset_yaml("soc2"))
    main(["check", "--config", config, "--against", "soc2"])
    out = capsys.readouterr().out
    assert "RESULT: PASS" in out
    assert "PASS  repo_confidence_threshold" in out


# --------------------------------------------------------------------------- #
# `explain`: introspect a preset OR the operator's own config
# --------------------------------------------------------------------------- #
# A small config that sets gate knobs distinctly from any preset, so an
# assertion can prove `explain` read *this* config and not a default/preset.
_OWN_CONFIG_YAML = """
policy:
  repo_confidence_threshold: 73
  max_files_changed: 9
  min_approvals: 2
  forbidden_globs:
    - "infra/**"
    - "**/my-secrets/**"
  repo_required_roles:
    billing-service: ["security"]
  path_required_roles:
    "**/ledger/**": ["security"]
remediation:
  max_agent_retries: 1
budget:
  max_cost_per_run: 7.5
"""


def test_cli_explain_introspects_own_config_via_flag(
    tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    from foundry.policy.cli import main

    config = _write_config(tmp_path, _OWN_CONFIG_YAML)
    main(["explain", "--config", config])
    out = capsys.readouterr().out
    # Labelled as a config (not a preset) and reflecting THIS file's knobs.
    assert f"config '{config}'" in out
    assert "repo_confidence_threshold : 73" in out
    assert "min_approvals             : 2" in out
    assert "billing-service: security" in out
    assert "**/ledger/**: security" in out


def test_cli_explain_accepts_a_config_path_positionally(
    tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    from foundry.policy.cli import main

    config = _write_config(tmp_path, _OWN_CONFIG_YAML)
    main(["explain", config])
    out = capsys.readouterr().out
    assert f"config '{config}'" in out
    assert "max_files_changed         : 9" in out


def test_cli_explain_uses_foundry_config_env(
    tmp_path, monkeypatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from foundry.policy.cli import main

    config = _write_config(tmp_path, _OWN_CONFIG_YAML)
    monkeypatch.setenv("FOUNDRY_CONFIG", config)
    main(["explain"])
    out = capsys.readouterr().out
    assert f"config '{config}'" in out
    assert "repo_confidence_threshold : 73" in out


def test_cli_explain_preset_still_labelled_as_preset(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Backward compatibility: a bare preset name is unchanged - still loaded
    # pure and headed "preset '<name>'".
    from foundry.policy.cli import main

    main(["explain", "soc2"])
    out = capsys.readouterr().out
    assert "Effective policy for preset 'soc2':" in out


def test_cli_explain_json_emits_effective_knobs(
    tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    from foundry.policy.cli import main

    config = _write_config(tmp_path, _OWN_CONFIG_YAML)
    main(["explain", "--config", config, "--format", "json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["source"] == config
    assert payload["kind"] == "config"
    policy = payload["policy"]
    assert policy["repo_confidence_threshold"] == 73
    assert policy["min_approvals"] == 2
    assert policy["repo_required_roles"] == {"billing-service": ["security"]}
    assert policy["path_required_roles"] == {"**/ledger/**": ["security"]}
    assert policy["max_cost_per_run"] == 7.5


def test_cli_explain_json_for_a_preset_is_marked_preset(
    capsys: pytest.CaptureFixture[str],
) -> None:
    import json

    from foundry.policy.cli import main

    main(["explain", "pci-dss", "--format", "json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["source"] == "pci-dss"
    assert payload["kind"] == "preset"
    assert payload["policy"]["min_approvals"] == 2


def test_cli_explain_errors_when_no_source(
    monkeypatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from foundry.policy.cli import main

    monkeypatch.delenv("FOUNDRY_CONFIG", raising=False)
    with pytest.raises(SystemExit) as excinfo:
        main(["explain"])
    assert excinfo.value.code == 2
    assert "nothing to explain" in capsys.readouterr().err.lower()


def test_cli_explain_errors_on_unknown_target(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from foundry.policy.cli import main

    with pytest.raises(SystemExit) as excinfo:
        main(["explain", "not-a-preset-or-path"])
    assert excinfo.value.code == 2
    assert "not-a-preset-or-path" in capsys.readouterr().err


def test_cli_explain_errors_when_config_missing(
    tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    from foundry.policy.cli import main

    missing = str(tmp_path / "nope.yaml")
    with pytest.raises(SystemExit) as excinfo:
        main(["explain", "--config", missing])
    assert excinfo.value.code == 2
    assert "not found" in capsys.readouterr().err.lower()


def test_cli_explain_json_error_on_missing_config_goes_to_stderr(
    tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    from foundry.policy.cli import main

    missing = str(tmp_path / "nope.yaml")
    with pytest.raises(SystemExit) as excinfo:
        main(["explain", "--config", missing, "--format", "json"])
    assert excinfo.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""  # nothing on stdout for a json consumer to parse
    assert json.loads(captured.err)["error"]


def test_cli_explain_rejects_both_positional_and_config(
    tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    from foundry.policy.cli import main

    config = _write_config(tmp_path, _OWN_CONFIG_YAML)
    with pytest.raises(SystemExit) as excinfo:
        main(["explain", "soc2", "--config", config])
    assert excinfo.value.code == 2
    assert "not both" in capsys.readouterr().err.lower()
