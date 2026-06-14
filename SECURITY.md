# Security Policy

Foundry is a control plane that decides whether AI agents may act on your
code. Vulnerabilities in it are, by definition, supply-chain-shaped: please
report them privately.

## Reporting a vulnerability

Use GitHub's **private vulnerability reporting** on this repository
(Security tab → "Report a vulnerability"). Please include reproduction steps
and the impact you believe the issue has on the policy/approval guarantees.

We aim to acknowledge reports within 72 hours.

## Scope - what counts as a vulnerability here

Anything that lets work bypass the governance loop, including:

- approving or dispatching a run without a configured approver's action
  (auth bypass on the approval surfaces);
- satisfying a sensitive-area approval requirement without the configured
  role (privilege escalation);
- forging or replaying Linear/GitHub webhooks past signature verification;
- getting an agent to act on paths the policy forbids, or evading the
  diff-aware risk escalation;
- secrets reaching an agent prompt past the dispatch guard;
- tampering with the audit trail (artifact/decision hashes).

## Deployment expectations

Foundry fails closed by design, but you still need to deploy it sensibly:

- Set strong values for `FOUNDRY_LINEAR_WEBHOOK_SECRET`,
  `FOUNDRY_GITHUB_WEBHOOK_SECRET` and `FOUNDRY_API_TOKEN`. Without an API
  token the REST approval endpoint is disabled (this is intentional).
- Terminate TLS in front of the API; webhook signatures authenticate payloads,
  not transport.
- Keep the approver → roles mapping in reviewed, committed YAML.
- **Treat `FOUNDRY_JIRA_WEBHOOK_SECRET` as an approver-level credential.**
  Jira webhooks carry no HMAC signature over the body, so the approver
  identity is taken from the payload (`comment.author.emailAddress`). Anyone
  holding the shared token can therefore assert any configured approver's
  email and approve sensitive work as them. Scope and rotate the token
  accordingly. Foundry accepts the token from the `X-Foundry-Webhook-Token`
  header only; query-string delivery (`?token=`, which leaks into access
  logs, proxies, and link history) is off unless you explicitly set
  `tracker.jira_allow_query_token: true`. The GitHub PR webhook falls back to
  the Linear signing secret when `FOUNDRY_GITHUB_WEBHOOK_SECRET` is unset, so
  one leaked secret can cover both providers — set a distinct GitHub secret in
  production.
