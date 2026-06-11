# Roadmap — the top 10 builds

Prioritized from a full code + market review (June 2026). The theme: the loop is
built; what's missing is **judgment** (the gates are keyword-dumb), **enterprise
readiness** (the buyer's checklist), and **the moat** (delivery memory, which no
competitor has). Strategy in one line: stop competing on the loop — GitHub Agent
HQ has one — and compete on judgment, neutrality, and learned routing.

## How agents should use this file

- Pick an item, set its status to `in progress` with your branch name, and work
  on a feature branch. One item (or one clearly-scoped slice of one) per PR.
- Update the status line and check off acceptance criteria **in the same PR** as
  the work. When all criteria are checked, mark it `done` with the PR number.
- Respect the invariants in `AGENTS.md` (never weaken a gate, Python policy +
  Rego in lock-step, no network in core tests, default-deny). Feature changes
  must also update `AGENTS.md` per its maintenance rule.
- If you split an item, add sub-bullets under it rather than new top-level items.
  New top-level items need a human decision, not an agent's.

Status values: `not started` | `in progress (branch, who/what)` | `blocked (why)` | `done (PR #)`

---

## 1. Code-aware context engine

**Status:** done (merged as commit `2fc27ba`)

The highest-leverage build: it upgrades routing, risk, planning, and the policy
gate at once. At enrichment time, gather real code facts for candidate repos —
file tree, test layout, CODEOWNERS, dependency manifests, conventions — via
shallow clone or the SCM tree API. Today the catalog is metadata only (topics,
README head, dir names, PR titles); routing is lexical token overlap and the
README's "which files, what tests" claim is unmet. Plugs into the existing
`context.provider` seam (`engines/enrichment.py`, `catalog/`).

- [x] New context provider that returns file tree, test layout, CODEOWNERS, and manifests for candidate repos (`context.provider: code`, `engines/code_context.py`)
- [x] Offline tests via recorded tree fixtures (no network, per AGENTS.md)
- [x] Enrichment confidence can cite code-level evidence in its reason string
- [x] Catalog sync stays budget-aware and resumable (per-repo call reservation; code facts default off)
- [x] `foundry.example.yaml` + README + AGENTS.md updated

## 2. LLM-backed risk classification with cited evidence

**Status:** done (`claude/roadmap-research-plan-3bu7vc`, Claude Code)

Replace `_SENSITIVE_KEYWORDS` substring matching (`engines/risk.py`) with a
model pass that writes its reasoning into the audit trail ("touches session
issuance in `auth/tokens.py`"). The keyword heuristic stays as a deterministic
floor: the LLM may only escalate risk, never downgrade it. The gate is the
product; its judgment must be better than `"stripe" in text`.

- [x] `LlmRiskClassifier` behind the existing engine seam, mirroring the `OpenAITicketAnalyzer` pattern (structured output, validation retry, fake for tests)
- [x] Heuristic floor enforced: combined risk = max(heuristic, LLM)
- [x] Risk artifacts carry evidence strings into the audit trail and dashboard timeline (`RiskAssessment.evidence`, `risk.escalated` metadata)
- [x] Works on both ticket-stage and diff-stage classification (`LlmRiskClassifier`, `LlmDiffRiskClassifier` behind the new `DiffRiskClassifier` seam)
- [x] Config: `risk.provider: heuristic | llm` with heuristic default

## 3. Real planning (file-level, convention-aware)

**Status:** not started

`engines/planner.py` renders "Satisfy acceptance criterion: X" boilerplate. Feed
item #1's context into an LLM planner so dispatched agents receive named files,
patterns to follow, test locations, and build/test commands. Directly cuts
retries and failed runs — and delivery memory can prove the improvement.

- [ ] `LlmPlanner` behind the planner seam (template planner remains the no-key default)
- [ ] Plans include file-level steps, test locations, and verify commands when code context is available
- [ ] `DeliveryPlan.expected_files_or_areas` populated, enabling a future plan-vs-diff gate
- [ ] Secret-leak guard still covers the enriched instruction payload

## 4. Programmable policy (policy-as-code for real)

**Status:** not started

Promote the Rego bundle from mirror to primary user-extensible interface:
per-repo/per-path rules, N-of-M approval matrices, role combinations, time
windows, custom risk categories. Ship a starter library (SOC 2,
change-management). Today policy is ~10 hardcoded checks in
`policy/engine.py`; buyers can't express their rules without forking. EU AI
Act Article 14 / NIST AI RMF make provable human oversight a compliance
requirement — this is the item regulated buyers choose on.

- [ ] User policy bundles loadable alongside the built-in rules (built-ins remain a non-overridable floor — never weaker, only stricter)
- [ ] Per-repo and per-path policy scoping
- [ ] N-of-M / multi-role approval matrices
- [ ] Starter policy library with tests
- [ ] Python engine and Rego stay in lock-step (or Rego becomes authoritative with the Python engine evaluating it)

## 5. Slack / Teams approvals

**Status:** not started

Approvers live in chat, not Linear. Interactive message with plan summary, risk,
diff stats; approve/reject with workspace identity, audited like every other
approval surface. Cheapest item here relative to daily friction removed. New
connector behind the existing transport seam; same fail-closed posture
(no signing secret = surface disabled).

- [ ] Slack connector: signed interactivity webhook, approve/reject actions, identity from workspace
- [ ] Approval recorded with the same role checks as Linear/REST (roles from config, never payload)
- [ ] Run status notifications (parked, blocked, PR open, merged)
- [ ] Fixture-pinned payload mapping tests

## 6. Agent scorecards + learned dispatch

**Status:** not started

The moat. Extend delivery memory (`memory/`) from "which repo" to "which
agent": per work-type and per-repo success rate, retry count, and cost for each
provider — then route dispatch on it ("Devin gets dependency bumps, Claude Code
gets feature work"). GitHub will never tell you Cursor outperforms Copilot on
your billing service; Foundry can, with receipts. Compounds with every run.

- [ ] Outcome rows already capture provider/cost/retries — aggregate into per-provider scorecards by work type and repo
- [ ] `GET /metrics/agents` + dashboard view
- [ ] Optional policy-gated routing: `agent.provider: auto` picks by scorecard, with min-sample floor and explicit-config override (mirror the priors design: bounded, audited reason strings, kill switch)
- [ ] `foundry-memory show-scorecards` CLI

## 7. Enterprise identity, tenancy, and hardening

**Status:** not started

The buyer's checklist. SSO/OIDC + SCIM; approver roles from IdP groups instead
of YAML emails; `org_id` + row-level security in `db/models.py` (currently
single-tenant, no tenant columns). Plus nearby criticals: DB-level idempotency
on intake (two simultaneous webhooks for one issue can race into
`intake_and_plan()`), rate limiting, encryption-at-rest for artifact payloads.

- [ ] Unique constraint: one active run per issue, enforced in the DB
- [ ] OIDC auth for API + dashboard; IdP-group → approver-role mapping
- [ ] `org_id` on all tables + row-level isolation
- [ ] Rate limiting on webhook and API surfaces
- [ ] Artifact payload encryption at rest

## 8. Epic decomposition and multi-repo runs

**Status:** not started

One run targets one repo today; the README's own motivating example (a
codebase-wide migration) can't be expressed. Build a parent plan that splits an
epic into governed child runs across repos — each with its own gate and
approval, rolled up into one timeline. Monorepo support via path-scoped
policies (pairs with item #4).

- [ ] Parent/child run model with rollup status in DB + timeline + dashboard
- [ ] Decomposition step produces child plans, each independently policy-gated and approved
- [ ] Path-scoped policy for monorepos
- [ ] Cross-run audit linkage (the epic's full chain is one export)

## 9. Compliance evidence packs

**Status:** not started

The audit trail already exists (content-hashed artifacts, append-only events);
this is packaging. One-click export of a run's full chain — ticket, plan,
approvals with identities, policy decisions, diff-risk checks — mapped to
SOC 2 / ISO 27001 / EU AI Act controls. Turns the log into a procurement
line-item.

- [ ] `GET /runs/{id}/evidence` export (JSON + rendered PDF/HTML)
- [ ] Control mappings (SOC 2 CC8.1 change management, ISO 27001 A.8, EU AI Act Art. 14) as config, not code
- [ ] Hash-chain verification included so an auditor can confirm integrity
- [ ] Org-wide export over a date range

## 10. Finish Temporal + fleet dashboard

**Status:** not started

Make the durability pitch true: the Temporal driver (`workflows/`) exists but is
unproven against a real server — crash-proof runs and days-long approval waits
are currently aspirational; the inline driver is the production path. Then
replace the zero-build HTML page with the screen a VP of Engineering buys:
every agent working across the org, pending approvals as a queue, live spend,
delivery metrics over time.

- [ ] Temporal driver tested against a real server (CI job with the Temporal profile from docker-compose)
- [ ] Activity retry policies, timeouts, and failure compensation made explicit
- [ ] Fleet dashboard: live run board, approval queue, spend, metrics trends
- [ ] Dashboard auth upgraded alongside item #7 (OIDC, not just bearer token)

---

**If only three get funded: #1, #2, #6** — context, risk, and scorecards. Those
make Foundry smarter than the platform-native control planes rather than a
smaller version of them, and #6 compounds: every run improves the product in a
way a competitor can't copy without your history.
