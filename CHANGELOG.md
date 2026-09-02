# Changelog

All notable changes to Project Foundry are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
the project uses [Semantic Versioning](https://semver.org/). Sections for
released versions were reconstructed from the git history between tags; the
tags after `v1.0.0` are bare (`1.2.0`, not `v1.2.0`), which is why the release
pipeline now accepts both spellings.

## [Unreleased]

### Added
- `CHANGELOG.md` (this file).
- Explicit `[tool.ruff.lint]` rule selection (`E4`, `E7`, `E9`, `F`) so a ruff
  upgrade that changes the built-in default cannot silently widen the lint gate.
- A `[dev]` extra carrying the test dependencies plus the pinned ruff.
- Microsoft Teams approval surface, the unbuilt half of #32 (#174).
- LLM plan-satisfaction judge: PRs that do not satisfy the approved plan are
  escalated to a human (#169, #171); PRs touching plan out-of-scope paths (#170)
  and PRs that promise tests but ship none (#172) are escalated too.
- Multi-tenancy: `org_id` on every table with row-level isolation (#156),
  webhook intake mapped to a non-default org via per-org secrets (#167), and
  the org bound onto the dashboard SSO session cookie (#166).
- SCIM 2.0 user and group provisioning (#157).
- Artifact payload encryption at rest (#162) and `foundry-db reencrypt-artifacts`
  for legacy or rotated-away rows (#164).
- Operator-defined custom risk categories beyond the fixed areas (#160), with
  their roles surfaced in the approval prompt (#186).
- Live user policy bundles loaded as a strict-floor overlay (#154).
- `retry_on` compared by `foundry-policy check` (#153).
- Foundry now dogfoods itself: the `claude_code` runner workflow is installed
  in this repository (#182).

### Changed
- `GET /runs` and `GET /runs/{id}` are now token-gated like every other read.
  They returned approver and requester identities and agent spend to anonymous
  callers. The bundled dashboard already sent the bearer token to both, so it
  is unaffected. With no `FOUNDRY_API_TOKEN` and no OIDC configured the two
  endpoints are disabled outright (403), the same fail-closed posture as the
  rest of the API.
- The FastAPI application version is read from the installed distribution
  metadata instead of a hard-coded string (it had stayed at `1.1.0`).
- `pyproject.toml` version aligned with the newest tag (`1.6.0`).
- Ruff pinned to `0.15.22` in CI and in the `[dev]` extra. Ruff 0.16 changed
  the default rule set, which turned a clean tree into 413 findings overnight.
- The release workflow now triggers on both `v[0-9]*` and `[0-9]*` tags. Every
  tag after `v1.0.0` was bare, so no release had fired since 1.0.0.
- Claude Code runner workflows (`.github/workflows/foundry-claude-code.yml` and
  `examples/claude-code-runner.yml`) pass workflow inputs to shell steps via
  quoted environment variables instead of `${{ inputs.* }}` interpolation, and
  pin `anthropics/claude-code-base-action` to a commit SHA.
- Forbidden-path and escalate-only path gates are depth-agnostic for bare
  relative globs (#178, #180, #184).
- The LLM analyzer degrades to the deterministic floor on a non-object
  response (#176).

### Fixed
- `tzdata` is a core dependency. `zoneinfo.ZoneInfo()` failed on Windows and
  slim container images (which ship no system zone database), breaking the
  change-freeze window code and 20 tests. `UTC` is additionally served from
  `datetime.timezone.utc` so the default zone works with no database at all.
- A rejected OIDC bearer token was swallowed silently in the API auth path; it
  is now logged at warning level with the method and path (never the token).
- Stale docs claiming #154, #155 and #32 were still open (#168).

## [1.6.0] - 2026-06-17

A large release: the policy library, the compliance surface, SSO and the fleet
dashboard all landed between 1.4.0 and this tag.

### Added
- Starter policy library with presets (SOC 2, PCI-DSS and others) and the
  `foundry-policy` CLI: `explain`, `check` (with `--format json`) and config
  introspection (#95, #106, #110, #112, #113, #133). In-app compliance check via
  `GET /metrics/policy/check` and a dashboard panel (#31).
- Approval matrix: N-of-M distinct sign-offs (the two-person rule), per-repo
  and per-path required approval roles, mid-flow re-pinging of the next
  approver, and the required count surfaced in prompts (#94, #96, #97, #98,
  #101). The policy gate requires at least one recorded approval (#18) and
  refuses under-privileged approvals before recording them (#56).
- Change-freeze (maintenance) windows on the policy surface, shown on the
  policy views and in `foundry-policy explain` (#115, #117, #152).
- Risk-escalation knobs: operator-configurable ticket-text keywords and plan
  scope drift escalation (#116, #129, #150).
- OIDC bearer-token auth for the API, approvals bound to the verified token
  with IdP-group to role mapping, OIDC browser login (SSO) for the dashboard,
  sliding-session refresh and RP-initiated logout (#86, #89, #90, #104, #125).
- Compliance evidence packs: per-run export, org-wide date-range export,
  cross-run epic export, PDF rendering, the `foundry-evidence` CLI and a
  `verify` audit-integrity CI gate; a cross-row tamper-evident hash chain over
  the audit trail and `GET /metrics/integrity` (#58, #76, #79, #88, #91, #93,
  #132, #144).
- Epics: parent/child run model with rollup, deterministic and LLM-assisted
  decomposition, auto-decompose on intake, per-repo forbidden-path globs, an
  epic board in the dashboard (#78, #80, #82, #83, #84, #85).
- Fleet dashboard: live fleet strip, approval, execution, review and failure
  queues with SLA ages, failures by category, repo and work type with trends,
  delivery metrics by repo and work type with trends, approval and merge
  latency (median and p90), agent scorecards and trends, per-run spend (#67,
  #71, #99 to #109, #118 to #128, #131, #134 to #148). Offline CLI twins of the
  fleet, failure and delivery metrics (#119, #120, #121).
- Agent scorecards per provider, scorecard trends, a scorecard-backed
  recommendation engine and `agent.provider: auto` learned dispatch (#33, #68,
  #87, #92).
- Slack approval surface and outbound notifications (#64, #65).
- File-level LLM planner behind the planner seam (#59).
- Webhook replay protection: durable dedup and replay-age validation (#52).
- Rate limiting on the webhook and API surfaces (#34).
- C#/.NET 8 port of the Foundry core under `dotnet/` (#1).
- Temporal: explicit retry policies, timeouts and compensation; auto-fail on
  exhausted retries; the workflow proven against a real server in CI (#37,
  #81, #107).
- CI hardening: lint job, pinned OPA, reusable release gate, matrix and cache,
  concurrency, image smoke test (#25); Python/Rego policy parity is
  machine-verified and the OPA backend wired (#63).

### Changed
- One active run per issue is enforced with a partial unique index (#42).
- Run state transitions are locked so "blocked stays blocked" under
  concurrency (#10); stop, reject and mark-agent-failed are refused on
  terminal runs (#39).
- The budget cap is enforced at first dispatch and for cost-blind providers
  (#54).
- Jira approval surface hardened to a header-only token by default (#55).
- Roadmap moved from `ROADMAP.md` to GitHub Issues.

### Fixed
- OPA policy parity: the budget-cap message had diverged from the Python
  engine (#73).
- Docker Compose deployment boots on a fresh clone with shipped migrations and
  a single schema owner (#62); the offline demo works from a fresh clone on any
  OS.
- Temporal workflow holes (#60), engine correctness defects (#48), audit
  integrity gaps in agent dispatch (#13), connector mapping bugs (#47).
- PR file and check-run listings are paginated so forbidden paths cannot
  bypass the hard block (#38); GitLab MR diffs are fetched so file-based gates
  apply (#8).
- Only idempotent HTTP operations are retried (#11); OpenAI SDK failures are
  wrapped as `LLMError` so degrade-to-floor holds (#40).
- Catalog sync survives empty-repo 409s, scopes deletion to the org and retries
  degraded code-facts fetches (#41).
- Secret-leak scan coverage broadened (#16); the provider job is cancelled when
  a human stops a run (#28).

## [1.4.0] - 2026-06-11

### Added
- LLM-backed risk classification with cited evidence (#5).

## [1.3.0] - 2026-06-11

### Added
- Code-aware context engine.
- `AGENTS.md` for fast agent orientation and `ROADMAP.md` with the prioritised
  backlog.

## [1.2.0] - 2026-06-11

### Added
- Delivery memory: outcomes, routing priors and ROI metrics (#3).
- Repo catalog and catalog-backed context enrichment (#2).
- Releases can be dispatched from the Actions tab.

### Fixed
- README release badge no longer depends on the tag-only workflow.

## [1.0.0] - 2026-06-09

Initial public release: signed Linear and GitHub webhooks, the policy-gated
ticket-to-PR orchestrator, the pure-Python and Rego policy engines, the audit
trail, the coding-agent provider abstraction and the FastAPI skeleton.

[Unreleased]: https://github.com/JeremySNR/Project-foundry/compare/1.6.0...HEAD
[1.6.0]: https://github.com/JeremySNR/Project-foundry/compare/1.4.0...1.6.0
[1.4.0]: https://github.com/JeremySNR/Project-foundry/compare/1.3.0...1.4.0
[1.3.0]: https://github.com/JeremySNR/Project-foundry/compare/1.2.0...1.3.0
[1.2.0]: https://github.com/JeremySNR/Project-foundry/compare/v1.0.0...1.2.0
[1.0.0]: https://github.com/JeremySNR/Project-foundry/releases/tag/v1.0.0
