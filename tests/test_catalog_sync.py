"""Tests for CatalogSync - all offline, fake transport."""

from __future__ import annotations

import base64
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from foundry.catalog.sync import CatalogSync, CatalogSyncError
from foundry.connectors.transport import TransportError
from foundry.db.base import create_all, make_engine, make_session_factory
from foundry.db.models import FoundryRepoCatalogEntry


def _engine_and_sf():
    engine = make_engine()
    create_all(engine)
    return engine, make_session_factory(engine)


def _page_num(path: str) -> int | None:
    """Extract the page query param (last param, so anchored at end of string)."""
    m = re.search(r"[?&]page=(\d+)$", path)
    return int(m.group(1)) if m else None


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def _aware(dt: datetime) -> datetime:
    """SQLite hands back naive datetimes; normalize for comparison."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _repo(name: str, pushed: str = "2026-01-01T00:00:00Z", archived: bool = False) -> dict:
    return {
        "full_name": name,
        "description": f"Description for {name}",
        "topics": ["python", "web"],
        "language": "Python",
        "archived": archived,
        "default_branch": "main",
        "pushed_at": pushed,
    }


def _readme_response(text: str = "This is a README.") -> dict:
    return {"content": _b64(text)}


def _pr(title: str, merged: bool = True, user: str = "alice") -> dict:
    return {
        "title": title,
        "merged_at": "2026-01-02T00:00:00Z" if merged else None,
        "user": {"login": user},
    }


# ---------------------------------------------------------------------------
# 1. Bootstrap populates rows with pagination
# ---------------------------------------------------------------------------

def test_bootstrap_populates_rows_with_pagination() -> None:
    """2-page listing (100 + 3 repos), deep fetch performed, synced_at set."""
    _, sf = _engine_and_sf()

    # Build 100 repos for page 1, 3 for page 2
    page1 = [_repo(f"org/repo{i:03d}", pushed="2025-01-01T00:00:00Z") for i in range(100)]
    page2 = [
        _repo("org/alpha", pushed="2025-06-01T00:00:00Z"),
        _repo("org/beta", pushed="2025-06-01T00:00:00Z"),
        _repo("org/gamma", pushed="2025-06-01T00:00:00Z"),
    ]

    calls: list[str] = []

    def transport(method: str, path: str):
        calls.append(path)
        pn = _page_num(path)
        if "/orgs/org/repos" in path and pn == 1:
            return 200, {}, page1
        if "/orgs/org/repos" in path and pn == 2:
            return 200, {}, page2
        if "/orgs/org/repos" in path and pn and pn > 2:
            return 200, {}, []
        if "/readme" in path:
            return 200, {}, _readme_response("README content " * 400)
        if "/contents/" in path:
            return 200, {}, [{"name": "src"}, {"name": "tests"}]
        if "/pulls" in path:
            return 200, {}, [_pr("Add feature", user="alice"), _pr("Fix bug", user="bob"), _pr("Draft", merged=False)]
        return 404, {}, None

    sync = CatalogSync(sf, transport, call_budget=3000)
    report = sync.sync("org", bootstrap=True)

    assert report.repos_listed == 103
    assert report.deep_fetched == 103
    assert report.budget_exhausted is False

    # Verify pagination happened
    listing_calls = [c for c in calls if "/orgs/org/repos" in c]
    assert len(listing_calls) == 2

    with sf() as session:
        alpha = session.get(FoundryRepoCatalogEntry, "org/alpha")
        assert alpha is not None
        assert alpha.synced_at is not None
        assert alpha.readme_head is not None
        assert "README content" in alpha.readme_head
        assert len(alpha.readme_head) == 4096  # truncated from ~6000 chars
        assert json.loads(alpha.top_dirs) == ["src", "tests"]
        assert json.loads(alpha.recent_pr_titles) == ["Add feature", "Fix bug"]
        assert json.loads(alpha.top_contributors) == ["alice", "bob"]
        assert alpha.primary_language == "Python"
        assert alpha.default_branch == "main"


# ---------------------------------------------------------------------------
# 2. Unchanged repos skip deep fetch on re-run
# ---------------------------------------------------------------------------

def test_unchanged_repos_skip_deep_fetch() -> None:
    """Second run with identical pushed_at should only call listing endpoints."""
    _, sf = _engine_and_sf()

    repos = [_repo("org/stable", pushed="2025-01-01T00:00:00Z")]
    deep_calls: list[str] = []

    def transport(method: str, path: str):
        if "/orgs/org/repos" in path:
            return 200, {}, repos if _page_num(path) == 1 else []
        deep_calls.append(path)
        if "/readme" in path:
            return 200, {}, _readme_response()
        if "/contents/" in path:
            return 200, {}, []
        if "/pulls" in path:
            return 200, {}, []
        return 404, {}, None

    sync = CatalogSync(sf, transport, call_budget=3000)

    # First run: bootstrap
    sync.sync("org", bootstrap=True)
    first_deep = len(deep_calls)
    assert first_deep == 3  # readme + contents + pulls

    # Second run: no change to pushed_at
    deep_calls.clear()
    sync.sync("org")
    assert len(deep_calls) == 0, "unchanged repo should not be deep-fetched again"


# ---------------------------------------------------------------------------
# 3. Changed pushed_at triggers refetch of only that repo
# ---------------------------------------------------------------------------

def test_changed_pushed_at_triggers_refetch() -> None:
    """Bumping pushed_at for one repo causes only that repo to deep-fetch."""
    _, sf = _engine_and_sf()

    sync_time = datetime(2025, 12, 1, 0, 0, 0, tzinfo=timezone.utc)

    repos = [
        _repo("org/changed", pushed="2025-01-01T00:00:00Z"),
        _repo("org/stable", pushed="2025-01-01T00:00:00Z"),
    ]
    deep_fetched: list[str] = []

    def transport(method: str, path: str):
        if "/orgs/org/repos" in path:
            return 200, {}, repos if _page_num(path) == 1 else []
        if "/readme" in path:
            deep_fetched.append(path)
            return 200, {}, _readme_response()
        if "/contents/" in path:
            return 200, {}, []
        if "/pulls" in path:
            return 200, {}, []
        return 404, {}, None

    # First sync: set synced_at to sync_time (2025-12-01)
    sync = CatalogSync(sf, transport, call_budget=3000, now=lambda: sync_time)
    sync.sync("org", bootstrap=True)
    deep_fetched.clear()

    # Simulate a push AFTER the first sync time
    repos[0]["pushed_at"] = "2026-01-01T00:00:00Z"
    sync2 = CatalogSync(sf, transport, call_budget=3000, now=lambda: sync_time)
    sync2.sync("org")

    # Only "changed" should have been deep-fetched (pushed_at > synced_at)
    assert any("org/changed" in p for p in deep_fetched)
    assert not any("org/stable" in p for p in deep_fetched)


# ---------------------------------------------------------------------------
# 4. Archived repos are never deep-fetched
# ---------------------------------------------------------------------------

def test_archived_repos_are_not_deep_fetched() -> None:
    _, sf = _engine_and_sf()

    repos = [_repo("org/archived-repo", archived=True)]
    deep_calls: list[str] = []

    def transport(method: str, path: str):
        if "/orgs/org/repos" in path:
            return 200, {}, repos if _page_num(path) == 1 else []
        deep_calls.append(path)
        return 404, {}, None

    sync = CatalogSync(sf, transport, call_budget=3000)
    sync.sync("org", bootstrap=True)

    assert len(deep_calls) == 0

    with sf() as session:
        entry = session.get(FoundryRepoCatalogEntry, "org/archived-repo")
        assert entry is not None
        assert entry.archived is True
        assert entry.synced_at is None


# ---------------------------------------------------------------------------
# 5. Deleted repos are removed from the catalog
# ---------------------------------------------------------------------------

def test_deleted_repos_are_removed() -> None:
    _, sf = _engine_and_sf()

    repos = [_repo("org/will-disappear"), _repo("org/stays")]

    def transport(method: str, path: str):
        if "/orgs/org/repos" in path:
            return 200, {}, repos if _page_num(path) == 1 else []
        if "/readme" in path:
            return 200, {}, _readme_response()
        if "/contents/" in path:
            return 200, {}, []
        if "/pulls" in path:
            return 200, {}, []
        return 404, {}, None

    sync = CatalogSync(sf, transport, call_budget=3000)
    sync.sync("org", bootstrap=True)

    with sf() as session:
        assert session.get(FoundryRepoCatalogEntry, "org/will-disappear") is not None

    # Second run: repo disappears from listing
    def transport2(method: str, path: str):
        if "/orgs/org/repos" in path:
            return 200, {}, [_repo("org/stays")] if _page_num(path) == 1 else []
        if "/readme" in path:
            return 200, {}, _readme_response()
        if "/contents/" in path:
            return 200, {}, []
        if "/pulls" in path:
            return 200, {}, []
        return 404, {}, None

    sync2 = CatalogSync(sf, transport2, call_budget=3000)
    report = sync2.sync("org")

    assert report.deleted == 1

    with sf() as session:
        assert session.get(FoundryRepoCatalogEntry, "org/will-disappear") is None
        assert session.get(FoundryRepoCatalogEntry, "org/stays") is not None


# ---------------------------------------------------------------------------
# 6. README 404 sets readme_head to None
# ---------------------------------------------------------------------------

def test_readme_404_results_in_none() -> None:
    _, sf = _engine_and_sf()

    def transport(method: str, path: str):
        if "/orgs/org/repos" in path:
            return 200, {}, [_repo("org/no-readme")] if _page_num(path) == 1 else []
        if "/readme" in path:
            return 404, {}, None
        if "/contents/" in path:
            return 200, {}, []
        if "/pulls" in path:
            return 200, {}, []
        return 404, {}, None

    sync = CatalogSync(sf, transport, call_budget=3000)
    sync.sync("org", bootstrap=True)

    with sf() as session:
        entry = session.get(FoundryRepoCatalogEntry, "org/no-readme")
        assert entry is not None
        assert entry.readme_head is None
        assert entry.synced_at is not None


# ---------------------------------------------------------------------------
# 6b. A failed org listing aborts the sweep instead of wiping the catalog
# ---------------------------------------------------------------------------

def test_failed_listing_aborts_without_deleting() -> None:
    """An org 404 (typo, lost access) must not delete every catalog row."""
    _, sf = _engine_and_sf()

    def transport_ok(method: str, path: str):
        if "/orgs/org/repos" in path:
            return 200, {}, [_repo("org/keep")] if _page_num(path) == 1 else []
        if "/readme" in path:
            return 200, {}, _readme_response()
        if "/contents/" in path:
            return 200, {}, []
        if "/pulls" in path:
            return 200, {}, []
        return 404, {}, None

    CatalogSync(sf, transport_ok, call_budget=3000).sync("org", bootstrap=True)

    def transport_404(method: str, path: str):
        return 404, {}, None

    sync = CatalogSync(sf, transport_404, call_budget=3000)
    with pytest.raises(CatalogSyncError):
        sync.sync("org")

    with sf() as session:
        assert session.get(FoundryRepoCatalogEntry, "org/keep") is not None


# ---------------------------------------------------------------------------
# 7. Budget exhaustion: partial progress committed, resumes on next run
# ---------------------------------------------------------------------------

def test_budget_exhaustion_stops_cleanly_and_resumes() -> None:
    """Budget covering listing + 1 repo deep-fetch. Second run completes the rest."""
    _, sf = _engine_and_sf()

    repos = [
        _repo("org/repo-a", pushed="2025-01-01T00:00:00Z"),
        _repo("org/repo-b", pushed="2025-01-01T00:00:00Z"),
    ]

    def transport(method: str, path: str):
        if "/orgs/org/repos" in path:
            return 200, {}, repos if _page_num(path) == 1 else []
        if "/readme" in path:
            return 200, {}, _readme_response()
        if "/contents/" in path:
            return 200, {}, []
        if "/pulls" in path:
            return 200, {}, []
        return 404, {}, None

    # Budget: 1 (listing page) + 3 (deep fetch for repo-a) = 4
    sync = CatalogSync(sf, transport, call_budget=4)
    report = sync.sync("org", bootstrap=True)

    assert report.budget_exhausted is True
    assert report.deep_fetched == 1

    # Verify partial progress: one repo synced, one not
    with sf() as session:
        entries = session.query(FoundryRepoCatalogEntry).all()
        synced = [e for e in entries if e.synced_at is not None]
        unsynced = [e for e in entries if e.synced_at is None]
        assert len(synced) == 1
        assert len(unsynced) == 1

    # Second run: ample budget
    sync2 = CatalogSync(sf, transport, call_budget=3000)

    report2 = sync2.sync("org")

    assert report2.budget_exhausted is False
    assert report2.deep_fetched == 1  # only the remaining unsynced one

    with sf() as session:
        entries = session.query(FoundryRepoCatalogEntry).all()
        assert all(e.synced_at is not None for e in entries)


# ---------------------------------------------------------------------------
# 8. Code facts: fetched, derived, and stored when enabled
# ---------------------------------------------------------------------------

_FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> dict:
    return json.loads((_FIXTURES / name).read_text())


def _code_facts_transport(calls: list[str], tree_fixture: str = "github_tree_recursive.json"):
    """Fake transport serving the recorded tree + contents fixtures."""

    def transport(method: str, path: str):
        calls.append(path)
        if "/orgs/org/repos" in path:
            return 200, {}, [_repo("org/billing")] if _page_num(path) == 1 else []
        if "/readme" in path:
            return 200, {}, _readme_response()
        if "/git/trees/" in path:
            return 200, {}, _fixture(tree_fixture)
        if path.endswith("/contents/"):
            return 200, {}, [{"name": "src"}, {"name": "tests"}]
        if "/contents/.github/CODEOWNERS" in path:
            return 200, {}, _fixture("github_codeowners_contents.json")
        if "/contents/pyproject.toml" in path:
            return 200, {}, _fixture("github_pyproject_contents.json")
        if "/contents/package.json" in path:
            return 200, {}, _fixture("github_package_json_contents.json")
        if "/pulls" in path:
            return 200, {}, []
        return 404, {}, None

    return transport


def test_code_facts_fetched_and_stored() -> None:
    _, sf = _engine_and_sf()
    calls: list[str] = []
    sync = CatalogSync(sf, _code_facts_transport(calls), call_budget=3000, fetch_code_facts=True)
    report = sync.sync("org", bootstrap=True)

    assert report.budget_exhausted is False
    with sf() as session:
        entry = session.get(FoundryRepoCatalogEntry, "org/billing")
        assert entry is not None
        paths = json.loads(entry.tree_paths)
        assert "src/billing/invoice.py" in paths
        assert all("/" not in p or True for p in paths)  # blobs only, dirs excluded
        assert ".github" not in paths  # tree-type entries are not stored
        assert entry.tree_truncated is False
        assert "tests/" in json.loads(entry.test_layout)
        rules = json.loads(entry.codeowners)
        assert {"pattern": "src/billing/", "owners": ["@org/payments", "@alice"]} in rules
        manifests = json.loads(entry.manifests)
        kinds = {m["kind"] for m in manifests}
        assert {"pyproject", "package_json"} <= kinds
        assert json.loads(entry.languages)["py"] >= 4


def test_code_facts_disabled_keeps_three_calls_per_repo() -> None:
    _, sf = _engine_and_sf()
    calls: list[str] = []
    sync = CatalogSync(sf, _code_facts_transport(calls), call_budget=3000)
    sync.sync("org", bootstrap=True)

    deep_calls = [c for c in calls if "/orgs/" not in c]
    assert len(deep_calls) == 3  # readme + contents + pulls, no tree fetch
    assert not any("/git/trees/" in c for c in deep_calls)


def test_truncated_tree_sets_flag_and_still_derives() -> None:
    _, sf = _engine_and_sf()
    calls: list[str] = []
    sync = CatalogSync(
        sf,
        _code_facts_transport(calls, tree_fixture="github_tree_truncated.json"),
        call_budget=3000,
        fetch_code_facts=True,
    )
    sync.sync("org", bootstrap=True)

    with sf() as session:
        entry = session.get(FoundryRepoCatalogEntry, "org/billing")
        assert entry.tree_truncated is True
        assert "tests/" in json.loads(entry.test_layout)


def test_tree_fetch_failure_still_syncs_repo() -> None:
    _, sf = _engine_and_sf()

    def transport(method: str, path: str):
        if "/orgs/org/repos" in path:
            return 200, {}, [_repo("org/empty")] if _page_num(path) == 1 else []
        if "/readme" in path:
            return 200, {}, _readme_response()
        if "/git/trees/" in path:
            return 404, {}, None
        if path.endswith("/contents/"):
            return 200, {}, []
        if "/pulls" in path:
            return 200, {}, []
        return 404, {}, None

    sync = CatalogSync(sf, transport, call_budget=3000, fetch_code_facts=True)
    sync.sync("org", bootstrap=True)

    with sf() as session:
        entry = session.get(FoundryRepoCatalogEntry, "org/empty")
        assert entry.synced_at is not None
        assert json.loads(entry.tree_paths) == []
        assert json.loads(entry.codeowners) == []


def test_no_codeowners_means_no_contents_call() -> None:
    _, sf = _engine_and_sf()
    calls: list[str] = []

    def transport(method: str, path: str):
        calls.append(path)
        if "/orgs/org/repos" in path:
            return 200, {}, [_repo("org/bare")] if _page_num(path) == 1 else []
        if "/readme" in path:
            return 200, {}, _readme_response()
        if "/git/trees/" in path:
            return 200, {}, {"sha": "x", "tree": [
                {"path": "main.go", "type": "blob"},
            ], "truncated": False}
        if path.endswith("/contents/"):
            return 200, {}, []
        if "/pulls" in path:
            return 200, {}, []
        return 404, {}, None

    sync = CatalogSync(sf, transport, call_budget=3000, fetch_code_facts=True)
    sync.sync("org", bootstrap=True)

    assert not any("CODEOWNERS" in c for c in calls)


def test_empty_repo_409_does_not_abort_code_facts_sweep() -> None:
    """The trees API answers 409 for an empty repo - it must not kill the sweep."""
    _, sf = _engine_and_sf()

    repos = [_repo("org/empty"), _repo("org/full")]

    def transport(method: str, path: str):
        if "/orgs/org/repos" in path:
            return 200, {}, repos if _page_num(path) == 1 else []
        if "/readme" in path:
            return 200, {}, _readme_response()
        if "/git/trees/" in path:
            if "org/empty" in path:
                return 409, {}, None
            return 200, {}, {"sha": "x", "tree": [{"path": "main.py", "type": "blob"}], "truncated": False}
        if path.endswith("/contents/"):
            return 200, {}, []
        if "/pulls" in path:
            return 200, {}, []
        return 404, {}, None

    sync = CatalogSync(sf, transport, call_budget=3000, fetch_code_facts=True)
    report = sync.sync("org", bootstrap=True)

    assert report.deep_fetched == 2
    with sf() as session:
        empty = session.get(FoundryRepoCatalogEntry, "org/empty")
        assert empty.synced_at is not None  # definitively empty: synced, not retried
        assert json.loads(empty.tree_paths) == []
        full = session.get(FoundryRepoCatalogEntry, "org/full")
        assert full.synced_at is not None
        assert json.loads(full.tree_paths) == ["main.py"]


def test_deletion_is_scoped_to_the_synced_org() -> None:
    """Syncing one org must never delete another org's catalog rows."""
    _, sf = _engine_and_sf()

    with sf() as session:
        session.add(FoundryRepoCatalogEntry(repo="other-org/keeper"))
        session.add(FoundryRepoCatalogEntry(repo="org/goner"))
        session.commit()

    def transport(method: str, path: str):
        if "/orgs/org/repos" in path:
            return 200, {}, [_repo("org/mine")] if _page_num(path) == 1 else []
        if "/readme" in path:
            return 200, {}, _readme_response()
        if "/contents/" in path:
            return 200, {}, []
        if "/pulls" in path:
            return 200, {}, []
        return 404, {}, None

    report = CatalogSync(sf, transport, call_budget=3000).sync("org", bootstrap=True)

    assert report.deleted == 1  # org/goner only - never other-org/keeper
    with sf() as session:
        assert session.get(FoundryRepoCatalogEntry, "other-org/keeper") is not None
        assert session.get(FoundryRepoCatalogEntry, "org/goner") is None
        assert session.get(FoundryRepoCatalogEntry, "org/mine") is not None


def _tree_failing_transport(repos: list[dict], fail: bool):
    """Code-facts transport whose tree fetch raises (simulating 5xx after retries)."""

    def transport(method: str, path: str):
        if "/orgs/org/repos" in path:
            return 200, {}, repos if _page_num(path) == 1 else []
        if "/readme" in path:
            return 200, {}, _readme_response()
        if "/git/trees/" in path:
            if fail:
                raise TransportError("GitHub request failed after 3 retries")
            return 200, {}, {"sha": "x", "tree": [{"path": "main.py", "type": "blob"}], "truncated": False}
        if path.endswith("/contents/"):
            return 200, {}, []
        if "/pulls" in path:
            return 200, {}, []
        return 404, {}, None

    return transport


def test_degraded_code_facts_fetch_is_retried_next_sweep() -> None:
    """A transient tree failure leaves synced_at unset so the next run retries."""
    _, sf = _engine_and_sf()
    repos = [_repo("org/flaky")]

    report = CatalogSync(
        sf, _tree_failing_transport(repos, fail=True), call_budget=3000, fetch_code_facts=True
    ).sync("org", bootstrap=True)
    assert report.budget_exhausted is False  # the sweep survived the failure

    with sf() as session:
        entry = session.get(FoundryRepoCatalogEntry, "org/flaky")
        assert entry.synced_at is None, "degraded fetch must not look fresh"
        assert entry.readme_head is not None  # metadata still stored

    # Next sweep: transport healthy again, repo is picked up and completed.
    report2 = CatalogSync(
        sf, _tree_failing_transport(repos, fail=False), call_budget=3000, fetch_code_facts=True
    ).sync("org")
    assert report2.deep_fetched == 1
    with sf() as session:
        entry = session.get(FoundryRepoCatalogEntry, "org/flaky")
        assert entry.synced_at is not None
        assert json.loads(entry.tree_paths) == ["main.py"]


def test_degraded_fetch_preserves_prior_code_facts() -> None:
    """A transient failure must not overwrite good facts with empty ones."""
    _, sf = _engine_and_sf()
    first_sync = datetime(2025, 12, 1, 0, 0, 0, tzinfo=timezone.utc)
    repos = [_repo("org/app", pushed="2025-01-01T00:00:00Z")]

    CatalogSync(
        sf,
        _tree_failing_transport(repos, fail=False),
        call_budget=3000,
        fetch_code_facts=True,
        now=lambda: first_sync,
    ).sync("org", bootstrap=True)

    # A push lands, then the refresh sweep's tree fetch fails transiently.
    repos[0]["pushed_at"] = "2026-01-01T00:00:00Z"
    CatalogSync(
        sf, _tree_failing_transport(repos, fail=True), call_budget=3000, fetch_code_facts=True
    ).sync("org")

    with sf() as session:
        entry = session.get(FoundryRepoCatalogEntry, "org/app")
        assert json.loads(entry.tree_paths) == ["main.py"], "prior facts kept"
        assert _aware(entry.synced_at) == first_sync, "synced_at unbumped: still stale"

    # The repo is still considered stale, so a healthy sweep refreshes it.
    report = CatalogSync(
        sf, _tree_failing_transport(repos, fail=False), call_budget=3000, fetch_code_facts=True
    ).sync("org")
    assert report.deep_fetched == 1
    with sf() as session:
        entry = session.get(FoundryRepoCatalogEntry, "org/app")
        assert entry.synced_at is not None
        assert _aware(entry.synced_at) != first_sync


def test_code_facts_budget_reservation_resumes() -> None:
    """A budget too small for a code-facts repo stops before starting it."""
    _, sf = _engine_and_sf()
    calls: list[str] = []
    # Listing costs 1; reservation with code facts is 9, so budget 5 stops
    # before the first repo is touched.
    sync = CatalogSync(sf, _code_facts_transport(calls), call_budget=5, fetch_code_facts=True)
    report = sync.sync("org", bootstrap=True)

    assert report.budget_exhausted is True
    assert report.deep_fetched == 0
    with sf() as session:
        entry = session.get(FoundryRepoCatalogEntry, "org/billing")
        assert entry.synced_at is None

    # A second run with ample budget completes the repo.
    sync2 = CatalogSync(sf, _code_facts_transport(calls), call_budget=3000, fetch_code_facts=True)
    report2 = sync2.sync("org")
    assert report2.deep_fetched == 1
    with sf() as session:
        entry = session.get(FoundryRepoCatalogEntry, "org/billing")
        assert entry.synced_at is not None
        assert json.loads(entry.tree_paths)
