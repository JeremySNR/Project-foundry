# Webhook fixtures (spec-derived)

Realistic webhook payloads, one JSON file per event, exercised by
`tests/test_webhook_fixtures.py` through the real signed API endpoints. They are
**spec-derived** (written from the providers' webhook documentation), not yet
captured from live traffic - see the provenance note below.

Why they exist: the payload mappings (`src/foundry/api/mapping.py`, the
connectors) are where live integrations break first, and fixing them should
not require live credentials. If a real Linear/GitHub/GitLab/Jira webhook
exposes a mapping bug:

1. Capture the payload (webhook dashboards show recent deliveries).
2. **Redact anything private** (org names, emails, tokens, internal URLs).
3. Drop it in here, add a case to `test_webhook_fixtures.py` asserting what it
   should map to, and fix the mapping.

The fixtures below were written from the providers' webhook documentation;
payloads captured from live traffic are strictly better and welcome as
replacements.

## GitHub REST responses (catalog code facts)

`github_tree_recursive.json` / `github_tree_truncated.json` mirror the Git
Trees API (`GET /repos/{repo}/git/trees/{ref}?recursive=1`), and the
`github_*_contents.json` files mirror the contents API (base64 payloads) for
CODEOWNERS and root manifests. They pin the code-facts phase of
`foundry-catalog sync` in `tests/test_catalog_sync.py` and the code-aware
enricher in `tests/test_code_enricher.py` - same rule: no network in tests,
recorded payloads only.
