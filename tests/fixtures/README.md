# Recorded webhook fixtures

Realistic webhook payloads, one JSON file per event, exercised by
`tests/test_webhook_fixtures.py` through the real signed API endpoints.

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
