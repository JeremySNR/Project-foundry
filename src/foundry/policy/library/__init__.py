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
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from foundry.policy.freeze import describe_window, window_key

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
    (
        "pci-dss",
        "pci_dss.yaml",
        "PCI-DSS cardholder data environment: high routing confidence, a "
        "two-person rule (separation of duties) raised to three for key "
        "management, protected crypto/key paths, and per-repo plus per-path "
        "security sign-off on cardholder-data and cryptographic surfaces.",
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
    overrides, the risk-escalation surface (which diff paths / ticket-text
    keywords flag a sensitive area, and so demand its approval roles) and the
    retry/budget caps - so an operator can see a preset's effect without
    standing up a run.
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
        # Risk-escalation surface (issue #31): the diff-path globs and ticket-text
        # keywords that flag a sensitive area (auth/payments/...), which in turn
        # demand that area's approval roles. Both are escalate-only floors - more
        # entries can only flag *more* areas, never fewer - so surfacing them lets
        # an operator see *what escalates a run to human review* alongside the gate
        # the escalation feeds. `sensitive_path_globs` carries the built-in default
        # map; `extra_sensitive_keywords` is purely additive (default empty).
        "sensitive_path_globs": {
            area: list(globs) for area, globs in settings.sensitive_path_globs
        },
        "extra_sensitive_keywords": {
            area: list(keywords)
            for area, keywords in settings.extra_sensitive_keywords
        },
        # N-of-M approval matrix (issue #31): the minimum DISTINCT human sign-offs
        # a run needs, globally and per-repo (effective = max(global, per-repo)).
        "min_approvals": settings.min_approvals,
        "repo_min_approvals": {
            repo: count for repo, count in settings.repo_min_approvals
        },
        # Per-path required approval roles (issue #31): path glob -> roles that
        # must sign off when a PR's diff touches the subtree.
        "path_required_roles": {
            glob: list(roles) for glob, roles in settings.path_required_roles
        },
        # Change-freeze / maintenance windows (issue #31, "time windows"): the
        # times when an autonomous re-dispatch is held for a human. Rendered as
        # human-readable one-liners so explain / the dashboard can print them.
        "change_freeze_windows": [
            describe_window(window) for window in settings.change_freeze_windows
        ],
        "max_agent_retries": settings.max_agent_retries,
        "retry_on": list(settings.retry_on),
        "max_cost_per_run": settings.max_cost_per_run,
        "estimated_cost_per_dispatch": settings.estimated_cost_per_dispatch,
        "approver_count": len(settings.approvers),
    }


def resolve_settings(ref: str, *, env: Mapping[str, str] | None = None) -> "Settings":
    """Load a :class:`Settings` from either a preset name or a YAML file path.

    The ``foundry-policy check --against`` argument accepts both: a vetted
    baseline by name (``soc2``) or a path to another config file (e.g. a
    second deployment's ``foundry.yaml``). A name is tried first; anything else
    is treated as a path.

    A non-existent path raises rather than silently loading defaults -
    ``Settings.load`` ignores a missing path (so a typo'd baseline would compare
    against the built-in defaults and quietly "pass"), which is exactly the
    misleading outcome a compliance check must not produce. ``env`` defaults to
    an empty mapping so a baseline reflects the file/preset itself, not ambient
    ``FOUNDRY_*`` overrides in the calling process.
    """
    if ref in available_preset_names():
        return load_preset_settings(ref, env=env)
    from foundry.config import Settings

    path = Path(ref)
    if not path.exists():
        available = ", ".join(available_preset_names())
        raise ValueError(
            f"baseline {ref!r} is neither a known preset nor an existing file; "
            f"available presets: {available}"
        )
    return Settings.load(path, env=env or {})


@dataclass(frozen=True)
class PolicyCheckFinding:
    """One control's verdict when checking a config against a baseline.

    ``ok`` is True when the subject config is *at least as strict* as the
    baseline for this knob; ``detail`` is a human-readable one-liner naming the
    values (and, for collection knobs, exactly which repos/paths/globs fall
    short).

    Alongside the prose ``detail`` each finding also carries a **typed,
    machine-readable** description of the comparison so a CI step / dashboard
    consumer can reason about it without scraping the sentence:

    - ``comparator`` - the direction the gate enforces for this knob: ``">="``
      (higher is stricter), ``"<="`` (lower is stricter), ``"superset"`` (the
      subject must cover everything the baseline lists), or ``"subset"`` (the
      mirror: the subject must list *no more* than the baseline - used for
      ``retry_on``, where a *smaller* set of autonomous-retry triggers is
      stricter).
    - ``subject`` / ``baseline`` - for the **scalar** knobs, the numeric value
      on each side (``None`` for an absent cap, e.g. ``max_cost_per_run``); left
      ``None`` for collection knobs, where a single number does not apply.
    - ``missing`` - for the **collection** knobs, the baseline items the subject
      fails to cover (empty when the control passes); always empty for scalars.
      For a ``subset`` knob the failure direction is reversed, so ``missing``
      instead names the subject's **disallowed extra** items (the triggers the
      baseline does not permit) - the items responsible for the failure either
      way.
    - ``missing_items`` - the same shortfall as ``missing`` but **structured**,
      so a consumer never has to parse the ``"<key>: <items>"`` prose. One dict
      per shortfall, shaped by the knob's kind:

      - **map -> list knobs** (``repo_forbidden_globs`` / ``repo_required_roles``
        / ``path_required_roles`` / ``sensitive_path_globs`` /
        ``extra_sensitive_keywords``): ``{"key": <repo|glob|area>, "items":
        [<missing glob/role/keyword>, ...]}`` per shortfall key.
      - **``repo_min_approvals``** (numeric): ``{"key": <repo>, "subject": <int>,
        "baseline": <int>}`` per shortfall repo.
      - **flat list knobs** (``forbidden_globs`` / ``change_freeze_windows`` and
        the ``subset`` knob ``retry_on``): ``{"item": <glob|window|trigger>}``
        per shortfall item (for ``retry_on``, each disallowed extra trigger).
      - **scalar knobs**: always empty (the ``subject``/``baseline`` numbers
        already carry the comparison).

      Derived from the *same* gap data as ``detail`` and ``missing``, so the
      three can't drift.
    """

    knob: str
    ok: bool
    detail: str
    comparator: str | None = None
    subject: Any = None
    baseline: Any = None
    missing: tuple[str, ...] = ()
    missing_items: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class PolicyComparison:
    """The result of :func:`compare_policy_strictness` - one finding per knob."""

    findings: tuple[PolicyCheckFinding, ...]

    @property
    def ok(self) -> bool:
        """True only when the subject is at least as strict on *every* knob."""
        return all(finding.ok for finding in self.findings)

    @property
    def weaknesses(self) -> tuple[PolicyCheckFinding, ...]:
        """The controls where the subject config is weaker than the baseline."""
        return tuple(finding for finding in self.findings if not finding.ok)


def _missing(required: "tuple[str, ...]", have: set[str]) -> list[str]:
    """Baseline-required items not present in ``have`` (order-preserving)."""
    return [item for item in required if item not in have]


def compare_policy_strictness(
    subject: "Settings", baseline: "Settings"
) -> PolicyComparison:
    """Check whether ``subject`` is at least as strict as ``baseline`` per knob.

    This is the verification counterpart to :func:`effective_policy_summary`:
    ``explain`` shows what a config resolves to; this answers *"does my config
    meet (or exceed) this control baseline?"*. It is **pure and read-only** - it
    compares two already-loaded :class:`Settings` and changes nothing - so it
    touches no gate, ``engine.py`` or ``foundry.rego`` (invariant #2 does not
    apply).

    Strictness is defined per knob, always in the direction the gate enforces:

    - **higher is stricter** - ``repo_confidence_threshold``, ``min_approvals``
      (and the per-repo effective ``max(global, per-repo)`` minimum);
    - **lower is stricter** - ``max_files_changed``, ``max_agent_retries``,
      ``max_cost_per_run`` (``None`` = no cap = *weakest*);
    - **superset is stricter** - ``forbidden_globs``, the per-repo / per-path
      required-role and forbidden-glob maps, the change-freeze windows, and the
      risk-escalation maps (``sensitive_path_globs`` / ``extra_sensitive_keywords``,
      area -> diff-path globs / ticket-text keywords): the subject must protect /
      require / escalate on everything the baseline does (it may add more).
    - **subset is stricter** - ``retry_on`` (the mirror of the superset knobs):
      the failure reasons that trigger an *autonomous* agent re-dispatch; a
      *smaller* set hands more failures to a human, so the subject may auto-retry
      on no more triggers than the baseline (it may auto-retry on fewer).

    The comparison only ever looks at *configured* knobs. The risk-escalation
    maps are configured (they decide *which* runs escalate to which roles, an
    escalate-only floor), so they are compared; the risk-*derived* approval roles
    a flagged area then demands apply identically to both sides, so those are not
    themselves part of the diff.
    """
    findings: list[PolicyCheckFinding] = []

    # --- scalar knobs ---------------------------------------------------- #
    s_thr, b_thr = subject.repo_confidence_threshold, baseline.repo_confidence_threshold
    findings.append(
        PolicyCheckFinding(
            "repo_confidence_threshold",
            s_thr >= b_thr,
            f"{s_thr} (baseline requires >= {b_thr})",
            comparator=">=",
            subject=s_thr,
            baseline=b_thr,
        )
    )

    s_files, b_files = subject.max_files_changed, baseline.max_files_changed
    findings.append(
        PolicyCheckFinding(
            "max_files_changed",
            s_files <= b_files,
            f"{s_files} (baseline requires <= {b_files})",
            comparator="<=",
            subject=s_files,
            baseline=b_files,
        )
    )

    s_min, b_min = subject.min_approvals, baseline.min_approvals
    findings.append(
        PolicyCheckFinding(
            "min_approvals",
            s_min >= b_min,
            f"{s_min} (baseline requires >= {b_min})",
            comparator=">=",
            subject=s_min,
            baseline=b_min,
        )
    )

    s_ret, b_ret = subject.max_agent_retries, baseline.max_agent_retries
    findings.append(
        PolicyCheckFinding(
            "max_agent_retries",
            s_ret <= b_ret,
            f"{s_ret} (baseline requires <= {b_ret})",
            comparator="<=",
            subject=s_ret,
            baseline=b_ret,
        )
    )

    # --- retry_on (subset stricter: fewer autonomous-retry triggers) ----- #
    # `retry_on` is the set of PR-failure reasons (`ci_failed` /
    # `changes_requested`) that trigger an *autonomous* agent re-dispatch; a
    # reason outside the set parks the run for a human instead
    # (`orchestrator._attempt_remediation`). So a SMALLER set is stricter (more
    # failures handed to a human, fewer autonomous re-dispatches), and the subject
    # is at least as strict only when its triggers are a SUBSET of the baseline's.
    # A subject that adds a trigger the baseline omits (e.g. auto-retrying
    # `changes_requested` when the baseline only auto-retries `ci_failed`) is
    # *weaker* - exactly the autonomous-action drift a compliance check should
    # catch, yet which the superset knobs above can't express. Compared `subset`
    # (the mirror of the `superset` knobs): `missing`/`missing_items` carry the
    # subject's disallowed *extra* triggers (the items causing the failure),
    # since the failure direction is reversed.
    b_retry = set(baseline.retry_on)
    retry_extra = [trigger for trigger in subject.retry_on if trigger not in b_retry]
    findings.append(
        PolicyCheckFinding(
            "retry_on",
            not retry_extra,
            f"auto-retries on {retry_extra} not permitted by baseline "
            f"{sorted(b_retry)}"
            if retry_extra
            else f"auto-retries only within baseline's {sorted(b_retry)}",
            comparator="subset",
            missing=tuple(retry_extra),
            missing_items=tuple({"item": trigger} for trigger in retry_extra),
        )
    )

    s_cap, b_cap = subject.max_cost_per_run, baseline.max_cost_per_run
    s_cap_str = f"${s_cap}" if s_cap is not None else "none"
    if b_cap is None:
        # Baseline imposes no cap, so any subject cap (or none) satisfies it.
        cap_ok, cap_detail = True, f"{s_cap_str} (baseline sets no cap)"
    else:
        # None on the subject means *no* cap, which is weaker than any cap.
        cap_ok = s_cap is not None and s_cap <= b_cap
        cap_detail = f"{s_cap_str} (baseline requires <= ${b_cap})"
    findings.append(
        PolicyCheckFinding(
            "max_cost_per_run",
            cap_ok,
            cap_detail,
            comparator="<=",
            subject=s_cap,
            baseline=b_cap,
        )
    )

    # --- forbidden globs (superset) ------------------------------------- #
    subject_globs = set(subject.forbidden_globs)
    missing_globs = _missing(tuple(baseline.forbidden_globs), subject_globs)
    findings.append(
        PolicyCheckFinding(
            "forbidden_globs",
            not missing_globs,
            f"missing {missing_globs}"
            if missing_globs
            else f"covers all {len(set(baseline.forbidden_globs))} baseline path(s)",
            comparator="superset",
            missing=tuple(missing_globs),
            missing_items=tuple({"item": glob} for glob in missing_globs),
        )
    )

    # --- per-repo forbidden globs (superset, accounting for the global set) #
    s_repo_forbidden = subject.repo_forbidden_map
    repo_glob_gaps: list[tuple[str, list[str]]] = []
    for repo, globs in baseline.repo_forbidden_map.items():
        # The orchestrator protects a repo with the global globs PLUS its
        # per-repo extras, so compare against that merged effective set.
        effective = subject_globs | set(s_repo_forbidden.get(repo, ()))
        gap = _missing(globs, effective)
        if gap:
            repo_glob_gaps.append((repo, gap))
    findings.append(
        PolicyCheckFinding(
            "repo_forbidden_globs",
            not repo_glob_gaps,
            "; ".join(f"{repo}: {gap}" for repo, gap in repo_glob_gaps)
            if repo_glob_gaps
            else _none_or_covered(baseline.repo_forbidden_map),
            comparator="superset",
            missing=tuple(f"{repo}: {gap}" for repo, gap in repo_glob_gaps),
            missing_items=tuple(
                {"key": repo, "items": list(gap)} for repo, gap in repo_glob_gaps
            ),
        )
    )

    # --- per-repo required roles (superset) ----------------------------- #
    s_repo_roles = subject.repo_required_roles_map
    repo_role_gaps: list[tuple[str, list[str]]] = []
    for repo, roles in baseline.repo_required_roles_map.items():
        gap = _missing(roles, set(s_repo_roles.get(repo, ())))
        if gap:
            repo_role_gaps.append((repo, gap))
    findings.append(
        PolicyCheckFinding(
            "repo_required_roles",
            not repo_role_gaps,
            "; ".join(f"{repo}: {gap}" for repo, gap in repo_role_gaps)
            if repo_role_gaps
            else _none_or_covered(baseline.repo_required_roles_map),
            comparator="superset",
            missing=tuple(f"{repo}: {gap}" for repo, gap in repo_role_gaps),
            missing_items=tuple(
                {"key": repo, "items": list(gap)} for repo, gap in repo_role_gaps
            ),
        )
    )

    # --- per-repo minimum approvers (effective max(global, per-repo)) ---- #
    s_repo_min = subject.repo_min_approvals_map
    b_repo_min = baseline.repo_min_approvals_map
    repo_min_gaps: list[tuple[str, int, int]] = []
    for repo in b_repo_min:
        s_eff = max(s_min, s_repo_min.get(repo, 0))
        b_eff = max(b_min, b_repo_min[repo])
        if s_eff < b_eff:
            repo_min_gaps.append((repo, s_eff, b_eff))
    findings.append(
        PolicyCheckFinding(
            "repo_min_approvals",
            not repo_min_gaps,
            "; ".join(f"{repo}: {s} < {b}" for repo, s, b in repo_min_gaps)
            if repo_min_gaps
            else _none_or_covered(b_repo_min),
            comparator=">=",
            missing=tuple(f"{repo}: {s} < {b}" for repo, s, b in repo_min_gaps),
            missing_items=tuple(
                {"key": repo, "subject": s, "baseline": b}
                for repo, s, b in repo_min_gaps
            ),
        )
    )

    # --- per-path required roles (superset, keyed on the exact glob) ----- #
    s_path_roles = subject.path_required_roles_map
    path_role_gaps: list[tuple[str, list[str]]] = []
    for glob, roles in baseline.path_required_roles_map.items():
        gap = _missing(roles, set(s_path_roles.get(glob, ())))
        if gap:
            path_role_gaps.append((glob, gap))
    findings.append(
        PolicyCheckFinding(
            "path_required_roles",
            not path_role_gaps,
            "; ".join(f"{glob}: {gap}" for glob, gap in path_role_gaps)
            if path_role_gaps
            else _none_or_covered(baseline.path_required_roles_map),
            comparator="superset",
            missing=tuple(f"{glob}: {gap}" for glob, gap in path_role_gaps),
            missing_items=tuple(
                {"key": glob, "items": list(gap)} for glob, gap in path_role_gaps
            ),
        )
    )

    # --- sensitive-path globs (superset, area -> diff-path globs) -------- #
    # A diff touching these paths escalates the run to the area's required
    # approval roles, so dropping a baseline glob makes the gate *weaker* (a PR
    # that should have demanded e.g. security sign-off no longer does). The
    # subject must therefore cover at least every area->glob the baseline maps;
    # it may add more (a one-way ratchet towards stricter). Compared against the
    # configured default map both sides inherit when neither overrides it.
    s_sensitive_globs = subject.sensitive_globs_map
    sensitive_glob_gaps: list[tuple[str, list[str]]] = []
    for area, globs in baseline.sensitive_globs_map.items():
        gap = _missing(globs, set(s_sensitive_globs.get(area, ())))
        if gap:
            sensitive_glob_gaps.append((area, gap))
    findings.append(
        PolicyCheckFinding(
            "sensitive_path_globs",
            not sensitive_glob_gaps,
            "; ".join(f"{area}: {gap}" for area, gap in sensitive_glob_gaps)
            if sensitive_glob_gaps
            else _none_or_covered(baseline.sensitive_globs_map),
            comparator="superset",
            missing=tuple(f"{area}: {gap}" for area, gap in sensitive_glob_gaps),
            missing_items=tuple(
                {"key": area, "items": list(gap)}
                for area, gap in sensitive_glob_gaps
            ),
        )
    )

    # --- extra sensitive keywords (superset, area -> ticket-text words) -- #
    # The ticket-text twin of `sensitive_path_globs`: extra keywords that flag a
    # sensitive area from a ticket's title/description. Strictly additive on top
    # of the built-in keyword floor, so more entries only ever escalate more
    # runs - the subject must cover at least every area->keyword the baseline
    # adds. Default empty (both sides) => "baseline requires none".
    s_sensitive_kw = subject.extra_sensitive_keywords_map
    sensitive_kw_gaps: list[tuple[str, list[str]]] = []
    for area, keywords in baseline.extra_sensitive_keywords_map.items():
        gap = _missing(keywords, set(s_sensitive_kw.get(area, ())))
        if gap:
            sensitive_kw_gaps.append((area, gap))
    findings.append(
        PolicyCheckFinding(
            "extra_sensitive_keywords",
            not sensitive_kw_gaps,
            "; ".join(f"{area}: {gap}" for area, gap in sensitive_kw_gaps)
            if sensitive_kw_gaps
            else _none_or_covered(baseline.extra_sensitive_keywords_map),
            comparator="superset",
            missing=tuple(f"{area}: {gap}" for area, gap in sensitive_kw_gaps),
            missing_items=tuple(
                {"key": area, "items": list(gap)}
                for area, gap in sensitive_kw_gaps
            ),
        )
    )

    # --- change-freeze windows (superset by canonical identity) --------- #
    # The subject must freeze at least everything the baseline freezes (it may
    # add more). Windows are compared on their canonical key (time, not their
    # human label), so a re-worded reason does not read as a gap.
    s_freeze = {window_key(w) for w in subject.change_freeze_windows}
    freeze_gaps = [
        describe_window(w)
        for w in baseline.change_freeze_windows
        if window_key(w) not in s_freeze
    ]
    findings.append(
        PolicyCheckFinding(
            "change_freeze_windows",
            not freeze_gaps,
            f"missing {freeze_gaps}"
            if freeze_gaps
            else (
                "baseline requires none"
                if not baseline.change_freeze_windows
                else f"covers all {len(baseline.change_freeze_windows)} baseline "
                "window(s)"
            ),
            comparator="superset",
            missing=tuple(freeze_gaps),
            missing_items=tuple({"item": window} for window in freeze_gaps),
        )
    )

    return PolicyComparison(findings=tuple(findings))


def _none_or_covered(baseline_map: Mapping[str, Any]) -> str:
    """Detail text for a collection knob with no shortfall."""
    if not baseline_map:
        return "baseline requires none"
    return f"covers all {len(baseline_map)} baseline entr(y/ies)"


def comparison_to_dict(comparison: PolicyComparison) -> dict[str, Any]:
    """Serialise a :class:`PolicyComparison` to a plain JSON-able dict.

    One definition shared by every machine-readable consumer of the strictness
    check - the ``foundry-policy check --format json`` CLI output and the in-app
    ``GET /metrics/policy/check`` endpoint - so the verdict surfaced on a
    dashboard can't drift from the verdict a CI step exits on. Read-only: it just
    reshapes an already-computed comparison, it changes no gate.

    Each finding carries both the human ``detail`` string **and** the typed
    ``comparator`` / ``subject`` / ``baseline`` / ``missing`` / ``missing_items``
    fields, so a CI step can compare the numeric values (or read exactly which
    collection items fall short, by key) directly, without parsing the prose.
    """
    return {
        "ok": comparison.ok,
        "findings": [
            {
                "knob": finding.knob,
                "ok": finding.ok,
                "detail": finding.detail,
                "comparator": finding.comparator,
                "subject": finding.subject,
                "baseline": finding.baseline,
                "missing": list(finding.missing),
                "missing_items": [dict(item) for item in finding.missing_items],
            }
            for finding in comparison.findings
        ],
        "weaknesses": [finding.knob for finding in comparison.weaknesses],
    }


__all__ = [
    "PolicyPreset",
    "PolicyCheckFinding",
    "PolicyComparison",
    "available_preset_names",
    "get_preset",
    "list_presets",
    "load_preset_yaml",
    "load_preset_settings",
    "resolve_settings",
    "effective_policy_summary",
    "compare_policy_strictness",
    "comparison_to_dict",
]
