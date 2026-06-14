# Quickstart: zero to governed PR in ~30 minutes

This walks you from nothing to a real run: a Linear ticket analysed, a plan
approved by a human, a coding agent dispatched, and the resulting PR tracked
with guardrails re-checked on every push.

Want to see the loop before wiring anything? `python scripts/demo.py` runs the
whole governed story offline in under a minute, no credentials needed.

This guide uses Linear + GitHub (the default pairing). GitHub Issues and Jira
work as trackers too (`tracker.provider` in the YAML), and GitLab as the SCM
(`/webhooks/gitlab`) - see the README's "Bring your own tracker and SCM".

## 0. What you need

- Docker (with compose)
- A Linear workspace where you can create webhooks and API keys
- A GitHub repository the agent will work on, and a token that can read PRs
  (and fire `workflow_dispatch` if you use the Claude Code provider)
- A coding agent: the Cursor Linear integration (easiest), a Cursor Cloud API
  key, Claude Code via GitHub Actions, or any agent behind a webhook
- A public URL for webhooks while testing - `ngrok http 8000` is fine

## 1. Configure

Copy the example config and edit it:

```bash
cp foundry.example.yaml foundry.yaml
```

The three sections that matter first (keys must be nested exactly as below -
`config.py` ignores anything it doesn't recognise, so a misplaced block is
silently dropped):

```yaml
approval:
  approvers:
    - email: you@company.com
      roles: [engineering, security, product]

agent:
  provider: cursor_via_linear   # or cursor_cloud / claude_code / webhook / manual

context:
  provider: static              # keyword routing, no DB required (the default)
  # Map each candidate repo to the words that, in a ticket, point at it.
  # With provider: static this list IS the routing catalog; with
  # provider: catalog / code it's merged into the synced org catalog.
  repo_keywords:
    your-org/your-repo: [favourites, checkout, billing]
```

There is no top-level `approvers:` or `repos:` key - routing is driven by
`context.repo_keywords` (or, for `provider: catalog`/`code`, the catalog synced
by `foundry-catalog sync`). See `foundry.example.yaml` for every option.

Secrets never go in the YAML. Put them in `.env` next to `docker-compose.yaml`:

```bash
FOUNDRY_LINEAR_WEBHOOK_SECRET=...   # from step 3
FOUNDRY_GITHUB_WEBHOOK_SECRET=...   # from step 4
FOUNDRY_LINEAR_API_TOKEN=lin_api_...
FOUNDRY_GITHUB_API_TOKEN=ghp_...
FOUNDRY_API_TOKEN=$(openssl rand -hex 24)   # gates the API + dashboard
```

## 2. Run it

```bash
docker compose up --build
```

- API: http://localhost:8000 (health: `/healthz`)
- Dashboard: http://localhost:8000/dashboard (paste your `FOUNDRY_API_TOKEN`)
- Postgres data persists in the `foundry-pg` volume

Expose it: `ngrok http 8000` and note the public URL.

## 3. Linear webhook

In Linear: Settings → API → Webhooks → New webhook.

- URL: `https://<your-public-url>/webhooks/linear`
- Events: Issues + Comments
- Copy the signing secret into `FOUNDRY_LINEAR_WEBHOOK_SECRET` and restart.

## 4. GitHub webhook

In the target repo: Settings → Webhooks → Add webhook.

- URL: `https://<your-public-url>/webhooks/github`
- Content type: `application/json`
- Secret: whatever you set as `FOUNDRY_GITHUB_WEBHOOK_SECRET`
- Events: Pull requests, Pull request reviews, Check suites

## 5. The agent

**Cursor via Linear (recommended first run).** Install the Cursor Linear
integration (Cursor dashboard → Integrations → Linear) and connect GitHub.
Foundry delegates by posting an `@Cursor` comment with the governed
instructions; Cursor opens the PR itself.

**Claude Code.** Copy `examples/claude-code-runner.yml` into the target repo as
`.github/workflows/foundry-claude-code.yml`, add `ANTHROPIC_API_KEY` to the
repo secrets, and set `agent.provider: claude_code`.

**Your own agent.** Set `agent.provider: webhook` plus
`FOUNDRY_AGENT_WEBHOOK_URL` (and `FOUNDRY_AGENT_WEBHOOK_SECRET`). Foundry POSTs
the signed job input; your receiver does the work and opens a PR on the branch
named in the payload.

## 6. First run

1. Create a Linear issue with a clear description and an
   `Acceptance Criteria:` section (bullet list). Thin tickets get parked as
   *needs clarification* with drafted criteria to react to - that is working
   as intended.
2. Add the `foundry:candidate` label (or comment `/foundry analyse`).
3. Foundry posts the analysis: work type, risk, chosen repo, the delivery
   plan, and what approval it needs.
4. Approve by commenting `/foundry approve` (you must be in `approvers`).
   Reject with `/foundry reject`, halt with `/foundry stop`.
5. The agent is dispatched through the policy gate. Watch the run on the
   dashboard - every artifact, policy decision and audit event is there.
6. The PR opens and Foundry tracks it: forbidden paths block the run, diffs
   into unanticipated sensitive areas escalate to human review, CI failures
   and changes-requested reviews re-dispatch the agent with the failure
   context (at most `remediation.max_agent_retries` times), and merge/close
   completes the run.

## 7. Verify end-to-end (optional)

The live smoke test drives all of the above from one script:

```bash
FOUNDRY_E2E=1 \
FOUNDRY_CONFIG=foundry.yaml \
FOUNDRY_LINEAR_API_TOKEN=lin_api_... \
FOUNDRY_E2E_ISSUE_ID=<linear issue uuid> \
FOUNDRY_E2E_APPROVER=you@company.com \
FOUNDRY_GITHUB_API_TOKEN=ghp_... \
FOUNDRY_E2E_REPO=your-org/your-repo \
python scripts/smoke_e2e.py
```

It refuses to run without `FOUNDRY_E2E=1` and is never part of CI.

## Troubleshooting

- **Webhook returns 401** - signing secret mismatch; check the right secret is
  in `.env` and the container was restarted.
- **Run stuck at `waiting_approval`** - the approver's email must match the
  Linear account email on the `/foundry approve` comment, and must be listed
  in `approvers`.
- **`policy gate blocked`** on approve - that is the point: check the policy
  decision on the dashboard for the reasons (risk level, missing role,
  low repo confidence).
- **PR not picked up** - the GitHub webhook must be on the repo the agent
  pushed to; correlation works by branch name or the issue key (e.g.
  `LIN-123`) appearing in the branch or PR title.
