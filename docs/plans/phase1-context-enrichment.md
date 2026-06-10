# Implementation Spec — Phase 1: Repo Catalog + Catalog-Backed Context Enrichment

**Status:** ready to implement
**Audience:** an implementing agent with access to this repository but no other context.
**Branch:** all work on `claude/repo-qovery-comparison-1g8kc9`.

---

## 1. Background and goal

Foundry's context-enrichment stage decides *which repository a ticket belongs in* and attaches a
confidence to every candidate. The policy gate blocks autonomous work unless exactly one repo
clears `repo_confidence_threshold` (default 70) — see
`ContextBundle.has_confident_repository()` in `src/foundry/schemas/context.py`.

Today the only enricher is `StaticContextEnricher` (`src/foundry/engines/enrichment.py`):

- Tier 0 signals work: explicit `ticket.known_repositories` → confidence 90; linked resources with
  a `repo` → confidence 85.
- Keyword matching against a hand-maintained `repo_catalog` dict exists **but is never wired in
  production**: `build_orchestrator()` in `src/foundry/api/app.py` does not construct an enricher,
  so the orchestrator default `StaticContextEnricher()` runs with an empty catalog.

**Goal of this phase:** a self-maintaining *repo catalog* — a DB table of per-repo metadata synced
from the GitHub org — plus a new `CatalogContextEnricher` that scores ticket text against the
catalog with explainable, freshness-aware confidence, wired via config. This replaces hand-written
keyword lists with an index that maintains itself, and scales to thousands of repos because all
scoring happens over a tiny metadata corpus.

Design constraints (non-negotiable, from the project's principles in `VISION.md` and `README.md`):

1. **Explainable evidence.** Every candidate carries a human-readable `reason` naming the matched
   terms, the fields they matched in, and the catalog sync age. No opaque scores.
2. **Fail closed on staleness.** A stale catalog entry must *degrade* confidence below the dispatch
   threshold and surface in `ContextBundle.unknowns` — never confidently serve old data.
3. **Same code on laptop and prod.** Scoring is pure Python over rows loaded from the DB; it must
   behave identically on SQLite and Postgres. No FTS extensions, no embeddings, no new search
   infrastructure, no new heavyweight dependencies.
4. **Offline tests.** The full test suite must pass with no network and no credentials. All HTTP is
   behind an injected transport callable with fakes in tests (existing house pattern).
5. **Calibration invariant.** A single coincidental keyword match must NOT cross the default
   dispatch threshold of 70 (see the comment in `StaticContextEnricher.enrich`, ~line 63). Two or
   more independent matched terms are required.

Out of scope for this phase (do not build): delivery-memory priors from past runs, top-K
shallow-clone code search, ETag conditional requests (we add the column but don't use it), GitLab
catalog sources, embeddings of any kind.

---

## 2. Conventions to follow (read before coding)

- Python 3.11+, `from __future__ import annotations` at the top of every module.
- SQLAlchemy 2.0 style: `Mapped[...]` + `mapped_column(...)`, declarative `Base` from
  `src/foundry/db/base.py`. JSON-ish lists are stored as `Text` containing JSON (use
  `json.dumps`/`json.loads`); follow `src/foundry/db/models.py` for naming and style.
- Config is the frozen dataclass `Settings` in `src/foundry/config.py` with three layers:
  defaults < YAML (`_from_yaml`) < env (`_from_env`). Behaviour goes in YAML, secrets in env.
- HTTP transports are injected callables built by factories in
  `src/foundry/connectors/transport.py`; they retry 429/5xx with backoff. Tests never construct
  live transports — they pass plain Python functions (see `tests/test_github_connector.py`,
  `test_files_enriched_via_transport` for the style).
- Tests are function-style pytest, offline, deterministic. DB tests use in-memory SQLite via
  `make_engine()` + `create_all()` + `make_session_factory()` from `foundry.db`.
- Module docstrings explain *why* the module exists, in the codebase's existing plain-spoken tone.
- Run `pytest` from the repo root; everything must be green before committing.

---

## 3. Work item A — DB model and migration

### A1. New model in `src/foundry/db/models.py`

Add (and add the table to the module docstring's table list):

```python
class FoundryRepoCatalogEntry(Base):
    """One row per repository in the org: the metadata the enricher scores against.

    Metadata only - never file contents beyond the README head. ``synced_at`` is
    when we last deep-fetched; ``pushed_at`` is GitHub's last-push time refreshed
    on every sweep. ``pushed_at > synced_at`` means the entry is stale.
    """

    __tablename__ = "foundry_repo_catalog"

    repo: Mapped[str] = mapped_column(String(255), primary_key=True)  # "org/name"
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    topics: Mapped[str] = mapped_column(Text, default="[]")            # JSON list[str]
    primary_language: Mapped[str | None] = mapped_column(String(64), nullable=True)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    default_branch: Mapped[str | None] = mapped_column(String(128), nullable=True)
    readme_head: Mapped[str | None] = mapped_column(Text, nullable=True)   # first 4096 chars
    top_dirs: Mapped[str] = mapped_column(Text, default="[]")          # JSON list[str]
    recent_pr_titles: Mapped[str] = mapped_column(Text, default="[]")  # JSON list[str]
    top_contributors: Mapped[str] = mapped_column(Text, default="[]")  # JSON list[str] of logins
    pushed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    etag: Mapped[str | None] = mapped_column(String(128), nullable=True)  # reserved, unused
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
```

No relationships to other tables. SQLite/dev gets the table automatically via `create_all()`
(`src/foundry/db/base.py`).

### A2. Migration `migrations/versions/0003_repo_catalog.py`

Hand-written in the style of `0002_agent_job_cost.py`: `revision = '0003'`,
`down_revision = '0002'`, `upgrade()` does `op.create_table('foundry_repo_catalog', ...)` with the
columns above, `downgrade()` drops it.

---

## 4. Work item B — transport addition

In `src/foundry/connectors/transport.py`, add a factory alongside `github_transport`:

```python
def github_rest_transport(
    token: str, *, client: Any | None = None, base: str = GITHUB_API_BASE
) -> Callable[..., tuple[int, dict[str, str], Any]]:
    """``transport(method, path) -> (status, headers, json|None)`` for the catalog sync.

    Unlike ``github_transport`` this exposes status and headers, because the sync
    needs pagination metadata and (later) conditional requests. 404 is returned,
    not raised - missing READMEs are normal.
    """
```

Behaviour:
- Same auth headers, retry statuses, backoff, and `Retry-After` handling as `github_transport`
  (copy the loop; factor shared pieces only if it stays readable).
- Returns `(status_code, dict(response.headers), parsed_json_or_None)`.
- **404 returns `(404, headers, None)` instead of raising** (the sync treats missing
  README/contents as absent, not as failure). Other 4xx still raise.

---

## 5. Work item C — catalog sync package `src/foundry/catalog/`

New package: `src/foundry/catalog/__init__.py`, `sync.py`, `cli.py`.

### C1. `sync.py` — `CatalogSync`

```python
class CatalogSync:
    def __init__(
        self,
        session_factory,
        transport,                       # github_rest_transport-shaped callable
        *,
        call_budget: int = 3000,
        now=lambda: datetime.now(timezone.utc),
    ) -> None: ...

    def sync(self, org: str, *, bootstrap: bool = False) -> SyncReport: ...
```

`SyncReport` is a small frozen dataclass: `repos_listed: int`, `deep_fetched: int`,
`deleted: int`, `calls_used: int`, `budget_exhausted: bool`.

**Algorithm for `sync(org)`:**

1. **List sweep (always runs, cheap).** Page through
   `GET /orgs/{org}/repos?type=all&per_page=100&page=N` until a page returns fewer than 100 items.
   Each request counts against the budget. For every listed repo, upsert the light fields onto the
   row (creating it if new): `description`, `topics` (from the listing's `topics` array),
   `primary_language` (listing `language`), `archived`, `default_branch`, `pushed_at` (parse the
   ISO timestamp). Do **not** touch `synced_at` here.
2. **Deletion.** Any catalog row whose `repo` was not in the listing is deleted (the repo is gone
   or access was lost — keeping it would be stale-confident routing).
3. **Deep-fetch selection.** A repo needs a deep fetch when any of: it is new (`synced_at IS
   NULL`); `bootstrap=True`; GitHub `pushed_at` is newer than stored `synced_at`. Skip archived
   repos (light fields are enough; the enricher ignores them anyway).
4. **Deep fetch (3 calls per repo, budget-checked before each):**
   - `GET /repos/{repo}/readme` → response JSON has base64 `content`; decode, take the first
     4096 characters into `readme_head`. 404 → `readme_head = None`.
   - `GET /repos/{repo}/contents/` → list of entries; store up to 50 entry `name`s in `top_dirs`.
     404/non-list → `[]`.
   - `GET /repos/{repo}/pulls?state=closed&sort=updated&direction=desc&per_page=30` → keep titles
     of entries whose `merged_at` is non-null in `recent_pr_titles`; tally `user.login` across the
     same merged entries and store the 10 most frequent logins in `top_contributors`.
   - On success set `synced_at = now()` and commit the row.
5. **Budget.** Before *every* HTTP call, check the budget. When exhausted: commit everything done
   so far, stop cleanly, and return the report with `budget_exhausted=True`. The next run resumes
   naturally (selection in step 3 is state-driven). Never raise on budget exhaustion.
6. Commit per-repo (or in small batches) so a crash mid-sweep loses at most one repo's work.

Notes:
- This module must not import httpx; the transport is injected.
- Secrets discipline: the transport holds the token; nothing in this package logs or stores it.
- Never fetch any file contents other than the README. This is a metadata catalog by design.

### C2. `cli.py` — console entry point

```
foundry-catalog sync [--org ORG] [--bootstrap] [--budget N]
```

- `main()` uses argparse with a `sync` subcommand.
- Settings come from `Settings.load(os.environ.get("FOUNDRY_CONFIG"))` (same as
  `app_from_env` in `src/foundry/api/app.py`). `--org` falls back to `settings.context_org`;
  missing both → exit 2 with a clear message. Missing `FOUNDRY_GITHUB_API_TOKEN`
  (`settings.github_api_token`) → exit 2.
- Build engine/session via `make_engine(settings.database_url)`, `create_all(engine)`,
  `make_session_factory(engine)` (import from `foundry.db`).
- Build the transport with `github_rest_transport(settings.github_api_token)`.
- Run the sync, print a one-line human summary of the `SyncReport`, exit 0 (also when
  `budget_exhausted` — that's a normal partial sweep).

### C3. `pyproject.toml`

Add:

```toml
[project.scripts]
foundry-catalog = "foundry.catalog.cli:main"
```

(There is no existing `[project.scripts]` table; create it.)

---

## 6. Work item D — `CatalogContextEnricher` in `src/foundry/engines/enrichment.py`

### D1. Shared Tier-0 helper

Extract the explicit/linked-candidate logic currently inside `StaticContextEnricher.enrich`
(the `known_repositories` loop at confidence 90 and the `linked_resources` loop at 85, plus the
`consider()` max-merge helper and the related-PR/issue extraction) into module-level helpers so
both enrichers share them. `StaticContextEnricher`'s observable behaviour must not change —
existing tests in `tests/test_engines.py` must pass unmodified.

### D2. The new enricher

```python
class CatalogContextEnricher:
    """Scores ticket text against the synced repo catalog, with freshness-aware confidence."""

    def __init__(
        self,
        session_factory,
        *,
        repo_keywords: dict[str, list[str]] | None = None,   # legacy manual catalog, still honoured
        default_test_commands: list[str] | None = None,
        max_catalog_age_days: int = 7,
        now=lambda: datetime.now(timezone.utc),
    ) -> None: ...

    def enrich(self, ticket: RawTicket, analysis: TicketAnalysis) -> ContextBundle: ...
```

`enrich()` pipeline:

1. **Tier 0** — shared helpers: explicit repos (90), linked repos (85). These always win the
   max-merge against catalog scores.
2. **Manual keywords** — if `repo_keywords` is set, apply exactly the existing
   `StaticContextEnricher` keyword logic (`min(50 + 10 * hits, 95)`).
3. **Catalog scoring** — load all non-archived rows. If the table is empty, skip to step 6 and
   append to `unknowns`: `"Repo catalog is empty - run 'foundry-catalog sync' to populate it."`
4. **Freshness capping** (see D4).
5. **Merge** all candidates with the max-confidence `consider()` rule; sort descending (existing
   behaviour).
6. **Bundle**: `related_prs`/`related_issues` exactly as Static; `test_commands` from
   `default_test_commands`; `docs` gets one entry per above-threshold catalog candidate of the form
   `"{repo}: {description}"` (when a description exists); `candidate_files` stays empty (later
   phase). `unknowns` gets `"No candidate repository could be identified."` when there are no
   candidates at all (same string as Static), plus any staleness/empty-catalog messages.

### D3. Scoring algorithm (deterministic, pure Python — implement exactly)

**Tokenization** — `_tokens(text)`: lowercase, `re.findall(r"[a-z0-9]+", text)`, drop tokens
shorter than 3 chars, drop a small built-in stopword set (at minimum: `the and for with that this
from are was should would when then than can our your has have not but all any out new use using
add fix bug issue ticket user`).

**Catalog document** — per repo, a mapping of field → token set, with field weights:

| field | source | weight |
|---|---|---|
| `name` | repo name part after `/`, split on `-`/`_` too | 3.0 |
| `topics` | JSON list | 3.0 |
| `description` | description | 2.0 |
| `pr_titles` | recent_pr_titles joined | 2.0 |
| `dirs` | top_dirs joined | 2.0 |
| `readme` | readme_head | 1.0 |

**IDF filter** — compute document frequency of each token across all catalog repos (a token
"occurs in" a repo if it appears in any field). Ignore query tokens whose document frequency
exceeds `max(3, 25% of repo count)` — this kills generic terms like "service"/"api" that would
otherwise match everything.

**Per-repo score** — for each surviving query token, add the weight of each field that contains it
(a token counts once per field, not per occurrence). Track `matched_terms` = set of distinct query
tokens that matched anywhere, and `matched_fields` per term for the reason string.

**Confidence mapping** — candidates require `matched_terms >= 1`:

```
confidence = min(50 + 10 * len(matched_terms), 95)
```

This deliberately mirrors the existing calibration: 1 term → 60 (below the 70 threshold), 2 terms
→ 70. The weighted score is used only to *rank* catalog candidates and break ties (it must NOT
inflate confidence). When multiple repos have identical confidence the higher weighted score sorts
first; ties beyond that sort lexicographically for determinism.

**Reason string** — e.g.
`"Catalog match: 'invoice', 'reconciliation' (description, 2 PR titles; synced 2d ago)."`
Exact wording is flexible; it MUST name the matched terms, at least the strongest fields, and the
sync age.

### D4. Freshness rule (fail-closed — implement exactly)

A catalog row is **stale** when either:
- `pushed_at` and `synced_at` are both set and `pushed_at > synced_at` (the repo changed since we
  deep-fetched), **or**
- `updated_at` is older than `max_catalog_age_days` (the sync job has stopped running).

For stale rows, catalog-derived confidence is capped at **65** (below the default 70 threshold) and
the reason gains a suffix like `"(stale: last synced 12d ago)"`. If any candidate was capped, append
once to `unknowns`:
`"Repo catalog data is stale for some candidates - run 'foundry-catalog sync'."`

Tier-0 and manual-keyword confidences are never capped by freshness.

---

## 7. Work item E — config

### E1. `Settings` fields (`src/foundry/config.py`)

```python
# --- context enrichment (behaviour: yaml) ---
context_provider: str = "static"          # "static" | "catalog"
context_org: str | None = None            # GitHub org for foundry-catalog sync
context_repo_keywords: tuple[tuple[str, tuple[str, ...]], ...] = ()
context_max_catalog_age_days: int = 7
context_sync_call_budget: int = 3000
```

### E2. YAML parsing in `_from_yaml`

```yaml
context:
  provider: catalog            # or "static" (default)
  org: acme
  max_catalog_age_days: 7
  sync_call_budget: 3000
  repo_keywords:               # optional manual hints, merged with catalog scoring
    acme/billing-service: ["invoice", "stripe"]
```

`repo_keywords` parses like `sensitive_path_globs` does (mapping → tuple of `(key, tuple(values))`).

### E3. Env overrides in `_from_env`

`FOUNDRY_CONTEXT_PROVIDER` → `context_provider`; `FOUNDRY_CONTEXT_ORG` → `context_org`.

### E4. Validation in `_validate`

- `context_provider` must be `static` or `catalog`.
- `context_max_catalog_age_days >= 1`, `context_sync_call_budget >= 1`.

### E5. Docs

Add the `context:` block to `foundry.example.yaml` with comments, and add
`FOUNDRY_CONTEXT_PROVIDER` / `FOUNDRY_CONTEXT_ORG` rows to the env-var table in `README.md`.
Mention the `foundry-catalog sync` command where the README discusses running Foundry.

---

## 8. Work item F — wiring

### F1. `build_orchestrator` (`src/foundry/api/app.py`)

Construct the enricher from settings and pass it through (the `enricher=` parameter already exists
on `FoundryOrchestrator.__init__`):

```python
repo_keywords = {repo: list(kws) for repo, kws in settings.context_repo_keywords}
if settings.context_provider == "catalog":
    enricher = CatalogContextEnricher(
        session_factory,
        repo_keywords=repo_keywords,
        max_catalog_age_days=settings.context_max_catalog_age_days,
    )
else:
    enricher = StaticContextEnricher(repo_catalog=repo_keywords)
```

Note this also fixes the pre-existing hole where YAML keywords were never wired for the static
enricher.

### F2. Webhook freshness nudge (`src/foundry/api/app.py`, GitHub webhook handler)

After the existing event handling in the `github_webhook` route, best-effort update the catalog:
if the payload has `repository.full_name` and a catalog row exists for it, set its
`pushed_at = now`. Wrap in try/except (log at debug on failure) — a catalog hiccup must never fail
webhook processing. This keeps staleness detection live between sync sweeps. GitHub webhooks are
at-least-once delivery; a timestamp write is naturally idempotent.

Do the same in the GitLab handler ONLY if trivial; otherwise skip (GitLab catalog is out of scope).

---

## 9. Work item G — tests

All offline. Use the existing styles: plain-function fake transports
(`tests/test_github_connector.py`), in-memory SQLite via `foundry.db` helpers, function-style
pytest.

### G1. `tests/test_catalog_sync.py`

Build a fake `(method, path) -> (status, headers, json)` transport backed by canned dicts (small
JSON fixture files in `tests/fixtures/` are welcome but inline dicts are fine; follow what reads
best). Cases:

1. **Bootstrap populates rows** — 2-page listing (100 + 3 repos → assert pagination), rows created
   with light fields, deep fetch performed, `synced_at` set, README decoded from base64 and
   truncated to 4096 chars, merged-PR titles only (non-merged filtered out), contributors tallied.
2. **Unchanged repos skip deep fetch** — run sync twice with identical `pushed_at`; assert the
   second run's transport saw only the listing calls (count calls by path).
3. **Changed `pushed_at` triggers refetch** — bump one repo's `pushed_at`; only that repo deep-fetches.
4. **Archived repos** — marked `archived=True`, never deep-fetched.
5. **Deleted repos** — row present in DB but absent from listing is removed.
6. **README 404** — `readme_head is None`, sync still succeeds.
7. **Budget exhaustion** — budget that covers the listing plus one repo's deep fetch; assert clean
   stop, `budget_exhausted=True`, partial progress committed, and that a follow-up run resumes
   (deep-fetches the remaining repos).

### G2. `tests/test_catalog_enricher.py`

Seed an in-memory DB with catalog rows; fix `now` for determinism. Cases:

1. **Two matched terms cross the threshold** — ticket mentioning two informative terms from one
   repo's metadata → that repo at confidence 70+, reason names both terms.
2. **Single coincidental term does not** — one matched term → confidence 60, and
   `has_confident_repository()` is False.
3. **IDF filter** — a token present in (say) all 10 seeded repos contributes nothing.
4. **Tier 0 wins** — ticket with `known_repositories=["acme/x"]` → `acme/x` at 90 regardless of
   catalog scores.
5. **Stale by push** — `pushed_at > synced_at` → confidence capped at 65, stale suffix in reason,
   staleness message in `unknowns`.
6. **Stale by age** — `updated_at` older than `max_catalog_age_days` → same capping.
7. **Empty catalog** — behaves like Static (Tier 0 + manual keywords only) and adds the
   empty-catalog `unknowns` message.
8. **Archived rows ignored.**
9. **Determinism** — same inputs twice → identical `ContextBundle`.
10. **Manual `repo_keywords` still work** and merge by max confidence.

### G3. Extend `tests/test_config.py`

`context:` YAML block parsing (all five keys), env overrides, validation errors for a bad provider
and non-positive numbers, and defaults when the block is absent.

### G4. Extend wiring tests (`tests/test_api.py` or `tests/test_orchestrator.py`)

- `build_orchestrator` with `context_provider="catalog"` injects a `CatalogContextEnricher`; with
  `"static"` injects `StaticContextEnricher` carrying the YAML keywords.
- A GitHub webhook request updates `pushed_at` on an existing catalog row and does not error when
  the row is absent.

### G5. Regression

`tests/test_engines.py` must pass **unmodified** — the Tier-0 extraction must not change
`StaticContextEnricher` behaviour.

---

## 10. Acceptance checklist

- [ ] `pytest` green from repo root, offline, no credentials.
- [ ] `python scripts/demo.py` runs unchanged (demo uses the static default).
- [ ] New table created by both `create_all` (SQLite) and `alembic upgrade head` (Postgres;
      verify against the docker-compose Postgres if available, otherwise eyeball 0003 carefully
      against 0001/0002 conventions).
- [ ] `foundry-catalog sync --org <org>` exits 2 with clear messages when the org or token is
      missing (unit-testable without network).
- [ ] `ContextBundle` artifacts produced by the catalog enricher validate against the existing
      Pydantic schema (`extra="forbid"` — do not add schema fields in this phase).
- [ ] No secrets in logs, job inputs, or catalog rows.
- [ ] `foundry.example.yaml` and `README.md` updated.
- [ ] Commit messages follow the repo's existing plain, descriptive style.

## 11. Verification narrative (manual, optional)

With a real token: `FOUNDRY_GITHUB_API_TOKEN=… foundry-catalog sync --org <org> --bootstrap`, then
start the API with `context.provider: catalog` and submit a ticket whose text mentions terms from
one repo's README/PR titles; confirm the run's context artifact (dashboard timeline or
`GET /runs/{id}/timeline`) shows that repo as a candidate with a reason naming the matched terms
and sync age. Then set the row's `synced_at` back 30 days in the DB and re-trigger: the candidate
must cap at 65 and the run must park for repo confirmation instead of dispatching.
