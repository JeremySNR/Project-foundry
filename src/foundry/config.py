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

from foundry.compliance.controls import (
    DEFAULT_CONTROL_MAPPINGS,
    KNOWN_EVIDENCE_SECTIONS,
    ControlMapping,
    mappings_from_config,
)
from foundry.engines.risk import (
    CustomRiskCategory,
    custom_category_from_mapping,
    validate_custom_categories,
)
from foundry.policy.freeze import (
    ChangeFreezeWindow,
    validate_windows,
    window_from_mapping,
)

_TRUE = {"1", "true", "yes", "on"}

# Root-anchored *and* depth-agnostic variants: `migrations/**` only matches a
# top-level dir, so the `**/...` siblings ensure a nested `services/api/migrations/`
# is caught by the sticky forbidden-path block too (not just the softer
# sensitive-area escalation). `**/.env*` and `**/secrets/**` already match at any
# depth via the `**/` prefix handling in `glob_match`.
DEFAULT_FORBIDDEN_GLOBS = (
    "infra/**",
    "**/infra/**",
    "migrations/**",
    "**/migrations/**",
    "**/.env*",
    "**/secrets/**",
)
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


def _parse_org_secret_pairs(raw: str) -> tuple[tuple[str, str], ...]:
    """Parse ``FOUNDRY_WEBHOOK_ORG_SECRETS`` (``org=secret`` pairs, comma-sep).

    Split each pair on the first ``=`` so a base64/Fernet secret's ``=`` padding
    survives; blank chunks are skipped. Shape only — semantic validation (blank /
    reserved / duplicate / reused) happens fail-closed at ``Settings`` load via
    ``WebhookOrgSecrets.from_pairs``.
    """
    pairs: list[tuple[str, str]] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        org, sep, secret = chunk.partition("=")
        if not sep:
            raise ValueError(
                "FOUNDRY_WEBHOOK_ORG_SECRETS entries must be 'org=secret' pairs, "
                f"got {chunk!r}"
            )
        pairs.append((org.strip(), secret.strip()))
    return tuple(pairs)


@dataclass(frozen=True)
class Settings:
    # --- storage (secret: env) ---
    database_url: str = "sqlite+pysqlite:///:memory:"

    # --- webhook signing secrets (secret: env) ---
    linear_webhook_secret: str = ""
    github_webhook_secret: str | None = None

    # --- per-org webhook secrets (secret: env; issue #34 follow-up) ---
    # Map a verified webhook delivery to its tenant org. Webhooks carry no OIDC
    # token, so the verified principal is the shared secret that authenticated
    # the delivery: a delivery signed (HMAC: Linear/GitHub) or tokened
    # (GitLab/Jira) with org X's dedicated secret runs in org X; the global
    # secrets above resolve to the default org. Org comes from *which committed
    # secret matched*, never the payload (invariant #5). Set via
    # FOUNDRY_WEBHOOK_ORG_SECRETS as comma-separated ``org=secret`` pairs; empty
    # (the default) => single-tenant, every delivery in the default org.
    webhook_org_secrets: tuple[tuple[str, str], ...] = ()

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
    # Jira webhook UIs that cannot set request headers can pass the shared
    # secret as a ?token= query parameter. Query-string secrets leak into
    # access logs, proxies, and link history, so this is off by default:
    # the token is header-only (X-Foundry-Webhook-Token) unless explicitly
    # opted in here (behaviour: yaml).
    jira_allow_query_token: bool = False

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

    # --- artifact encryption at rest (secret: env); None => plaintext ---
    # Fernet key for encrypting artifact payloads (foundry_artifacts.content_json)
    # at rest. Comma-separate keys to rotate (the first encrypts; all are tried
    # for decrypt). Unset => payloads stored as plaintext (the historical
    # behaviour). Needs the optional 'crypto' extra (cryptography); a key set
    # without it fails closed at startup. See db/encryption.py.
    artifact_encryption_key: str | None = None

    # --- SCIM 2.0 provisioning credential (secret: env; issue #157) ---
    # The bearer token an IdP presents to the /scim/v2 user/group provisioning
    # endpoints. Unset => the SCIM surface is disabled (403) and approval-role
    # resolution never consults the provisioned directory, so a non-SCIM
    # deployment is byte-for-byte unchanged. A machine-to-machine credential,
    # distinct from ``api_token``; the committed ``oidc_group_role_map`` still
    # owns the group->role mapping, so SCIM provisions membership, never roles.
    scim_bearer_token: str | None = None

    # --- OIDC API auth (behaviour: yaml; not secrets - public IdP metadata) ---
    # Optional, additive second credential path (issue #34): when issuer +
    # audience + jwks_uri are all set, token-gated endpoints also accept a valid
    # OIDC JWT bearer token, alongside the static api_token. All three are
    # required together (partial config is rejected at load - fail-closed). The
    # algorithm allow-list defaults to RS256 (asymmetric only: no alg:none / HS
    # confusion); leeway is the clock-skew tolerance in seconds.
    oidc_issuer: str | None = None
    oidc_audience: str | None = None
    oidc_jwks_uri: str | None = None
    oidc_algorithms: tuple[str, ...] = ("RS256",)
    oidc_leeway_seconds: int = 60
    # IdP-group -> approver-role mapping (issue #34). When an approval is
    # authenticated via OIDC, the approver *identity* is read from the verified
    # subject_claim (falling back to ``sub`` when absent), never the request
    # body, and roles are the union of the committed ``approvers`` grant for that
    # identity and the roles mapped from the verified group_claim through
    # ``oidc_group_role_map`` ({group -> roles}). All committed config; only the
    # cryptographically-verified claims come from the token (invariant #5).
    # Empty map => no group-derived roles (group-claim binding still applies the
    # verified identity, just grants no extra roles).
    oidc_subject_claim: str = "email"
    oidc_group_claim: str = "groups"
    oidc_group_role_map: tuple[tuple[str, tuple[str, ...]], ...] = ()
    # Multi-tenancy (issue #156): the verified OIDC claim that names the caller's
    # org. When set and present on the token, every read/write in that request is
    # isolated to that org; absent (the default), the request operates in the
    # single default org, so a single-tenant deployment is unchanged. The org is
    # taken only from the cryptographically-verified token, never the request
    # payload (invariant #5).
    oidc_org_claim: str | None = None

    # --- OIDC browser login / SSO for the dashboard (issue #34) ---
    # Authorization-code-with-PKCE login so a browser user signs in with the IdP
    # instead of pasting a token. It builds on the bearer OIDC config above: the
    # same issuer/jwks verify the id_token (audience = client_id). The non-secret
    # parts (client_id, endpoints, redirect_uri) live in YAML and are
    # all-or-nothing; the client secret and the session-cookie signing secret are
    # env-only credentials. Login is wired only when every part - including the
    # secrets - is present; otherwise the login routes 403 and the dashboard
    # falls back to the pasted-token UX.
    oidc_client_id: str | None = None
    oidc_client_secret: str | None = None  # secret: env only
    oidc_authorization_endpoint: str | None = None
    oidc_token_endpoint: str | None = None
    oidc_redirect_uri: str | None = None
    oidc_scopes: tuple[str, ...] = ("openid", "email")
    oidc_session_ttl_seconds: int = 8 * 60 * 60
    # Sliding-session refresh (issue #34). When set, session_ttl_seconds becomes
    # the *idle* timeout and this is the absolute cap: each authenticated request
    # slides the session cookie forward, but never past the original login + this
    # value, so total session age is bounded and a real re-login is forced
    # periodically. None (default) => sliding off, the cookie keeps its fixed TTL
    # (byte-for-byte the prior behaviour). Must be >= session_ttl_seconds.
    oidc_session_max_lifetime_seconds: int | None = None
    # Mark the login/session cookies Secure (HTTPS-only). Defaults on; set false
    # only for a local plain-HTTP deployment.
    oidc_cookie_secure: bool = True
    session_secret: str | None = None  # secret: env only
    # RP-Initiated Logout 1.0 (issue #34): when set, /dashboard/logout deletes the
    # local session cookie *and* redirects on to the IdP's end-session endpoint so
    # the SSO session is terminated too (not just the local cookie). Optional even
    # when browser login is configured; unset => logout returns to /dashboard
    # byte-for-byte as before. post_logout_redirect_uri (where the IdP returns the
    # browser after logout) must be pre-registered with the IdP and is only
    # meaningful with end_session_endpoint set.
    oidc_end_session_endpoint: str | None = None
    oidc_post_logout_redirect_uri: str | None = None

    # --- webhook replay protection (behaviour: yaml) ---
    # How long a processed delivery id is remembered in foundry_webhook_deliveries
    # for durable, cross-worker dedup. Rows older than this are pruned so the
    # table stays bounded. None disables pruning (unbounded growth - not advised).
    webhook_dedup_ttl_seconds: int | None = 86_400
    # Maximum age of an inbound delivery, validated against a provider-supplied
    # timestamp (Linear's webhookTimestamp). None = disabled. When set it is
    # fail-closed (missing/stale timestamp rejected) and must be <= the dedup
    # TTL, so a delivery can't age out of the dedup table while still replayable.
    webhook_replay_max_age_seconds: int | None = None

    # --- API rate limiting (behaviour: yaml; operational env overrides) ---
    # Coarse per-client request caps on the network surfaces. Enabled by
    # default with generous limits; set rate_limit_enabled: false to turn off.
    # Two buckets so a flood on one surface can't starve the other:
    # webhooks (provider deliveries can be bursty) and the API (human/automation).
    # Per-process, fixed-window; see api/ratelimit.py for the scope caveats.
    rate_limit_enabled: bool = True
    rate_limit_webhook_per_minute: int = 120
    rate_limit_api_per_minute: int = 60

    # --- dashboard / fleet (behaviour: yaml) ---
    # Approval-queue SLA: how long (seconds) a run may sit parked on a human
    # before it is flagged as breaching. Surfaced read-only on the fleet strip
    # and the GET /metrics/approvals queue; it changes no gate and blocks no
    # run. None (default) = no SLA configured, no breach signal (the historical
    # behaviour, byte-for-byte).
    approval_sla_seconds: int | None = None

    # Execution SLA: how long (seconds) a run may sit dispatched to an agent
    # (AGENT_RUNNING, no PR opened yet) before it is flagged as breaching - the
    # hung/runaway-agent signal, the machine-state complement to
    # ``approval_sla_seconds``. Surfaced read-only on the fleet strip and the
    # GET /metrics/executions queue; it changes no gate and blocks no run. None
    # (default) = no SLA configured, no breach signal (the historical behaviour,
    # byte-for-byte).
    execution_sla_seconds: int | None = None

    # Execution *cost* SLA: how many dollars an in-flight agent run (AGENT_RUNNING)
    # may spend before it is flagged - the spend twin of ``execution_sla_seconds``,
    # the "this agent has burned over $N and is still running" signal. Fired purely
    # for human attention on the fleet strip and the GET /metrics/executions queue
    # *before* the hard ``policy.max_cost_per_run`` budget cap (which blocks the
    # run); it changes no gate and blocks no run. None (default) = no cost SLA
    # configured, no breach signal (the historical behaviour, byte-for-byte).
    execution_cost_sla_usd: float | None = None

    # Review SLA: how long (seconds) a run may sit at an open PR (PR_OPEN,
    # awaiting review/CI) before it is flagged as breaching - the review-latency
    # signal ("PRs sitting unreviewed for N hours"), the review-side complement
    # to ``approval_sla_seconds`` / ``execution_sla_seconds``. The product
    # deliberately stops at a reviewed PR, so this is read-only visibility on the
    # fleet strip and the GET /metrics/reviews queue; it changes no gate and
    # blocks no run. None (default) = no SLA configured, no breach signal (the
    # historical behaviour, byte-for-byte).
    review_sla_seconds: int | None = None

    # Review *staleness* SLA: how long (seconds) an open PR may go with no observed
    # activity (no PR_OPENED/PR_UPDATED) before it is flagged stale - the "stale
    # since last push" signal, distinct from ``review_sla_seconds`` (total open
    # time). Separates an actively-pushed PR from an abandoned one. Surfaced
    # read-only on the fleet strip and the GET /metrics/reviews queue; it changes no
    # gate and blocks no run. None (default) = no SLA configured, no breach signal
    # (the historical behaviour, byte-for-byte).
    review_stale_sla_seconds: int | None = None

    # Compliance baseline the deployment is continuously checked against (issue
    # #31): a preset name (e.g. ``soc2``/``pci-dss``) or a path to another config
    # file. When set, ``GET /metrics/policy/check`` and the dashboard compliance
    # panel report, control by control, whether the live gate is at least as
    # strict as this baseline - the always-on, in-app twin of ``foundry-policy
    # check --against``. Read-only: it labels the gate, it changes and blocks
    # nothing. None (default) = no baseline, no check (byte-for-byte the historical
    # behaviour). An unresolvable baseline fails loud at app startup rather than
    # silently producing no signal (a compliance check must not quietly "pass").
    policy_baseline: str | None = None

    # --- issue tracker (behaviour: yaml) ---
    # "linear" (default), "github_issues" (the issue is the ticket; approvers
    # are then keyed by GitHub login instead of email), or "jira".
    tracker_provider: str = "linear"

    # --- coding agent (behaviour: yaml; tokens: env) ---
    # Which CodingAgentProvider receives approved work. See foundry.agents.
    # "auto" turns on learned dispatch (issue #33): the provider is picked per
    # run by the scorecard recommendation over ``agent_auto_candidates``.
    agent_provider: str = "manual"
    cursor_api_token: str | None = None
    claude_workflow_file: str = "foundry-claude-code.yml"
    agent_webhook_url: str | None = None
    agent_webhook_secret: str | None = None
    # Learned dispatch (agent.provider: auto, issue #33). The candidate agents
    # the scorecard may route between (each must be a real, credentialled
    # provider - built and validated fail-closed at startup); the fallback agent
    # used when no candidate has a majority-merged history yet; and the
    # min-sample floor the recommendation must clear before it routes. All
    # committed YAML - the routing decision never comes from request input.
    agent_auto_candidates: tuple[str, ...] = ()
    agent_auto_fallback: str = "manual"
    agent_auto_min_samples: int = 3

    # --- intelligence (behaviour: yaml) ---
    use_openai_analyzer: bool = False
    openai_model: str = "gpt-5.5"
    # Risk classification backend. "heuristic" is deterministic keywords/globs;
    # "llm" adds an escalate-only LLM pass with cited evidence on top of that
    # same heuristic floor (needs OPENAI_API_KEY).
    risk_provider: str = "heuristic"
    risk_model: str = "gpt-5.5"
    # Operator-supplied extra keywords for the ticket-text risk classifier
    # (issue #31): sensitive-area name -> additional keywords that flag that area
    # when they appear in a ticket's title/description. Merged *on top of* the
    # built-in keyword floor in engines/risk.py (never replacing it), so a
    # deployment can teach the heuristic its own domain vocabulary (e.g. "pan"
    # for payments, "member record" for customer_data) without forking. The
    # ticket-text twin of ``policy.sensitive_path_globs``. Strictly additive: it
    # can only ever flag *more* areas, never fewer, so it only escalates risk.
    # Area names are validated against SENSITIVE_AREA_KEYS at load (fail-closed).
    extra_sensitive_keywords: tuple[tuple[str, tuple[str, ...]], ...] = ()
    # Operator-defined *custom* risk categories beyond the fixed seven built-in
    # sensitive areas (issue #155). Each category has a slug name (validated so
    # it cannot collide with a built-in area), ticket-text ``keywords`` and/or
    # diff ``path_globs`` triggers, and the approval ``required_roles`` it
    # demands. Built-in areas stay a non-overridable floor: a custom category is
    # strictly additive / escalate-only - it can only ever *add* a required
    # approval, never drop a built-in's role or lower risk (invariant #1). The
    # demanded roles reach the gate via ``PolicyInput.repo.required_roles`` (the
    # resolved-roles field both the Python engine and ``foundry.rego`` already
    # read), so there is no new gate rule / Rego change (invariant #2). Validated
    # at load (fail-closed). Empty default = byte-for-byte the historical behaviour.
    custom_risk_categories: tuple[CustomRiskCategory, ...] = ()
    # Delivery planner backend. "template" renders deterministic
    # "Satisfy acceptance criterion: X" steps (the no-key default); "llm" adds
    # an LLM pass that produces file-level steps, test locations and verify
    # commands from the code-aware context (needs OPENAI_API_KEY, best paired
    # with context.provider: code). Safety guardrails stay deterministic and an
    # LLM failure degrades to the template plan.
    planner_provider: str = "template"
    planner_model: str = "gpt-5.5"
    # Epic decomposition backend (issue #35). "heuristic" is the deterministic
    # producer (explicit Repositories section, or >= 2 associated repos); "llm"
    # adds an inference pass that recovers epics described in prose, keeping the
    # heuristic decomposer as a non-overridable floor (it only ever *adds* a
    # split the heuristic missed, never removes one) and grounding every
    # proposed repo in the ticket text (needs OPENAI_API_KEY). Only consulted on
    # the epic-intake path (epics.auto_decompose, or an explicit epic intake).
    decomposition_provider: str = "heuristic"
    decomposition_model: str = "gpt-5.5"

    # --- policy / safety knobs (behaviour: yaml) ---
    # Policy backend: "local" (the in-process Python LocalPolicyEngine, default)
    # or "opa" (delegate to an OPA server running the foundry.rego bundle). Both
    # enforce the same rules - the Rego bundle is held in lock-step by
    # tests/test_policy_parity.py + scripts/policy_parity.py over shared vectors.
    policy_provider: str = "local"
    # OPA decision endpoint base URL (e.g. http://opa:8181). Required when
    # policy_provider == "opa".
    policy_opa_url: str | None = None
    # Live user-loadable policy bundle (issue #154): a path to a separately
    # authored bundle (same ``policy:``/``risk:`` knobs as this file) that is
    # merged **on top of** the resolved config at load as a strictly-additive
    # overlay - the base config + built-in gate rules remain a non-overridable
    # floor, so the bundle can only ever make the gate *stricter* (more protected
    # paths, more required approvals, higher confidence/approval thresholds,
    # tighter caps, narrower autonomous-retry triggers), never weaker. See
    # ``policy/overlay.py``. Loaded from a configured path, never request input
    # (invariant #5); changes only knob *values* both backends already read, so
    # there is no Python/Rego lock-step concern (invariant #2). None (default) =
    # no overlay, byte-for-byte the historical behaviour. A configured-but-missing
    # bundle, or one that would weaken the floor, fails loud at load.
    policy_bundle_path: str | None = None
    repo_confidence_threshold: int = 70
    max_files_changed: int = 12
    forbidden_globs: tuple[str, ...] = DEFAULT_FORBIDDEN_GLOBS
    # area name -> path globs; a PR touching these paths is treated as touching
    # that sensitive area even when the ticket text never mentioned it.
    sensitive_path_globs: tuple[tuple[str, tuple[str, ...]], ...] = (
        DEFAULT_SENSITIVE_PATH_GLOBS
    )
    # repo name -> extra forbidden path globs applied only to runs routed to
    # that repo, on top of the global ``forbidden_globs`` (issue #35, path-scoped
    # policy for monorepos). Strictly additive: a repo's globs can only *add*
    # protected subtrees, never drop a global one, so the sticky forbidden-path
    # block stays a one-way ratchet towards stricter.
    repo_forbidden_globs: tuple[tuple[str, tuple[str, ...]], ...] = ()
    # repo name -> extra approval roles required for any run routed to that repo,
    # on top of the roles the risk classifier derives (issue #31, per-repo policy
    # scoping / multi-role approval matrices). Strictly additive: a repo's roles
    # can only *add* a required approval, never drop a risk-derived one, so the
    # approval gate stays a one-way ratchet towards stricter (invariant #1). Role
    # names are validated against the ApprovalRole vocabulary at load.
    repo_required_roles: tuple[tuple[str, tuple[str, ...]], ...] = ()
    # Minimum number of *distinct* human approvers a run needs before it leaves
    # WAITING_APPROVAL for APPROVED - the "two-person rule" / N-of-M approval
    # matrix (issue #31). Default 1 = the historical single-approval lifecycle,
    # byte-for-byte. Enforced in the orchestrator lifecycle (approvals accumulate
    # and the run only advances once the count is met), like the orchestrator-only
    # forbidden-path block, so there is no policy-engine/Rego lock-step concern.
    min_approvals: int = 1
    # repo name -> minimum approver count for runs routed to that repo, on top of
    # the global ``min_approvals`` floor. Strictly additive: the effective minimum
    # is max(min_approvals, per-repo value), so a repo can only ever demand *more*
    # sign-offs, never fewer (invariant #1).
    repo_min_approvals: tuple[tuple[str, int], ...] = ()
    # path glob -> approval roles required when a PR's diff touches that path
    # (issue #31/#35, per-*path* policy scoping for monorepos). Unlike
    # ``repo_required_roles`` (resolved at intake from the routed repo, before any
    # diff exists), these are evaluated *diff-aware* on every PR push, in the same
    # orchestrator re-check as the sticky forbidden-path block and the
    # unflagged-sensitive-area escalation: a diff touching a configured path whose
    # role is not already covered by the run's approvers escalates the run to
    # REVIEW_REQUIRED for a human sign-off. Strictly additive - it can only ever
    # *escalate* a run to human review, never release one (invariant #1) - so the
    # default empty tuple is byte-for-byte the historical behaviour. Enforced in
    # the orchestrator lifecycle, like the forbidden-path block, so there is no
    # policy-engine/Rego lock-step concern (invariant #2 does not apply). Role
    # names are validated against the ApprovalRole vocabulary at load.
    path_required_roles: tuple[tuple[str, tuple[str, ...]], ...] = ()
    # Plan-scope drift escalation: the consumer of the LLM planner's
    # ``DeliveryPlan.expected_files_or_areas`` (the long-promised plan-vs-diff
    # check). When on (the default), a PR whose diff changes files outside
    # *every* file/area the approved plan declared escalates the run to
    # REVIEW_REQUIRED - the "agent strayed outside its approved scope" signal.
    # Enforced diff-aware in the orchestrator re-check, alongside the
    # forbidden-path block and the per-path approval-role escalation, so there
    # is no policy-engine/Rego lock-step concern (invariant #2 does not apply);
    # strictly additive (it can only ever *escalate* to human review, never
    # release a run - invariant #1). Inert unless the plan actually declares
    # expected files/areas (the template planner declares none), so for the
    # default template planner this changes nothing; set False to disable.
    enforce_plan_scope: bool = True
    # Plan out-of-scope escalation (issue #169, slice 1): the out-of-scope twin of
    # ``enforce_plan_scope``. When on (the default), a PR whose diff changes a
    # path/area the approved plan explicitly listed in ``out_of_scope`` (promised
    # *not* to touch) escalates the run to REVIEW_REQUIRED - a stronger off-plan
    # signal than mere scope drift. Same family as the drift check: enforced
    # diff-aware in the orchestrator re-check (no policy-engine/Rego concern,
    # invariant #2 does not apply), strictly additive (escalate-only, never
    # releases a run - invariant #1), and inert unless the plan actually declares
    # out-of-scope entries (the template planner declares none). Set False to
    # disable.
    enforce_plan_out_of_scope: bool = True
    # Change-freeze / maintenance windows (issue #31, the "time windows" policy
    # dimension). During an active window the orchestrator holds an *autonomous*
    # re-dispatch (a remediation retry) and escalates the run to REVIEW_REQUIRED
    # instead, so a human decides whether code may change during the freeze. Each
    # window is either recurring weekly (weekdays + start/end local time in an
    # IANA tz) or an absolute calendar range (starts_at/ends_at). Enforced in the
    # orchestrator lifecycle, like the per-path approval roles, so there is no
    # policy-engine/Rego lock-step concern (invariant #2 does not apply); strictly
    # additive (a freeze can only ever hold an action for a human, never release
    # one - invariant #1), so the default empty tuple is byte-for-byte the
    # historical behaviour. Windows are validated at load (real weekdays, a
    # resolvable tz, well-formed times/dates), fail-closed.
    change_freeze_windows: tuple[ChangeFreezeWindow, ...] = ()

    # --- remediation / feedback loop (behaviour: yaml) ---
    # When CI fails or a reviewer requests changes, Foundry can re-dispatch the
    # agent with the failure context - still through the policy gate, and never
    # more than max_agent_retries times per run.
    max_agent_retries: int = 2
    retry_on: tuple[str, ...] = ("ci_failed", "changes_requested")
    # Deny further agent dispatch once a run's spend reaches this many USD.
    # Enforced at first dispatch and every retry. None = no budget cap.
    max_cost_per_run: float | None = None
    # Fallback per-dispatch cost (USD) for providers that don't report spend
    # (claude_code / webhook / manual). Provider-reported cost still wins where
    # available; otherwise each dispatched attempt counts this estimate so the
    # cap can trip. 0 = no estimate (the cap then needs reported cost to bind).
    estimated_cost_per_dispatch: float = 0.0

    # --- triggers (behaviour: yaml) ---
    trigger_label: str = DEFAULT_TRIGGER_LABEL
    trigger_status: str = DEFAULT_TRIGGER_STATUS

    # --- approval (behaviour: yaml) ---
    # Who may approve runs. Role grants are configured per user, never asserted
    # by the API caller: (email, (role, ...)). A user with no roles can approve
    # ordinary work but cannot satisfy sensitive-area approval requirements.
    approvers: tuple[tuple[str, tuple[str, ...]], ...] = ()

    # --- compliance evidence packs (behaviour: yaml) ---
    # Which evidence sections satisfy which compliance control. Config, not
    # code: override wholesale via ``compliance.control_mappings``. Section
    # names are validated against the fixed evidence vocabulary at load time.
    compliance_control_mappings: tuple[ControlMapping, ...] = DEFAULT_CONTROL_MAPPINGS

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

    # --- epics / multi-repo decomposition (behaviour: yaml) ---
    # When a ticket spans several repositories (an explicit Repositories section,
    # or >= 2 associated repos), decompose it at intake into one independently
    # gated child run per repo, grouped under a parent epic run (issue #35).
    # Off by default: the deterministic producer is conservative, but
    # automatically fanning a single ticket out into several governed runs is a
    # behaviour change an operator opts into. A ticket that does not decompose is
    # unaffected - it runs as a single ordinary run either way.
    epics_auto_decompose: bool = False

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
        # A configured user policy bundle (issue #154) is merged on top of the
        # resolved config as a strictly-additive overlay before validation, so the
        # merged result is what gets validated and what the gate runs against. The
        # overlay can only ever make the gate stricter (the base remains a
        # non-overridable floor); see policy/overlay.py.
        if settings.policy_bundle_path:
            from foundry.policy.overlay import apply_policy_bundle

            settings = apply_policy_bundle(settings)
        settings._validate()
        return settings

    def _validate(self) -> None:
        if self.policy_provider not in ("local", "opa"):
            raise ValueError(
                f"policy_provider must be 'local' or 'opa', got {self.policy_provider!r}"
            )
        if self.policy_provider == "opa" and not self.policy_opa_url:
            raise ValueError(
                "policy_opa_url is required when policy_provider is 'opa'"
            )
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
        # Learned dispatch (issue #33). The candidate/fallback *names* are
        # validated against the real provider set fail-closed at build time
        # (build_provider_registry); here we only enforce the shape so a
        # misconfigured auto deployment fails at load, not first dispatch.
        if self.agent_auto_min_samples < 1:
            raise ValueError(
                "agent.auto_min_samples must be >= 1, got "
                f"{self.agent_auto_min_samples}"
            )
        if self.agent_provider == "auto":
            if not self.agent_auto_candidates:
                raise ValueError(
                    "agent.provider=auto requires a non-empty agent.auto_candidates "
                    "list (the agents the scorecard may route between)"
                )
            if not self.agent_auto_fallback:
                raise ValueError(
                    "agent.provider=auto requires agent.auto_fallback (the agent "
                    "used when no candidate has earned a recommendation yet)"
                )
        unknown = set(self.retry_on) - {"ci_failed", "changes_requested"}
        if unknown:
            raise ValueError(f"unknown retry_on triggers: {sorted(unknown)}")
        if self.max_cost_per_run is not None and self.max_cost_per_run <= 0:
            raise ValueError(
                f"max_cost_per_run must be positive, got {self.max_cost_per_run}"
            )
        if (
            self.webhook_dedup_ttl_seconds is not None
            and self.webhook_dedup_ttl_seconds < 1
        ):
            raise ValueError(
                "webhook_dedup_ttl_seconds must be >= 1 (or null to disable "
                f"pruning), got {self.webhook_dedup_ttl_seconds}"
            )
        if self.webhook_replay_max_age_seconds is not None:
            if self.webhook_replay_max_age_seconds < 1:
                raise ValueError(
                    "webhook_replay_max_age_seconds must be >= 1 (or null to "
                    f"disable), got {self.webhook_replay_max_age_seconds}"
                )
            if (
                self.webhook_dedup_ttl_seconds is not None
                and self.webhook_replay_max_age_seconds
                > self.webhook_dedup_ttl_seconds
            ):
                raise ValueError(
                    "webhook_replay_max_age_seconds "
                    f"({self.webhook_replay_max_age_seconds}) must not exceed "
                    f"webhook_dedup_ttl_seconds ({self.webhook_dedup_ttl_seconds}): "
                    "a delivery could otherwise age out of the dedup table "
                    "while still inside the replay window"
                )
        if self.estimated_cost_per_dispatch < 0:
            raise ValueError(
                "estimated_cost_per_dispatch must be >= 0, got "
                f"{self.estimated_cost_per_dispatch}"
            )
        if self.webhook_org_secrets:
            # Fail-closed at load (deploy-time): a blank/reserved org, blank
            # secret, duplicate org, or a secret reused across orgs is rejected so
            # a delivery can never resolve to an ambiguous tenant (issue #34).
            from foundry.api.webhook_org import WebhookOrgSecrets

            WebhookOrgSecrets.from_pairs(self.webhook_org_secrets)
        if self.risk_provider not in ("heuristic", "llm"):
            raise ValueError(
                f"risk_provider must be 'heuristic' or 'llm', got {self.risk_provider!r}"
            )
        if self.planner_provider not in ("template", "llm"):
            raise ValueError(
                "planner_provider must be 'template' or 'llm', got "
                f"{self.planner_provider!r}"
            )
        if self.decomposition_provider not in ("heuristic", "llm"):
            raise ValueError(
                "decomposition_provider must be 'heuristic' or 'llm', got "
                f"{self.decomposition_provider!r}"
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
        for mapping in self.compliance_control_mappings:
            unknown_sections = set(mapping.evidence) - KNOWN_EVIDENCE_SECTIONS
            if unknown_sections:
                raise ValueError(
                    f"compliance control {mapping.control_id!r} references unknown "
                    f"evidence section(s): {sorted(unknown_sections)}; valid sections "
                    f"are {sorted(KNOWN_EVIDENCE_SECTIONS)}"
                )
        if self.rate_limit_webhook_per_minute < 1:
            raise ValueError(
                "rate_limit_webhook_per_minute must be >= 1, got "
                f"{self.rate_limit_webhook_per_minute} (use rate_limit_enabled: false to disable)"
            )
        if self.rate_limit_api_per_minute < 1:
            raise ValueError(
                "rate_limit_api_per_minute must be >= 1, got "
                f"{self.rate_limit_api_per_minute} (use rate_limit_enabled: false to disable)"
            )
        if self.artifact_encryption_key:
            # Fail closed at config load: a key that can't build a cipher
            # (cryptography missing, or invalid key material) is a deploy-time
            # error, not a first-write surprise.
            from foundry.db.encryption import build_cipher

            build_cipher(self.artifact_encryption_key)
        if self.approval_sla_seconds is not None and self.approval_sla_seconds < 1:
            raise ValueError(
                "approval_sla_seconds must be >= 1 when set, got "
                f"{self.approval_sla_seconds} (omit it to disable the SLA signal)"
            )
        if self.execution_sla_seconds is not None and self.execution_sla_seconds < 1:
            raise ValueError(
                "execution_sla_seconds must be >= 1 when set, got "
                f"{self.execution_sla_seconds} (omit it to disable the SLA signal)"
            )
        if (
            self.execution_cost_sla_usd is not None
            and self.execution_cost_sla_usd <= 0
        ):
            raise ValueError(
                "execution_cost_sla_usd must be > 0 when set, got "
                f"{self.execution_cost_sla_usd} (omit it to disable the cost-SLA signal)"
            )
        if self.review_sla_seconds is not None and self.review_sla_seconds < 1:
            raise ValueError(
                "review_sla_seconds must be >= 1 when set, got "
                f"{self.review_sla_seconds} (omit it to disable the SLA signal)"
            )
        if (
            self.review_stale_sla_seconds is not None
            and self.review_stale_sla_seconds < 1
        ):
            raise ValueError(
                "review_stale_sla_seconds must be >= 1 when set, got "
                f"{self.review_stale_sla_seconds} (omit it to disable the SLA signal)"
            )
        # OIDC is all-or-nothing: a partial config that looked enabled but
        # silently verified nothing would be a fail-open auth hole.
        oidc_parts = {
            "issuer": self.oidc_issuer,
            "audience": self.oidc_audience,
            "jwks_uri": self.oidc_jwks_uri,
        }
        set_parts = [name for name, value in oidc_parts.items() if value]
        if set_parts and len(set_parts) != len(oidc_parts):
            missing = sorted(name for name, value in oidc_parts.items() if not value)
            raise ValueError(
                "OIDC auth requires issuer, audience and jwks_uri together; "
                f"missing: {missing}"
            )
        if self.oidc_enabled and not self.oidc_algorithms:
            raise ValueError("oidc.algorithms must list at least one algorithm")
        if self.oidc_leeway_seconds < 0:
            raise ValueError(
                f"oidc.leeway_seconds must be >= 0, got {self.oidc_leeway_seconds}"
            )
        # Per-repo required approval roles must be real ApprovalRoles, validated
        # at load so a typo is a deploy-time error, not a silently-ignored rule
        # that would leave a repo less protected than the operator intended
        # (issue #31).
        if self.repo_required_roles:
            from foundry.schemas.common import ApprovalRole

            valid_roles = {r.value for r in ApprovalRole}
            for repo, roles in self.repo_required_roles:
                bad = [r for r in roles if r not in valid_roles]
                if bad:
                    raise ValueError(
                        f"policy.repo_required_roles repo {repo!r} lists unknown "
                        f"approval roles {bad}; valid roles are {sorted(valid_roles)}"
                    )
        # Per-path required approval roles must be real ApprovalRoles too, for the
        # same reason: a typo'd role on a path rule would silently leave a subtree
        # unprotected (issue #31/#35). Validated at load, fail-closed.
        if self.path_required_roles:
            from foundry.schemas.common import ApprovalRole

            valid_roles = {r.value for r in ApprovalRole}
            for glob, roles in self.path_required_roles:
                bad = [r for r in roles if r not in valid_roles]
                if bad:
                    raise ValueError(
                        f"policy.path_required_roles path {glob!r} lists unknown "
                        f"approval roles {bad}; valid roles are {sorted(valid_roles)}"
                    )
        # Extra risk keywords must be keyed on a real sensitive area (issue #31):
        # a typo'd area name would silently never flag anything, so the operator
        # would believe their domain vocabulary was wired in when it was not.
        # Validated at load, fail-closed - mirrors the path_required_roles check.
        if self.extra_sensitive_keywords:
            from foundry.schemas.common import SENSITIVE_AREA_KEYS

            valid_areas = set(SENSITIVE_AREA_KEYS)
            for area, _keywords in self.extra_sensitive_keywords:
                if area not in valid_areas:
                    raise ValueError(
                        f"risk.extra_sensitive_keywords lists unknown sensitive "
                        f"area {area!r}; valid areas are {sorted(valid_areas)}"
                    )
        # Operator-defined custom risk categories (issue #155): names must be
        # non-colliding slugs, roles real ApprovalRoles, each with >= 1 trigger.
        # Validated at load, fail-closed - a malformed category would otherwise
        # silently never escalate.
        if self.custom_risk_categories:
            validate_custom_categories(self.custom_risk_categories)
        # Change-freeze windows must be well-formed (issue #31): a real set of
        # weekdays + start/end OR an absolute range, a resolvable IANA tz. A
        # malformed window is a deploy-time error, not a silently-inert freeze
        # that an operator believes is protecting them.
        if self.change_freeze_windows:
            validate_windows(self.change_freeze_windows)
        # N-of-M approval counts must be at least one sign-off (issue #31). A
        # value below 1 would be a gate weakening - never silently allowed.
        if self.min_approvals < 1:
            raise ValueError(
                f"policy.min_approvals must be >= 1, got {self.min_approvals}"
            )
        for repo, count in self.repo_min_approvals:
            if count < 1:
                raise ValueError(
                    f"policy.repo_min_approvals repo {repo!r} must be >= 1, "
                    f"got {count}"
                )
        # Role names in the IdP-group map must be real ApprovalRoles, validated at
        # load time so a typo is a deploy-time error, not a silently-empty grant.
        if self.oidc_group_role_map:
            from foundry.schemas.common import ApprovalRole

            valid = {r.value for r in ApprovalRole}
            for group, roles in self.oidc_group_role_map:
                bad = [r for r in roles if r not in valid]
                if bad:
                    raise ValueError(
                        f"oidc.group_role_map group {group!r} lists unknown "
                        f"approval roles {bad}; valid roles are {sorted(valid)}"
                    )
            if not self.oidc_subject_claim:
                raise ValueError("oidc.subject_claim must be non-empty")
            if not self.oidc_group_claim:
                raise ValueError("oidc.group_claim must be non-empty")
        # Browser-login non-secret parts are all-or-nothing and require the
        # bearer OIDC config (the same verifier checks the id_token). A partial
        # config that looked enabled but silently disabled login would be a
        # confusing half-built feature - fail-closed at load. The client/session
        # secrets are env-only, so they are validated at app build, not here.
        login_parts = {
            "client_id": self.oidc_client_id,
            "authorization_endpoint": self.oidc_authorization_endpoint,
            "token_endpoint": self.oidc_token_endpoint,
            "redirect_uri": self.oidc_redirect_uri,
        }
        set_login = [name for name, value in login_parts.items() if value]
        if set_login:
            if len(set_login) != len(login_parts):
                missing = sorted(n for n, v in login_parts.items() if not v)
                raise ValueError(
                    "OIDC browser login requires client_id, "
                    "authorization_endpoint, token_endpoint and redirect_uri "
                    f"together; missing: {missing}"
                )
            if not self.oidc_enabled:
                raise ValueError(
                    "OIDC browser login requires the OIDC bearer config "
                    "(issuer, audience, jwks_uri) to be set as well"
                )
            if not self.oidc_scopes:
                raise ValueError("oidc.scopes must list at least one scope")
            if self.oidc_session_ttl_seconds <= 0:
                raise ValueError(
                    "oidc.session_ttl_seconds must be > 0, got "
                    f"{self.oidc_session_ttl_seconds}"
                )
            if self.oidc_session_max_lifetime_seconds is not None:
                if self.oidc_session_max_lifetime_seconds <= 0:
                    raise ValueError(
                        "oidc.session_max_lifetime_seconds must be > 0, got "
                        f"{self.oidc_session_max_lifetime_seconds}"
                    )
                if (
                    self.oidc_session_max_lifetime_seconds
                    < self.oidc_session_ttl_seconds
                ):
                    raise ValueError(
                        "oidc.session_max_lifetime_seconds (the absolute cap) "
                        "must be >= session_ttl_seconds (the idle timeout), got "
                        f"{self.oidc_session_max_lifetime_seconds} < "
                        f"{self.oidc_session_ttl_seconds}"
                    )
        # RP-initiated logout is consumed only by the logout route, which requires
        # the browser-login config; an end_session_endpoint without it would be
        # silently inert. Fail-closed at load rather than ship a half-built logout.
        if self.oidc_end_session_endpoint and not set_login:
            raise ValueError(
                "OIDC RP-initiated logout (end_session_endpoint) requires the "
                "browser-login config (client_id, authorization_endpoint, "
                "token_endpoint, redirect_uri) to be set as well"
            )
        if self.oidc_post_logout_redirect_uri and not self.oidc_end_session_endpoint:
            raise ValueError(
                "oidc.post_logout_redirect_uri is only meaningful with "
                "end_session_endpoint set"
            )
        # Sliding-session refresh is consumed only by the browser-login session
        # path; a cap without that config would be silently inert. Fail-closed at
        # load rather than ship a knob that does nothing.
        if self.oidc_session_max_lifetime_seconds is not None and not set_login:
            raise ValueError(
                "oidc.session_max_lifetime_seconds requires the browser-login "
                "config (client_id, authorization_endpoint, token_endpoint, "
                "redirect_uri) to be set as well"
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
    def group_role_map(self) -> dict[str, tuple[str, ...]]:
        """IdP group name -> approver roles that membership grants."""
        return {group: roles for group, roles in self.oidc_group_role_map}

    @property
    def sensitive_globs_map(self) -> dict[str, tuple[str, ...]]:
        return {area: globs for area, globs in self.sensitive_path_globs}

    @property
    def extra_sensitive_keywords_map(self) -> dict[str, tuple[str, ...]]:
        """sensitive-area name -> extra ticket-text keywords for that area."""
        return {area: keywords for area, keywords in self.extra_sensitive_keywords}

    @property
    def repo_forbidden_map(self) -> dict[str, tuple[str, ...]]:
        """repo name -> extra forbidden globs scoped to that repo."""
        return {repo: globs for repo, globs in self.repo_forbidden_globs}

    @property
    def repo_required_roles_map(self) -> dict[str, tuple[str, ...]]:
        """repo name -> extra approval roles required for runs routed there."""
        return {repo: roles for repo, roles in self.repo_required_roles}

    @property
    def repo_min_approvals_map(self) -> dict[str, int]:
        """repo name -> minimum distinct approver count for runs routed there."""
        return {repo: count for repo, count in self.repo_min_approvals}

    @property
    def path_required_roles_map(self) -> dict[str, tuple[str, ...]]:
        """path glob -> approval roles required when a PR's diff touches it."""
        return {glob: roles for glob, roles in self.path_required_roles}

    @property
    def oidc_enabled(self) -> bool:
        """True when OIDC bearer auth is fully configured (all three parts)."""
        return bool(self.oidc_issuer and self.oidc_audience and self.oidc_jwks_uri)

    @property
    def scim_enabled(self) -> bool:
        """True when the SCIM provisioning surface is enabled (issue #157)."""
        return bool(self.scim_bearer_token)

    @property
    def oidc_login_configured(self) -> bool:
        """True when the non-secret browser-login parts are all set.

        The env-only secrets (client secret, session secret) are validated at app
        build, where missing ones fail loud; this property gates whether to even
        attempt wiring the login routes.
        """
        return bool(
            self.oidc_enabled
            and self.oidc_client_id
            and self.oidc_authorization_endpoint
            and self.oidc_token_endpoint
            and self.oidc_redirect_uri
        )


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
    if "extra_sensitive_keywords" in risk:
        out["extra_sensitive_keywords"] = tuple(
            (str(area), tuple(str(kw) for kw in keywords))
            for area, keywords in (risk["extra_sensitive_keywords"] or {}).items()
        )
    if "custom_risk_categories" in risk:
        out["custom_risk_categories"] = tuple(
            custom_category_from_mapping(name, spec)
            for name, spec in (risk["custom_risk_categories"] or {}).items()
        )

    planner = data.get("planner", {}) or {}
    if "provider" in planner:
        out["planner_provider"] = planner["provider"]
    if "model" in planner:
        out["planner_model"] = planner["model"]

    decomposition = data.get("decomposition", {}) or {}
    if "provider" in decomposition:
        out["decomposition_provider"] = decomposition["provider"]
    if "model" in decomposition:
        out["decomposition_model"] = decomposition["model"]

    agent = data.get("agent", {}) or {}
    if "provider" in agent:
        out["agent_provider"] = agent["provider"]
    if "claude_workflow_file" in agent:
        out["claude_workflow_file"] = agent["claude_workflow_file"]
    if "auto_candidates" in agent:
        out["agent_auto_candidates"] = tuple(agent["auto_candidates"] or ())
    if "auto_fallback" in agent:
        out["agent_auto_fallback"] = agent["auto_fallback"]
    if "auto_min_samples" in agent:
        out["agent_auto_min_samples"] = int(agent["auto_min_samples"])

    tracker = data.get("tracker", {}) or {}
    if "provider" in tracker:
        out["tracker_provider"] = tracker["provider"]
    if "jira_base_url" in tracker:
        out["jira_base_url"] = tracker["jira_base_url"]
    if "jira_allow_query_token" in tracker:
        out["jira_allow_query_token"] = bool(tracker["jira_allow_query_token"])

    policy = data.get("policy", {}) or {}
    if "provider" in policy:
        out["policy_provider"] = policy["provider"]
    if "opa_url" in policy:
        out["policy_opa_url"] = policy["opa_url"]
    if "bundle_path" in policy:
        raw_bundle = policy["bundle_path"]
        out["policy_bundle_path"] = None if raw_bundle is None else str(raw_bundle)
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
    if "repo_forbidden_globs" in policy:
        out["repo_forbidden_globs"] = tuple(
            (str(repo), tuple(globs))
            for repo, globs in (policy["repo_forbidden_globs"] or {}).items()
        )
    if "repo_required_roles" in policy:
        out["repo_required_roles"] = tuple(
            (str(repo), tuple(str(role) for role in roles))
            for repo, roles in (policy["repo_required_roles"] or {}).items()
        )
    if "min_approvals" in policy:
        out["min_approvals"] = int(policy["min_approvals"])
    if "repo_min_approvals" in policy:
        out["repo_min_approvals"] = tuple(
            (str(repo), int(count))
            for repo, count in (policy["repo_min_approvals"] or {}).items()
        )
    if "path_required_roles" in policy:
        out["path_required_roles"] = tuple(
            (str(glob), tuple(str(role) for role in roles))
            for glob, roles in (policy["path_required_roles"] or {}).items()
        )
    if "enforce_plan_scope" in policy:
        out["enforce_plan_scope"] = bool(policy["enforce_plan_scope"])
    if "enforce_plan_out_of_scope" in policy:
        out["enforce_plan_out_of_scope"] = bool(policy["enforce_plan_out_of_scope"])
    if "change_freeze_windows" in policy:
        out["change_freeze_windows"] = tuple(
            window_from_mapping(entry)
            for entry in (policy["change_freeze_windows"] or [])
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
    if "estimated_cost_per_dispatch" in budget:
        out["estimated_cost_per_dispatch"] = float(
            budget["estimated_cost_per_dispatch"]
        )

    webhook = data.get("webhook", {}) or {}
    if "dedup_ttl_seconds" in webhook:
        raw_ttl = webhook["dedup_ttl_seconds"]
        out["webhook_dedup_ttl_seconds"] = None if raw_ttl is None else int(raw_ttl)
    if "replay_max_age_seconds" in webhook:
        raw_age = webhook["replay_max_age_seconds"]
        out["webhook_replay_max_age_seconds"] = (
            None if raw_age is None else int(raw_age)
        )

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

    compliance = data.get("compliance", {}) or {}
    if "control_mappings" in compliance:
        out["compliance_control_mappings"] = mappings_from_config(
            compliance["control_mappings"]
        )

    memory = data.get("memory", {}) or {}
    if "priors_enabled" in memory:
        out["memory_priors_enabled"] = _bool(memory["priors_enabled"], default=True)
    if "min_samples" in memory:
        out["memory_min_samples"] = int(memory["min_samples"])
    if "confidence_cap" in memory:
        out["memory_confidence_cap"] = int(memory["confidence_cap"])

    rate_limit = data.get("rate_limit", {}) or {}
    if "enabled" in rate_limit:
        out["rate_limit_enabled"] = _bool(rate_limit["enabled"], default=True)
    if "webhook_per_minute" in rate_limit:
        out["rate_limit_webhook_per_minute"] = int(rate_limit["webhook_per_minute"])
    if "api_per_minute" in rate_limit:
        out["rate_limit_api_per_minute"] = int(rate_limit["api_per_minute"])

    dashboard = data.get("dashboard", {}) or {}
    if "approval_sla_seconds" in dashboard:
        value = dashboard["approval_sla_seconds"]
        out["approval_sla_seconds"] = None if value is None else int(value)
    if "execution_sla_seconds" in dashboard:
        value = dashboard["execution_sla_seconds"]
        out["execution_sla_seconds"] = None if value is None else int(value)
    if "execution_cost_sla_usd" in dashboard:
        value = dashboard["execution_cost_sla_usd"]
        out["execution_cost_sla_usd"] = None if value is None else float(value)
    if "review_sla_seconds" in dashboard:
        value = dashboard["review_sla_seconds"]
        out["review_sla_seconds"] = None if value is None else int(value)
    if "review_stale_sla_seconds" in dashboard:
        value = dashboard["review_stale_sla_seconds"]
        out["review_stale_sla_seconds"] = None if value is None else int(value)
    if "policy_baseline" in dashboard:
        value = dashboard["policy_baseline"]
        out["policy_baseline"] = None if value is None else str(value)

    notifications = data.get("notifications", {}) or {}
    if "slack_channel" in notifications:
        out["slack_channel"] = notifications["slack_channel"]

    epics = data.get("epics", {}) or {}
    if "auto_decompose" in epics:
        out["epics_auto_decompose"] = _bool(epics["auto_decompose"])

    auth = data.get("auth", {}) or {}
    oidc = auth.get("oidc", {}) or {}
    if "issuer" in oidc:
        out["oidc_issuer"] = oidc["issuer"]
    if "audience" in oidc:
        out["oidc_audience"] = oidc["audience"]
    if "jwks_uri" in oidc:
        out["oidc_jwks_uri"] = oidc["jwks_uri"]
    if "algorithms" in oidc:
        out["oidc_algorithms"] = tuple(oidc["algorithms"] or [])
    if "leeway_seconds" in oidc:
        out["oidc_leeway_seconds"] = int(oidc["leeway_seconds"])
    if "subject_claim" in oidc:
        out["oidc_subject_claim"] = str(oidc["subject_claim"])
    if "group_claim" in oidc:
        out["oidc_group_claim"] = str(oidc["group_claim"])
    if "org_claim" in oidc:
        out["oidc_org_claim"] = str(oidc["org_claim"]) or None
    if "group_role_map" in oidc:
        out["oidc_group_role_map"] = tuple(
            (str(group), tuple(str(r) for r in (roles or [])))
            for group, roles in (oidc["group_role_map"] or {}).items()
        )
    if "client_id" in oidc:
        out["oidc_client_id"] = str(oidc["client_id"])
    if "authorization_endpoint" in oidc:
        out["oidc_authorization_endpoint"] = str(oidc["authorization_endpoint"])
    if "token_endpoint" in oidc:
        out["oidc_token_endpoint"] = str(oidc["token_endpoint"])
    if "redirect_uri" in oidc:
        out["oidc_redirect_uri"] = str(oidc["redirect_uri"])
    if "scopes" in oidc:
        out["oidc_scopes"] = tuple(str(s) for s in (oidc["scopes"] or []))
    if "session_ttl_seconds" in oidc:
        out["oidc_session_ttl_seconds"] = int(oidc["session_ttl_seconds"])
    if "session_max_lifetime_seconds" in oidc:
        out["oidc_session_max_lifetime_seconds"] = int(
            oidc["session_max_lifetime_seconds"]
        )
    if "cookie_secure" in oidc:
        out["oidc_cookie_secure"] = _bool(oidc["cookie_secure"])
    if "end_session_endpoint" in oidc:
        out["oidc_end_session_endpoint"] = str(oidc["end_session_endpoint"])
    if "post_logout_redirect_uri" in oidc:
        out["oidc_post_logout_redirect_uri"] = str(oidc["post_logout_redirect_uri"])

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
        "FOUNDRY_ARTIFACT_ENCRYPTION_KEY": "artifact_encryption_key",
        "FOUNDRY_SCIM_BEARER_TOKEN": "scim_bearer_token",
        "FOUNDRY_AGENT_PROVIDER": "agent_provider",
        "FOUNDRY_AGENT_AUTO_FALLBACK": "agent_auto_fallback",
        "FOUNDRY_TRACKER_PROVIDER": "tracker_provider",
        "FOUNDRY_CURSOR_API_TOKEN": "cursor_api_token",
        "FOUNDRY_AGENT_WEBHOOK_URL": "agent_webhook_url",
        "FOUNDRY_AGENT_WEBHOOK_SECRET": "agent_webhook_secret",
        "FOUNDRY_OPENAI_MODEL": "openai_model",
        "FOUNDRY_RISK_PROVIDER": "risk_provider",
        "FOUNDRY_RISK_MODEL": "risk_model",
        "FOUNDRY_PLANNER_PROVIDER": "planner_provider",
        "FOUNDRY_PLANNER_MODEL": "planner_model",
        "FOUNDRY_DECOMPOSITION_PROVIDER": "decomposition_provider",
        "FOUNDRY_DECOMPOSITION_MODEL": "decomposition_model",
        "TEMPORAL_ADDRESS": "temporal_address",
        "FOUNDRY_TASK_QUEUE": "task_queue",
        "FOUNDRY_CONTEXT_PROVIDER": "context_provider",
        "FOUNDRY_CONTEXT_ORG": "context_org",
        "FOUNDRY_POLICY_PROVIDER": "policy_provider",
        "FOUNDRY_POLICY_OPA_URL": "policy_opa_url",
        "FOUNDRY_POLICY_BUNDLE_PATH": "policy_bundle_path",
        "FOUNDRY_OIDC_ISSUER": "oidc_issuer",
        "FOUNDRY_OIDC_AUDIENCE": "oidc_audience",
        "FOUNDRY_OIDC_JWKS_URI": "oidc_jwks_uri",
        "FOUNDRY_OIDC_SUBJECT_CLAIM": "oidc_subject_claim",
        "FOUNDRY_OIDC_GROUP_CLAIM": "oidc_group_claim",
        "FOUNDRY_OIDC_ORG_CLAIM": "oidc_org_claim",
        "FOUNDRY_OIDC_CLIENT_ID": "oidc_client_id",
        "FOUNDRY_OIDC_CLIENT_SECRET": "oidc_client_secret",
        "FOUNDRY_OIDC_AUTHORIZATION_ENDPOINT": "oidc_authorization_endpoint",
        "FOUNDRY_OIDC_TOKEN_ENDPOINT": "oidc_token_endpoint",
        "FOUNDRY_OIDC_REDIRECT_URI": "oidc_redirect_uri",
        "FOUNDRY_OIDC_END_SESSION_ENDPOINT": "oidc_end_session_endpoint",
        "FOUNDRY_OIDC_POST_LOGOUT_REDIRECT_URI": "oidc_post_logout_redirect_uri",
        "FOUNDRY_SESSION_SECRET": "session_secret",
    }
    for env_key, field_name in mapping.items():
        if env_key in env:
            out[field_name] = env[env_key]
    if "FOUNDRY_OIDC_SCOPES" in env:
        out["oidc_scopes"] = tuple(
            part.strip()
            for part in env["FOUNDRY_OIDC_SCOPES"].split(",")
            if part.strip()
        )
    if "FOUNDRY_WEBHOOK_ORG_SECRETS" in env:
        out["webhook_org_secrets"] = _parse_org_secret_pairs(
            env["FOUNDRY_WEBHOOK_ORG_SECRETS"]
        )
    if "FOUNDRY_OIDC_SESSION_TTL_SECONDS" in env:
        out["oidc_session_ttl_seconds"] = int(env["FOUNDRY_OIDC_SESSION_TTL_SECONDS"])
    if "FOUNDRY_OIDC_SESSION_MAX_LIFETIME_SECONDS" in env:
        out["oidc_session_max_lifetime_seconds"] = int(
            env["FOUNDRY_OIDC_SESSION_MAX_LIFETIME_SECONDS"]
        )
    if "FOUNDRY_OIDC_COOKIE_SECURE" in env:
        out["oidc_cookie_secure"] = _bool(env["FOUNDRY_OIDC_COOKIE_SECURE"])
    if "FOUNDRY_OIDC_ALGORITHMS" in env:
        out["oidc_algorithms"] = tuple(
            part.strip()
            for part in env["FOUNDRY_OIDC_ALGORITHMS"].split(",")
            if part.strip()
        )
    if "FOUNDRY_OIDC_LEEWAY_SECONDS" in env:
        out["oidc_leeway_seconds"] = int(env["FOUNDRY_OIDC_LEEWAY_SECONDS"])
    if "FOUNDRY_AGENT_AUTO_CANDIDATES" in env:
        out["agent_auto_candidates"] = tuple(
            part.strip()
            for part in env["FOUNDRY_AGENT_AUTO_CANDIDATES"].split(",")
            if part.strip()
        )
    if "FOUNDRY_AGENT_AUTO_MIN_SAMPLES" in env:
        out["agent_auto_min_samples"] = int(env["FOUNDRY_AGENT_AUTO_MIN_SAMPLES"])
    if "FOUNDRY_USE_OPENAI_ANALYZER" in env:
        out["use_openai_analyzer"] = _bool(env["FOUNDRY_USE_OPENAI_ANALYZER"])
    if "FOUNDRY_EPICS_AUTO_DECOMPOSE" in env:
        out["epics_auto_decompose"] = _bool(env["FOUNDRY_EPICS_AUTO_DECOMPOSE"])
    if "FOUNDRY_RATE_LIMIT_ENABLED" in env:
        out["rate_limit_enabled"] = _bool(env["FOUNDRY_RATE_LIMIT_ENABLED"])
    if "FOUNDRY_RATE_LIMIT_WEBHOOK_PER_MINUTE" in env:
        out["rate_limit_webhook_per_minute"] = int(
            env["FOUNDRY_RATE_LIMIT_WEBHOOK_PER_MINUTE"]
        )
    if "FOUNDRY_RATE_LIMIT_API_PER_MINUTE" in env:
        out["rate_limit_api_per_minute"] = int(env["FOUNDRY_RATE_LIMIT_API_PER_MINUTE"])
    if "FOUNDRY_APPROVAL_SLA_SECONDS" in env:
        raw = env["FOUNDRY_APPROVAL_SLA_SECONDS"].strip()
        out["approval_sla_seconds"] = None if raw == "" else int(raw)
    if "FOUNDRY_EXECUTION_SLA_SECONDS" in env:
        raw = env["FOUNDRY_EXECUTION_SLA_SECONDS"].strip()
        out["execution_sla_seconds"] = None if raw == "" else int(raw)
    if "FOUNDRY_EXECUTION_COST_SLA_USD" in env:
        raw = env["FOUNDRY_EXECUTION_COST_SLA_USD"].strip()
        out["execution_cost_sla_usd"] = None if raw == "" else float(raw)
    if "FOUNDRY_REVIEW_SLA_SECONDS" in env:
        raw = env["FOUNDRY_REVIEW_SLA_SECONDS"].strip()
        out["review_sla_seconds"] = None if raw == "" else int(raw)
    if "FOUNDRY_REVIEW_STALE_SLA_SECONDS" in env:
        raw = env["FOUNDRY_REVIEW_STALE_SLA_SECONDS"].strip()
        out["review_stale_sla_seconds"] = None if raw == "" else int(raw)
    if "FOUNDRY_POLICY_BASELINE" in env:
        raw = env["FOUNDRY_POLICY_BASELINE"].strip()
        out["policy_baseline"] = None if raw == "" else raw
    return out
