"""Live user-loadable policy bundles: the non-overridable-floor overlay (#154).

The starter library (``policy/library/``, issue #31) is *copy-to-adopt*: a buyer
copies a preset's YAML into ``foundry.yaml`` by hand. This module is the missing
*live overlay* the headline criterion of #31 asked for - an operator points
Foundry at a separately-authored **policy bundle** (``policy.bundle_path``) that
is loaded and merged **on top of** the deployment's resolved config every time
``Settings.load`` runs, so a security team can ship a reviewed, independently
versioned bundle without forking the app or hand-editing the main config.

The whole safety story is one property: **the merge is strictly additive, so the
base config + built-in gate rules remain a non-overridable floor.** Each knob is
merged in the direction the gate enforces (the same directions
:func:`~foundry.policy.library.compare_policy_strictness` verifies):

- **superset / union** - ``forbidden_globs``, the per-repo / per-path role and
  forbidden-glob maps, the risk-escalation maps (``sensitive_path_globs`` /
  ``extra_sensitive_keywords``) and the ``change_freeze_windows``: the bundle can
  only *add* protected paths, required roles, escalations and freezes.
- **max (higher is stricter)** - ``repo_confidence_threshold``,
  ``min_approvals``, ``repo_min_approvals`` (per repo): the bundle can only raise
  the bar.
- **min (lower is stricter)** - ``max_files_changed``, ``max_agent_retries``,
  ``max_cost_per_run`` (``None`` = no cap = weakest): the bundle can only tighten
  the cap, never loosen one the base set.
- **subset / intersection** - ``retry_on``: the bundle can only *narrow* which
  PR-failure reasons trigger an autonomous re-dispatch (handing more to a human),
  never add a new autonomous-retry trigger.

A value in the bundle that *would* weaken the floor is therefore ignored - the
stricter of (base, bundle) always wins - so the bundle is a one-way ratchet
towards stricter (invariant #1). As defence-in-depth the result is re-checked
against the base with ``compare_policy_strictness`` at load and a weakening fails
**closed**, so even a future merge bug can't silently relax the gate.

Two invariants this deliberately does **not** touch:

- **Invariant #2 (Python ↔ Rego lock-step).** The overlay only changes the
  *values* of policy knobs that already flow to both backends identically
  (``repo_confidence_threshold`` injected into the OPA input, per-repo roles
  stamped onto ``PolicyInput.repo.required_roles``, and the orchestrator-only
  knobs that have no Rego mirror). It adds **no new gate rule and no new
  ``PolicyInput`` field**, so ``foundry.rego`` and the shared ``policy_vectors``
  are untouched and the two engines stay in lock-step with no edit.
- **Invariant #5 (authorization from committed config).** The bundle is loaded
  from a configured *path*, never a request payload, exactly like the rest of
  ``foundry.yaml``.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping

from foundry.policy.freeze import window_key

if TYPE_CHECKING:  # pragma: no cover - typing only
    from foundry.config import Settings

# The policy knobs a user bundle may set - exactly the gate's strictness surface
# that ``compare_policy_strictness`` verifies. A bundle key outside this set is a
# hard error: a *policy* bundle carries only policy, never secrets, tracker/agent
# wiring or other behaviour (it must not be a back door for unrelated config, and
# authorization stays committed-config-derived - invariant #5).
POLICY_OVERLAY_FIELDS: frozenset[str] = frozenset(
    {
        "repo_confidence_threshold",
        "max_files_changed",
        "forbidden_globs",
        "repo_forbidden_globs",
        "repo_required_roles",
        "sensitive_path_globs",
        "extra_sensitive_keywords",
        "min_approvals",
        "repo_min_approvals",
        "path_required_roles",
        "change_freeze_windows",
        "max_agent_retries",
        "retry_on",
        "max_cost_per_run",
    }
)


def _union_list(base: tuple[str, ...], extra: tuple[str, ...]) -> tuple[str, ...]:
    """Order-preserving union: base items first, then any new bundle items."""
    out = list(base)
    seen = set(base)
    for item in extra:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return tuple(out)


def _union_map(
    base: tuple[tuple[str, tuple[str, ...]], ...],
    extra: tuple[tuple[str, tuple[str, ...]], ...],
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Per-key union of a (key -> tuple-of-strings) map, preserving order.

    Base keys (in base order) come first, then keys the bundle introduces; within
    a key the base values are kept and the bundle's extras appended. The bundle
    can only ever *add* entries - it can neither drop a key nor drop a value the
    base set - so a per-repo/per-path/area protection is never weakened.
    """
    values: dict[str, list[str]] = {}
    order: list[str] = []
    for key, items in base:
        values[key] = list(items)
        order.append(key)
    for key, items in extra:
        if key not in values:
            values[key] = []
            order.append(key)
        for item in items:
            if item not in values[key]:
                values[key].append(item)
    return tuple((key, tuple(values[key])) for key in order)


def _max_map(
    base: tuple[tuple[str, int], ...], extra: tuple[tuple[str, int], ...]
) -> tuple[tuple[str, int], ...]:
    """Per-key max of a (key -> int) map (higher is stricter), order-preserving."""
    values: dict[str, int] = {}
    order: list[str] = []
    for key, count in base:
        values[key] = count
        order.append(key)
    for key, count in extra:
        if key not in values:
            values[key] = count
            order.append(key)
        else:
            values[key] = max(values[key], count)
    return tuple((key, values[key]) for key in order)


def _intersect(base: tuple[str, ...], extra: tuple[str, ...]) -> tuple[str, ...]:
    """Intersection in base order (subset is stricter - fewer auto-retry triggers)."""
    allowed = set(extra)
    return tuple(item for item in base if item in allowed)


def _union_windows(base: tuple[Any, ...], extra: tuple[Any, ...]) -> tuple[Any, ...]:
    """Union of change-freeze windows by canonical (label-agnostic) key."""
    keys = {window_key(window) for window in base}
    out = list(base)
    for window in extra:
        key = window_key(window)
        if key not in keys:
            keys.add(key)
            out.append(window)
    return tuple(out)


def _min_cap(base: float | None, extra: float | None) -> float | None:
    """Tighter of two budget caps; ``None`` means no cap (the weakest)."""
    if base is None:
        return extra
    if extra is None:
        return base
    return min(base, extra)


# field name -> merge(base_value, bundle_value) -> stricter value. Each direction
# matches what compare_policy_strictness treats as "at least as strict".
_MERGE: dict[str, Callable[[Any, Any], Any]] = {
    "repo_confidence_threshold": lambda base, extra: max(base, extra),
    "max_files_changed": lambda base, extra: min(base, extra),
    "min_approvals": lambda base, extra: max(base, extra),
    "max_agent_retries": lambda base, extra: min(base, extra),
    "max_cost_per_run": _min_cap,
    "forbidden_globs": _union_list,
    "retry_on": _intersect,
    "repo_forbidden_globs": _union_map,
    "repo_required_roles": _union_map,
    "path_required_roles": _union_map,
    "sensitive_path_globs": _union_map,
    "extra_sensitive_keywords": _union_map,
    "repo_min_approvals": _max_map,
    "change_freeze_windows": _union_windows,
}


def overlay_policy_values(
    base: "Settings", overlay: Mapping[str, Any]
) -> dict[str, Any]:
    """Compute the strictly-stricter merge of ``overlay`` on top of ``base``.

    ``overlay`` is a mapping of :class:`~foundry.config.Settings` *field names*
    (the policy knobs only) to the values parsed from the bundle. Returns a dict
    of the merged field values, suitable for ``base._with(...)``. Keys outside
    :data:`POLICY_OVERLAY_FIELDS` are rejected (fail-closed) so a bundle can never
    smuggle in non-policy config.
    """
    unknown = set(overlay) - POLICY_OVERLAY_FIELDS
    if unknown:
        raise ValueError(
            "policy bundle may only set policy knobs "
            f"({sorted(POLICY_OVERLAY_FIELDS)}); unexpected key(s): {sorted(unknown)}"
        )
    return {
        field: _MERGE[field](getattr(base, field), value)
        for field, value in overlay.items()
    }


def load_policy_bundle(path: str | Path) -> dict[str, Any]:
    """Parse a user policy bundle file into a dict of Settings field overrides.

    Reuses the main YAML reader so a bundle is parsed and shaped exactly like the
    deployment's own ``foundry.yaml`` (same key names, same coercions). A missing
    file raises rather than silently loading nothing - a configured-but-absent
    bundle is an operator error, not a no-op that would leave the gate quietly
    un-tightened.
    """
    from foundry.config import _from_yaml

    bundle_path = Path(path)
    if not bundle_path.exists():
        raise ValueError(f"policy.bundle_path {str(path)!r} does not exist")
    return _from_yaml(bundle_path)


def apply_policy_bundle(base: "Settings") -> "Settings":
    """Return ``base`` with its configured policy bundle merged in as a strict overlay.

    Called by ``Settings.load`` when ``policy.bundle_path`` is set. The merge is
    strictly additive (see the module docstring); as a fail-closed guarantee the
    result is re-verified against ``base`` with ``compare_policy_strictness`` and
    a bundle that would weaken any built-in denial or approval requirement raises
    at load.
    """
    overlay = load_policy_bundle(base.policy_bundle_path)  # type: ignore[arg-type]
    merged = overlay_policy_values(base, overlay)
    result = base._with(merged)

    from foundry.policy.library import compare_policy_strictness

    comparison = compare_policy_strictness(result, base)
    if not comparison.ok:  # pragma: no cover - additive merge can't reach here
        weak = ", ".join(finding.knob for finding in comparison.weaknesses)
        raise ValueError(
            f"policy bundle {base.policy_bundle_path!r} would weaken the built-in "
            f"floor on: {weak}; a policy bundle may only ever make the gate stricter"
        )
    return result


__all__ = [
    "POLICY_OVERLAY_FIELDS",
    "overlay_policy_values",
    "load_policy_bundle",
    "apply_policy_bundle",
]
