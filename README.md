# Project Foundry

Project Foundry is an **engineering control plane**. It is not the AI that writes
code — it is the system that decides what work is requested, whether there is
enough information, what context is needed, which system is affected, whether the
work is safe for an agent, which agent to use, what approvals are required, and
what happened afterwards.

## Module 1 — Ticket-to-PR

> **Promise:** Connect Linear and GitHub. Let approved AI agents turn well-formed
> tickets into reviewed PRs, safely.

The first Foundry loop:

```
Linear ticket
  → Foundry analyses & enriches it
  → Foundry creates a delivery plan
  → Human approves
  → Foundry launches a coding agent
  → Agent creates branch / PR
  → PR reviewed by CI + CodeRabbit + human as needed
  → Linear updated with status, summary, next action
```

The MVP **stops at a draft / ready-for-review PR**. Explicit non-goals: auto-merge,
auto production deploy, incident-to-fix automation, a custom developer portal,
multi-team marketplace, org-wide agent memory, and autonomous
migration/auth/payment/customer-data changes.

## What's in this repository (the testable core foundation)

This first increment is the governed, fully unit-tested core — **no live external
calls**. Live adapters (Temporal, LangGraph, OPA server, Linear/GitHub MCP, coding
agents) plug into these contracts later.

| Package | Responsibility |
| --- | --- |
| `foundry.schemas` | Pydantic contracts for every run artifact: `TicketAnalysis`, `ContextBundle`, `RiskAssessment`, `DeliveryPlan`, `PullRequestState`, coding-agent job I/O. |
| `foundry.policy`  | The policy gate — **hard rules, not prompts**. `LocalPolicyEngine` (pure-Python, default) mirrors `foundry.rego` for an OPA server. |
| `foundry.agents`  | The `CodingAgentProvider` abstraction (`ManualProvider`, `InMemoryFakeProvider`) + registry. Foundry is never built around one coding tool. |
| `foundry.db`      | SQLAlchemy data model: runs, versioned content-hashed artifacts, append-only audit events, policy decisions, agent jobs. |
| `foundry.audit`   | Content hashing + helpers to persist the run's verifiable trail. |
| `foundry.api`     | FastAPI skeleton: signed Linear webhook intake (idempotent), authorised approval commands, run-status endpoints. |

### Governance guarantees encoded here

- **Idempotent intake** — a redelivered webhook never creates a second run.
- **Authenticated webhooks** — bad signatures are rejected; no workflow starts.
- **Acceptance criteria required** — a ticket is not "buildable" without them, even
  if the LLM claims it is ready.
- **Repo confidence threshold** — work is blocked when no single repo clears 70.
- **MVP hard blocks** — production deploy and DB migrations cannot run autonomously.
- **Sensitive areas need approval** — auth/infra need engineering; customer-data/PII/
  payments need security. Draft-PR mode only for low/medium risk; never auto-merge.
- **No secrets to agents** — job inputs are scanned before dispatch.

## Architecture (target)

```
Linear ──webhook/MCP──▶ Foundry API
                          │
                          ▼
                    Temporal workflow         (crash-proof, long-running, waits)
                          │
                          ▼
              LangGraph / Agents SDK flow      (analysis, context, planning)
                          │
                          ▼
                 Ticket Intelligence Engine
                          │
                          ▼
                      Policy Gate (OPA)         ← foundry.policy mirrors this
                          │
                          ▼
                    Human approval (Linear)
                          │
                          ▼
              CodingAgentProvider adapter        ← foundry.agents
            (Cursor / Claude Code / OpenAI / manual)
                          │
                          ▼
                  GitHub branch + PR
                          │
                          ▼
              CI + CodeRabbit + human review
                          │
                          ▼
                  Foundry PR monitor ─▶ Linear update
```

## Getting started

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[test]"
pytest
```

Optional extras:

- `pip install -e ".[server]"` then `uvicorn foundry.api.app:create_app --factory`
  (provide a webhook secret).
- `pip install -e ".[workflow]"` for the Temporal / LangGraph adapters (added later).

### Policy

The Python `LocalPolicyEngine` is the default and runs with no external service.
The equivalent OPA bundle lives at `src/foundry/policy/foundry.rego`; run its tests
with the OPA CLI:

```bash
opa test src/foundry/policy
```

The two backends are held to the same behaviour (`tests/test_policy_engine.py`
mirrors `foundry_test.rego`).

## Project layout

```
src/foundry/
  schemas/   artifact contracts (+ shared enums in common.py)
  policy/    engine.py (Local + OPA), foundry.rego, foundry_test.rego
  agents/    provider.py, manual.py, registry.py
  db/        base.py, models.py
  audit/     events.py
  api/       app.py, security.py, store.py
tests/       one module per package
```
