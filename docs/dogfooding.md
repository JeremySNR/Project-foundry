# Dogfooding: Foundry governing its own repository

Project-foundry is developed by AI coding agents working from GitHub Issues -
exactly the ticket-to-PR workflow Foundry exists to govern. Dogfooding closes
the loop: this repo's own tickets run through Foundry's own intake, risk
classification, policy gate, human approval, dispatch and audit trail. It is
the cheapest source of real-world evidence the offline test suite cannot
produce, and it feeds delivery memory with genuine outcomes.

Two ways to run it, smallest first.

## 1. One governed run, no deployment (`scripts/smoke_e2e_github.py`)

Drives a single real issue through the production code paths - the same ones
the webhooks use - from any machine with a repo-scoped GitHub token. No server,
no webhooks, no LLM keys; state lands in a local SQLite file.

```bash
export FOUNDRY_E2E=1
export FOUNDRY_CONFIG=foundry.dogfood.yaml
export FOUNDRY_GITHUB_API_TOKEN=ghp_...          # repo scope on this repository
export FOUNDRY_E2E_ISSUE_ID='JeremySNR/Project-foundry#<number>'
export FOUNDRY_E2E_APPROVER=<your-github-login>  # must be in approval.approvers
python scripts/smoke_e2e_github.py
```

What happens, visibly, on the real issue:

1. Foundry fetches the issue as the ticket and posts its **readiness analysis**
   as a comment (a ticket without acceptance criteria is bounced to
   `needs-clarification` with drafted criteria - fix the ticket and re-run).
2. Routing, risk classification and the delivery plan are produced, the run
   parks at `waiting_approval`, and the **approval prompt** is posted.
3. The named approver's sign-off is recorded (roles from config, never from
   the request - invariant #5) and the **policy gate** evaluates the dispatch.
4. The configured agent provider receives the governed job. With the
   committed `foundry.dogfood.yaml` that is `manual` - a recorded handoff,
   zero spend. The script ends by printing the run's full **audit timeline**.

Set `FOUNDRY_E2E_PR_TIMEOUT=600` to also poll for and correlate the agent's PR
(useful once a real provider is wired).

The script mutates the real issue (comments, `foundry:status:*` labels), so
point it at an issue you are happy to annotate.

## 2. Deployed: webhooks in, governed PRs out

A standing deployment makes every labelled issue a governed run and re-checks
every PR push. What it needs:

- **Host**: `docker compose up --build` (API + Postgres) on anything that can
  receive HTTPS from GitHub. For a laptop/dev box, a tunnel such as
  `smee.io` or `cloudflared` forwarding to `localhost:8000` works.
- **Config**: `FOUNDRY_CONFIG=foundry.dogfood.yaml`, plus env secrets:
  `FOUNDRY_GITHUB_API_TOKEN` (repo scope) and
  `FOUNDRY_GITHUB_WEBHOOK_SECRET` (any strong random string).
- **One GitHub webhook** on this repository pointing at
  `https://<host>/webhooks/github`, content type `application/json`, secret =
  `FOUNDRY_GITHUB_WEBHOOK_SECRET`, events: **Issues, Issue comments,
  Pull requests, Pull request reviews, Check suites**. The single endpoint
  handles both ticket intake and PR observation.

The loop is then label-driven:

- Label an issue **`foundry:candidate`** (or comment `/foundry analyse`) to
  trigger intake. Foundry posts the analysis and, when ready, the approval
  prompt; pipeline position is tracked as a `foundry:status:*` label.
- Approve with a **`/foundry approve`** comment from a login listed in
  `approval.approvers` (or `reject` / `stop`). Roles come from the committed
  config; the gate still applies.
- The dispatched agent's PR is picked up by the same webhook; the diff-aware
  gates (forbidden paths, sensitive areas, plan scope) re-run on every push,
  and CI failures trigger policy-gated, capped, budgeted retries.

## Turning on the real coding agent

The committed config dispatches to `manual` so dogfooding starts spend-free.
To let Claude Code do the work:

1. `.github/workflows/foundry-claude-code.yml` is already on the default
   branch (this repo doubles as the reference install of
   `examples/claude-code-runner.yml`).
2. Add an `ANTHROPIC_API_KEY` secret to this repository - the key lives in
   repo secrets; Foundry never holds it.
3. Set `agent.provider: claude_code` in the config. Approved runs then
   dispatch via `workflow_dispatch`; the runner pushes the branch Foundry
   chose and opens the PR the webhook watches.

## What dogfooding is expected to surface

Fixture-pinned tests prove the gates fire; dogfooding proves the *plumbing*
around them survives reality: webhook delivery ordering, label races, GitHub
API pagination and rate limits, comment formatting on real tickets, PR
correlation against real branch names, and - over time - delivery-memory
priors and agent scorecards computed from genuine outcomes. File what it
breaks as `bug` issues; that is the point.
