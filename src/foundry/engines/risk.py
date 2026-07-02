"""Risk classification stage.

Produces a :class:`RiskAssessment` from the ticket text and context. This is
*advisory* input to the policy gate - it flags sensitive areas and proposes a
risk level, but the hard allow/deny decision is made by ``foundry.policy``.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence

from foundry.schemas.analysis import TicketAnalysis
from foundry.schemas.common import (
    SENSITIVE_AREA_KEYS,
    AgentMode,
    ApprovalRole,
    OverallRisk,
)
from foundry.schemas.context import ContextBundle
from foundry.schemas.risk import (
    DiffRiskFindings,
    RiskAssessment,
    RiskEvidence,
    SensitiveAreas,
)
from foundry.schemas.ticket import RawTicket

# Keyword signals for each sensitive area. Prefer multi-word phrases over
# single words to reduce false positives (e.g. "error" is not a payment signal,
# "checkout" alone doesn't mean payments, "infra" alone is too broad).
_SENSITIVE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "auth": ("oauth", "sso", "session token", "login flow", "authentication", "authorisation",
             "authorization", "access token", "jwt", "password reset"),
    "payments": ("payment", "billing", "stripe", "invoice", "payment gateway",
                 "credit card", "card number", "transaction"),
    "customer_data": ("customer data", "customer record", "personal data"),
    "pii": ("pii", "gdpr", "email address", "phone number", "passport",
            "date of birth", "national insurance", "social security"),
    "database_migration": ("migration", "schema change", "alter table", "drop column",
                            "drop table", "add column"),
    "infrastructure": ("terraform", "kubernetes", "helm chart", "deployment config",
                       "infrastructure as code", "k8s manifest"),
    "production_deploy": ("deploy to production", "prod deploy", "release to prod",
                          "production release"),
}


def merge_sensitive_keywords(
    extra: Mapping[str, Sequence[str]] | None,
) -> dict[str, tuple[str, ...]]:
    """Layer operator-supplied keywords *on top of* the built-in floor.

    The built-in ``_SENSITIVE_KEYWORDS`` table is the offline/no-key reference
    detection set. A deployment can extend it - teach the heuristic its own
    domain vocabulary (e.g. ``"pan"``/``"cardholder"`` for ``payments``,
    ``"member record"`` for ``customer_data``) - via ``risk.extra_sensitive_keywords``
    (issue #31). This is the ticket-text twin of the already-configurable
    diff-stage ``policy.sensitive_path_globs``.

    Strictly additive: every built-in keyword is preserved and the extras are
    appended (lower-cased, de-duplicated, built-ins first), so detection can only
    ever be *added to*, never removed - more keywords flag more areas, never
    fewer, so risk can only escalate (invariant #1). Extras keyed on an area that
    is not a built-in sensitive area are ignored here; ``Settings`` validates the
    area names at load (fail-closed), so a typo never reaches this point silently.
    """
    merged = {area: tuple(keywords) for area, keywords in _SENSITIVE_KEYWORDS.items()}
    for area, keywords in (extra or {}).items():
        if area not in merged:
            continue
        seen = set(merged[area])
        added = []
        for kw in keywords:
            normalised = kw.lower()
            if normalised and normalised not in seen:
                seen.add(normalised)
                added.append(normalised)
        if added:
            merged[area] = (*merged[area], *added)
    return merged


@dataclass(frozen=True)
class CustomRiskCategory:
    """An operator-defined risk category beyond the fixed built-in areas (#155).

    The fixed seven :data:`SENSITIVE_AREA_KEYS` cover Foundry's reference risk
    vocabulary, and operators can already *extend their triggers* without forking
    (``risk.extra_sensitive_keywords`` for ticket text, ``policy.sensitive_path_globs``
    for diffs). What they could not do is declare a genuinely *new*, dynamically
    named category - e.g. ``crypto_keys`` or ``gdpr_subject_data`` - with its own
    triggers mapping to its own approval roles. This is that category.

    A category *fires* when either trigger matches:

    * ``keywords`` appear in a ticket's title/description (intake), or
    * ``path_globs`` match a changed file in a PR diff (PR push).

    A fired category demands its ``required_roles`` as approval roles. It is
    **escalate-only** (invariant #1): a category can only ever *add* a required
    approval, never drop a built-in area's role or lower the risk level. The
    name is validated (a slug that cannot collide with a built-in area name), so
    a custom category can never shadow or weaken a built-in one. The roles reach
    the gate via the resolved-roles channel both policy backends already read
    (``PolicyInput.repo.required_roles``), so there is no new gate rule and no
    ``foundry.rego`` change (invariant #2 stays satisfied for free).
    """

    name: str
    keywords: tuple[str, ...] = ()
    path_globs: tuple[str, ...] = ()
    required_roles: tuple[str, ...] = ()

    def matched_keywords(self, blob: str) -> list[str]:
        """Keywords from this category present in a (lower-cased) ticket blob."""
        return [kw for kw in self.keywords if kw in blob]

    def matches_path(self, path: str) -> bool:
        """True if a changed file path matches any of this category's globs.

        Uses the depth-agnostic :func:`escalating_path_match`: this is an
        escalate-only gate (a match only ever *adds* a required approval role), so
        an operator's bare ``crypto_keys: ["keys/**"]`` protects a nested
        ``app/keys/...`` too, not just the repo root (issue #179).
        """
        return any(escalating_path_match(path, pattern) for pattern in self.path_globs)


def custom_category_from_mapping(name: str, data: Any) -> CustomRiskCategory:
    """Build a :class:`CustomRiskCategory` from a config mapping.

    Coercion only - keywords are lower-cased to match :meth:`RawTicket.risk_blob`
    (which lower-cases), mirroring the built-in keyword floor. The semantic
    checks (real roles, a non-colliding name, at least one trigger) live in
    :func:`validate_custom_categories` so they surface as a clear ``Settings``
    load error.
    """
    if not isinstance(data, Mapping):
        raise ValueError(
            f"risk.custom_risk_categories entry {name!r} must be a mapping, got "
            f"{data!r}"
        )
    keywords = tuple(str(kw).lower() for kw in (data.get("keywords") or ()))
    path_globs = tuple(str(g) for g in (data.get("path_globs") or ()))
    required_roles = tuple(str(r) for r in (data.get("required_roles") or ()))
    return CustomRiskCategory(
        name=str(name),
        keywords=keywords,
        path_globs=path_globs,
        required_roles=required_roles,
    )


def validate_custom_categories(categories: Sequence[CustomRiskCategory]) -> None:
    """Raise ``ValueError`` if any custom risk category is malformed (#155).

    Fail-closed at load, like the other risk/policy knobs: a typo'd role or a
    trigger-less category would silently never escalate, leaving an operator
    believing a category was protecting them when it was inert. Each category
    must (a) have a slug name that does not collide with a built-in sensitive
    area (so it can never shadow or weaken one), (b) be unique, (c) demand at
    least one valid approval role, and (d) declare at least one trigger.
    """
    valid_roles = {r.value for r in ApprovalRole}
    builtin = set(SENSITIVE_AREA_KEYS)
    seen: set[str] = set()
    for category in categories:
        name = category.name
        if not name or not all(ch.isalnum() or ch == "_" for ch in name):
            raise ValueError(
                f"risk.custom_risk_categories name {name!r} must be a non-empty "
                "slug (letters, digits, underscores)"
            )
        if name in builtin:
            raise ValueError(
                f"risk.custom_risk_categories name {name!r} collides with a "
                f"built-in sensitive area; choose a distinct name (built-ins: "
                f"{sorted(builtin)})"
            )
        if name in seen:
            raise ValueError(
                f"risk.custom_risk_categories lists {name!r} more than once"
            )
        seen.add(name)
        if not category.required_roles:
            raise ValueError(
                f"risk.custom_risk_categories {name!r} must list at least one "
                "required approval role, or it could never escalate anything"
            )
        bad = [r for r in category.required_roles if r not in valid_roles]
        if bad:
            raise ValueError(
                f"risk.custom_risk_categories {name!r} lists unknown approval "
                f"roles {bad}; valid roles are {sorted(valid_roles)}"
            )
        if not category.keywords and not category.path_globs:
            raise ValueError(
                f"risk.custom_risk_categories {name!r} needs at least one trigger "
                "(keywords and/or path_globs), or it would never fire"
            )


def glob_match(path: str, pattern: str) -> bool:
    """fnmatch with a usable ``**/`` prefix: ``**/auth/**`` also matches a path
    that *starts* with ``auth/`` (fnmatch alone would require a leading slash).
    """
    if fnmatch.fnmatch(path, pattern):
        return True
    return pattern.startswith("**/") and fnmatch.fnmatch(path, pattern[3:])


def escalating_path_match(path: str, pattern: str) -> bool:
    """Depth-agnostic path match for the *escalate-only* path gates.

    Like :func:`glob_match`, but a **bare relative** pattern also matches at any
    directory depth: ``secrets/**`` matches ``app/secrets/key.pem``, not just a
    top-level ``secrets/``. Operators write the natural ``secrets/**`` /
    ``migrations/**`` and expect every such directory covered wherever it lives;
    ``fnmatch`` anchors at the string start, so a bare pattern otherwise matches
    only the top level and a nested match silently slips through - the gate the
    operator configured is only partially enforced.

    This is the shared matcher for **every escalate-only path gate**, where
    matching *more* paths only ever makes the gate *stricter* (AGENTS.md
    invariant #1):

    * the sticky forbidden-path **BLOCK** (``_forbidden_violations``, issue #177,
      the original consumer - see the :data:`forbidden_path_match` alias),
    * the diff-stage **sensitive-area** globs (:func:`sensitive_areas_for_paths`,
      ``policy.sensitive_path_globs``),
    * **per-path approval roles** (``_unapproved_path_roles``,
      ``policy.path_required_roles``),
    * operator-defined **custom-risk-category** path globs
      (:meth:`CustomRiskCategory.matches_path`, ``risk.custom_risk_categories``).

    All of these can only ever *add* a denial / required approval / risk area, so
    a broader match is always the safe direction - a bare operator glob that only
    protected the repo root now protects the directory at any depth (issue #179,
    the operator-glob follow-up to #177, which fixed only the forbidden BLOCK).

    It is deliberately **not** folded into :func:`glob_match`, which is shared
    with :func:`files_outside_scope` / :func:`_scope_entry_covers` (plan-scope
    drift) and :func:`diff_touches_tests`: for those the direction is inverted -
    matching *more* paths marks *more* files in-scope / recognised-as-a-test, so
    *fewer* runs escalate, which would **weaken** the gate. The shared matcher
    must stay as-is there.

    A pattern that is rooted (``/…``) or already anchored (``**/…``) is honoured
    exactly as written - only a bare relative pattern is expanded to any depth,
    so the new matches are a strict superset of :func:`glob_match` (stricter
    only, never a behaviour change for the depth-agnostic default globs).
    """
    if glob_match(path, pattern):
        return True
    if pattern.startswith("/") or pattern.startswith("**/"):
        return False
    return glob_match(path, "**/" + pattern)


# Historical name: the sticky forbidden-path BLOCK (issue #177) was the first
# consumer of this matcher. Kept as an alias so the forbidden call-site and its
# tests read naturally; the matcher itself is shared by every escalate-only path
# gate (see :func:`escalating_path_match`).
forbidden_path_match = escalating_path_match


def _normalise_scope_entry(entry: str) -> str:
    """Trim a plan ``expected_files_or_areas`` entry to a comparable form."""
    entry = (entry or "").strip()
    if entry.startswith("./"):
        entry = entry[2:]
    return entry.rstrip("/")


def _scope_entry_covers(path: str, entry: str) -> bool:
    """True if a single plan-scope entry covers a changed file path.

    An entry may be an exact path, a glob (``*``/``**``/``?``), a directory
    prefix (``src/api`` covers ``src/api/app.py``), or a bare area/segment name
    (``favourites`` covers ``src/features/favourites/index.ts``).
    """
    if path == entry or glob_match(path, entry):
        return True
    if path.startswith(entry + "/"):
        return True
    # A bare area name (no path separator, no glob metacharacters) matches when
    # it appears as a whole path segment - so the LLM planner naming an "area"
    # rather than a file still scopes the diff.
    if "/" not in entry and not any(ch in entry for ch in "*?["):
        return entry in path.split("/")
    return False


def files_outside_scope(scope: Sequence[str], files: Sequence[str]) -> list[str]:
    """Changed files that fall outside *every* declared plan-scope entry.

    ``scope`` is a plan's ``expected_files_or_areas``. Matching is deliberately
    *generous* (see :func:`_scope_entry_covers`): the drift check this powers is
    escalate-only - it can hand a straying PR to a human but never release one -
    so an over-broad match merely keeps today's behaviour while a missed match
    would needlessly escalate (the safe direction). An empty/whitespace-only
    scope returns ``[]`` (nothing to check), so the check is inert unless the
    planner actually declared expected files/areas.
    """
    cleaned = [e for e in (_normalise_scope_entry(s) for s in scope) if e]
    if not cleaned:
        return []
    return [
        f for f in files if not any(_scope_entry_covers(f, e) for e in cleaned)
    ]


def _scope_entry_covers_at_depth(path: str, entry: str) -> bool:
    """Depth-agnostic form of :func:`_scope_entry_covers` for the *escalate-on-match*
    out-of-scope gate.

    :func:`_scope_entry_covers` is depth-**anchored** (a bare ``payments/**`` /
    ``src/vendor`` covers only a *top-level* ``payments/`` / ``src/vendor/``)
    because it is shared with the plan-scope **drift** check
    (:func:`files_outside_scope`), which escalates when a file matches *nothing* -
    there, matching *more* paths marks *more* files in-scope, so *fewer* runs
    escalate, and broadening would **weaken** the gate (invariant #1). See the
    same polarity argument in :func:`escalating_path_match`.

    The out-of-scope gate (:func:`files_matching_scope`) has the **opposite**
    polarity: it escalates when a file *matches* an entry. So there, under-matching
    a nested bare entry silently **fails to escalate** - a plan's
    ``out_of_scope: ["payments/**"]`` protects only a repo-root ``payments/`` and
    lets a nested ``app/payments/charge.py`` ride through, exactly the depth gap
    :func:`escalating_path_match` was created to close for the other escalate-only
    path gates (issue #179). This variant restores depth-agnostic coverage for that
    gate while leaving the drift check's anchored matcher untouched.

    Mirrors :func:`escalating_path_match`: a rooted (``/…``) or already-anchored
    (``**/…``) entry is honoured exactly as written; only a **bare relative** entry
    is expanded to any depth. Broadening is the safe direction here - a match can
    only ever *add* an escalation (invariant #1).
    """
    if _scope_entry_covers(path, entry):
        return True
    if entry.startswith("/") or entry.startswith("**/"):
        return False
    if any(ch in entry for ch in "*?["):
        # Bare relative glob (``payments/**``): reuse the shared escalate-only
        # matcher, which matches such a glob at any directory depth.
        return escalating_path_match(path, entry)
    # Bare relative directory/path entry (``src/vendor``): it covers a file when
    # its segments appear as a contiguous run anywhere in the path - a nested
    # directory prefix, not just the repo root. (The single-segment bare-area
    # case is already depth-agnostic in ``_scope_entry_covers``; this generalises
    # it to multi-segment prefixes.)
    entry_segs = entry.split("/")
    segs = path.split("/")
    return any(
        segs[i : i + len(entry_segs)] == entry_segs
        for i in range(len(segs) - len(entry_segs) + 1)
    )


def files_matching_scope(scope: Sequence[str], files: Sequence[str]) -> list[str]:
    """Changed files that match *any* declared scope entry - the inverse of
    :func:`files_outside_scope`.

    ``scope`` is a plan's ``out_of_scope``: paths/areas the plan explicitly
    promised **not** to touch. A returned file therefore hit a forbidden-by-plan
    entry. Matching uses the depth-agnostic :func:`_scope_entry_covers_at_depth`
    (exact / glob / directory prefix / bare-area segment, matched at *any* depth
    for bare relative entries), so an LLM planner naming an out-of-scope *area* -
    or a bare ``payments/**`` glob - flags a diff that reaches into it wherever it
    is nested, not just at the repo root (the escalate-only polarity means
    broadening only ever *adds* an escalation - invariant #1). An empty/
    whitespace-only scope returns ``[]`` (nothing declared off-limits), so the
    check this powers is inert unless the planner actually scoped something out.
    The check is escalate-only - it can hand a PR that touches an off-limits path
    to a human, never release one.
    """
    cleaned = [e for e in (_normalise_scope_entry(s) for s in scope) if e]
    if not cleaned:
        return []
    return [
        f for f in files if any(_scope_entry_covers_at_depth(f, e) for e in cleaned)
    ]


def diff_touches_tests(files: Sequence[str], test_globs: Sequence[str]) -> bool:
    """True if any changed file looks like a test, per the configured globs.

    Powers the plan-tests-satisfaction signal (issue #169, slice 2): when the
    approved plan promised tests but the diff touches *no* test file, the run is
    escalated to a human. ``test_globs`` is the operator-tunable test-path
    convention (default ``**/test_*.py`` / ``**/*_test.*`` / ``tests/**`` and a
    few common JS/TS layouts). Reuses the same ``**/``-aware :func:`glob_match`
    the sensitive-path checks use, so a pattern like ``**/test_*.py`` also matches
    a top-level ``test_foo.py``. An empty/whitespace-only ``test_globs`` returns
    ``False`` (no convention configured = nothing is recognised as a test), so the
    escalate-only check this powers stays inert rather than firing on every diff.
    """
    cleaned = [p for p in (g.strip() for g in test_globs) if p]
    if not cleaned:
        return False
    return any(glob_match(f, p) for f in files for p in cleaned)


def sensitive_areas_for_paths(
    files: list[str], globs_map: Mapping[str, tuple[str, ...]]
) -> dict[str, list[str]]:
    """Classify changed file paths against sensitive-area globs.

    This is the diff-aware half of risk classification: the upfront pass reads
    the *ticket text*, but the risk that matters materialises in the *diff*.
    Returns ``{area: [matching files...]}`` for every area actually touched.

    Matching uses the depth-agnostic :func:`escalating_path_match`: flagging a
    sensitive area is escalate-only (it only ever *adds* an area/approval), so an
    operator's bare ``sensitive_path_globs`` entry (or the built-in ``infra/**``)
    flags the directory at any depth, not just the repo root (issue #179).
    """
    touched: dict[str, list[str]] = {}
    for path in files:
        for area, patterns in globs_map.items():
            if any(escalating_path_match(path, p) for p in patterns):
                touched.setdefault(area, []).append(path)
    return {area: sorted(paths) for area, paths in sorted(touched.items())}


class RiskClassifier(Protocol):
    def classify(
        self, ticket: RawTicket, analysis: TicketAnalysis, context: ContextBundle
    ) -> RiskAssessment: ...


class DiffRiskClassifier(Protocol):
    """Classifies the sensitive areas a PR diff touches, from its file paths."""

    def classify_diff(
        self, files: list[str], ticket: RawTicket | None = None
    ) -> DiffRiskFindings: ...


class GlobDiffRiskClassifier:
    """Deterministic diff-stage classifier: sensitive-area path globs only.

    This is the floor every other diff classifier builds on - its matches are
    never dropped, only added to.
    """

    def __init__(self, globs_map: Mapping[str, tuple[str, ...]]) -> None:
        self._globs = globs_map

    def classify_diff(
        self, files: list[str], ticket: RawTicket | None = None
    ) -> DiffRiskFindings:
        areas = sensitive_areas_for_paths(files, self._globs)
        evidence = [
            RiskEvidence(
                area=area,
                detail=f"changed path(s) match sensitive globs: {', '.join(paths)}",
                source="diff",
            )
            for area, paths in areas.items()
        ]
        return DiffRiskFindings(areas=areas, evidence=evidence)


class HeuristicRiskClassifier:
    """Keyword-driven reference risk classifier.

    ``keywords`` defaults to the built-in ``_SENSITIVE_KEYWORDS`` floor. A
    deployment can pass a merged map (see :func:`merge_sensitive_keywords`) to
    extend detection with its own domain vocabulary without forking
    (``risk.extra_sensitive_keywords``, issue #31) - strictly additive, so the
    classifier can only ever flag *more* areas, never fewer.
    """

    def __init__(self, keywords: Mapping[str, Sequence[str]] | None = None) -> None:
        self._keywords: Mapping[str, Sequence[str]] = (
            keywords if keywords is not None else _SENSITIVE_KEYWORDS
        )

    def classify(
        self, ticket: RawTicket, analysis: TicketAnalysis, context: ContextBundle
    ) -> RiskAssessment:
        # Use risk_blob (title + description only) to avoid stale comments
        # inflating risk scores.
        blob = ticket.risk_blob()
        hits = {
            area: [k for k in keywords if k in blob]
            for area, keywords in self._keywords.items()
        }
        sensitive = SensitiveAreas(**{area: bool(found) for area, found in hits.items()})

        reasons: list[str] = []
        evidence: list[RiskEvidence] = []
        for area in sensitive.names():
            reasons.append(f"Ticket text suggests it touches '{area}'.")
            evidence.append(
                RiskEvidence(
                    area=area,
                    detail="keyword(s) in ticket title/description: "
                    + ", ".join(f"'{k}'" for k in hits[area]),
                    source="heuristic",
                )
            )

        overall = self._overall_risk(sensitive, context)
        if overall is OverallRisk.BLOCKED:
            reasons.append("No confident repository match; work cannot be scoped.")

        required = self._required_approvals(sensitive)
        mode = self._agent_mode(overall, sensitive)

        return RiskAssessment(
            overall_risk=overall,
            risk_reasons=reasons,
            sensitive_areas=sensitive,
            allowed_agent_mode=mode,
            required_approvals=required,
            evidence=evidence,
        )

    @staticmethod
    def _overall_risk(sensitive: SensitiveAreas, context: ContextBundle) -> OverallRisk:
        if not context.has_confident_repository():
            return OverallRisk.BLOCKED
        if sensitive.production_deploy or sensitive.database_migration:
            return OverallRisk.HIGH
        if sensitive.auth or sensitive.payments or sensitive.customer_data or sensitive.pii:
            return OverallRisk.HIGH
        if sensitive.infrastructure:
            return OverallRisk.MEDIUM
        return OverallRisk.LOW

    @staticmethod
    def _required_approvals(sensitive: SensitiveAreas) -> list[ApprovalRole]:
        required: list[ApprovalRole] = []
        if sensitive.auth or sensitive.infrastructure:
            required.append(ApprovalRole.ENGINEERING)
        if sensitive.customer_data or sensitive.pii or sensitive.payments:
            required.append(ApprovalRole.SECURITY)
        # Preserve order, de-duplicate.
        seen: set[ApprovalRole] = set()
        return [r for r in required if not (r in seen or seen.add(r))]

    @staticmethod
    def _agent_mode(overall: OverallRisk, sensitive: SensitiveAreas) -> AgentMode:
        if overall in (OverallRisk.BLOCKED, OverallRisk.HIGH):
            return AgentMode.HUMAN_ONLY
        return AgentMode.DRAFT_PR
