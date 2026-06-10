"""Catalog sync: list the GitHub org and deep-fetch per-repo metadata.

``CatalogSync.sync()`` is a two-phase algorithm:

1. **List sweep** — page through all org repos, upsert lightweight fields.
2. **Deep fetch** — for new or changed repos, fetch README, top-level dirs,
   and recent merged PR metadata (titles + contributor logins).

Everything is state-driven: a crash mid-sweep loses at most one repo's work,
and the next run resumes automatically from whatever ``synced_at`` rows record.
"""

from __future__ import annotations

import base64
import json
import logging
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

_log = logging.getLogger(__name__)


class CatalogSyncError(RuntimeError):
    """The org listing failed; the sweep is aborted before any deletion.

    Proceeding with a partial (or empty) listing would delete every catalog
    row not in it - a typo'd org or lost token access must never wipe the
    catalog the enricher depends on.
    """


@dataclass(frozen=True)
class SyncReport:
    repos_listed: int
    deep_fetched: int
    deleted: int
    calls_used: int
    budget_exhausted: bool


class CatalogSync:
    """Syncs GitHub org repo metadata into the ``foundry_repo_catalog`` table."""

    def __init__(
        self,
        session_factory: Any,
        transport: Callable[..., tuple[int, dict[str, str], Any]],
        *,
        call_budget: int = 3000,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._session_factory = session_factory
        self._transport = transport
        self._call_budget = call_budget
        self._now = now
        self._calls_used = 0

    def _call(self, method: str, path: str) -> tuple[int, dict[str, str], Any]:
        self._calls_used += 1
        return self._transport(method, path)

    def _budget_remaining(self) -> int:
        return self._call_budget - self._calls_used

    def sync(self, org: str, *, bootstrap: bool = False) -> SyncReport:
        from foundry.db.models import FoundryRepoCatalogEntry

        self._calls_used = 0
        repos_listed: list[dict[str, Any]] = []

        # Phase 1: list sweep
        page = 1
        while True:
            if self._budget_remaining() <= 0:
                _log.warning("Budget exhausted during list sweep at page %d", page)
                return SyncReport(
                    repos_listed=len(repos_listed),
                    deep_fetched=0,
                    deleted=0,
                    calls_used=self._calls_used,
                    budget_exhausted=True,
                )
            status, _, data = self._call(
                "GET",
                f"/orgs/{org}/repos?type=all&per_page=100&page={page}",
            )
            if status != 200 or not isinstance(data, list):
                raise CatalogSyncError(
                    f"Listing repos for org {org!r} failed (HTTP {status}, page {page}); "
                    "aborting sweep without touching the catalog."
                )
            repos_listed.extend(data)
            if len(data) < 100:
                break
            page += 1

        listed_names: set[str] = set()
        with self._session_factory() as session:
            for repo_data in repos_listed:
                full_name: str = repo_data.get("full_name", "")
                if not full_name:
                    continue
                listed_names.add(full_name)

                pushed_at = _parse_iso(repo_data.get("pushed_at"))
                topics = repo_data.get("topics") or []

                existing = session.get(FoundryRepoCatalogEntry, full_name)
                if existing is None:
                    entry = FoundryRepoCatalogEntry(
                        repo=full_name,
                        description=repo_data.get("description"),
                        topics=json.dumps(topics),
                        primary_language=repo_data.get("language"),
                        archived=bool(repo_data.get("archived", False)),
                        default_branch=repo_data.get("default_branch"),
                        pushed_at=pushed_at,
                    )
                    session.add(entry)
                else:
                    existing.description = repo_data.get("description")
                    existing.topics = json.dumps(topics)
                    existing.primary_language = repo_data.get("language")
                    existing.archived = bool(repo_data.get("archived", False))
                    existing.default_branch = repo_data.get("default_branch")
                    existing.pushed_at = pushed_at
            session.commit()

            # Phase 2: deletion of repos no longer in listing
            all_entries = session.query(FoundryRepoCatalogEntry).all()
            deleted = 0
            for entry in all_entries:
                if entry.repo not in listed_names:
                    session.delete(entry)
                    deleted += 1
            if deleted:
                session.commit()

            # Phase 3: deep-fetch selection
            all_entries = session.query(FoundryRepoCatalogEntry).all()
            to_deep_fetch = []
            for entry in all_entries:
                if entry.archived:
                    continue
                pushed = _utc(entry.pushed_at)
                synced = _utc(entry.synced_at)
                needs_deep = (
                    synced is None
                    or bootstrap
                    or (pushed is not None and synced is not None and pushed > synced)
                )
                if needs_deep:
                    to_deep_fetch.append(entry.repo)

        deep_fetched = 0
        for repo_name in to_deep_fetch:
            if self._budget_remaining() < 3:
                _log.warning("Budget exhausted before deep-fetching %s", repo_name)
                return SyncReport(
                    repos_listed=len(repos_listed),
                    deep_fetched=deep_fetched,
                    deleted=deleted,
                    calls_used=self._calls_used,
                    budget_exhausted=True,
                )
            self._deep_fetch(repo_name)
            deep_fetched += 1

        return SyncReport(
            repos_listed=len(repos_listed),
            deep_fetched=deep_fetched,
            deleted=deleted,
            calls_used=self._calls_used,
            budget_exhausted=False,
        )

    def _deep_fetch(self, repo: str) -> None:
        from foundry.db.models import FoundryRepoCatalogEntry

        readme_head: str | None = None
        top_dirs: list[str] = []
        recent_pr_titles: list[str] = []
        top_contributors: list[str] = []

        # README
        status, _, data = self._call("GET", f"/repos/{repo}/readme")
        if status == 200 and isinstance(data, dict):
            raw_content = data.get("content", "")
            try:
                decoded = base64.b64decode(raw_content).decode("utf-8", errors="replace")
                readme_head = decoded[:4096]
            except Exception:
                readme_head = None
        else:
            readme_head = None

        # Top-level directory listing
        status, _, data = self._call("GET", f"/repos/{repo}/contents/")
        if status == 200 and isinstance(data, list):
            top_dirs = [entry["name"] for entry in data[:50] if isinstance(entry, dict)]
        else:
            top_dirs = []

        # Recent merged PRs
        status, _, data = self._call(
            "GET",
            f"/repos/{repo}/pulls?state=closed&sort=updated&direction=desc&per_page=30",
        )
        contributor_counts: Counter[str] = Counter()
        if status == 200 and isinstance(data, list):
            for pr in data:
                if not isinstance(pr, dict):
                    continue
                if pr.get("merged_at") is not None:
                    title = pr.get("title")
                    if title:
                        recent_pr_titles.append(title)
                    login = (pr.get("user") or {}).get("login")
                    if login:
                        contributor_counts[login] += 1
        top_contributors = [login for login, _ in contributor_counts.most_common(10)]

        with self._session_factory() as session:
            entry = session.get(FoundryRepoCatalogEntry, repo)
            if entry is not None:
                entry.readme_head = readme_head
                entry.top_dirs = json.dumps(top_dirs)
                entry.recent_pr_titles = json.dumps(recent_pr_titles)
                entry.top_contributors = json.dumps(top_contributors)
                entry.synced_at = self._now()
            session.commit()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, AttributeError):
        return None


def _utc(dt: datetime | None) -> datetime | None:
    """Ensure a datetime is timezone-aware UTC (SQLite may return naive datetimes)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt
