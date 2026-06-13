"""Tests for the GitHub connector event mapping."""

from __future__ import annotations

from foundry.connectors import GitHubConnector
from foundry.schemas.common import CIStatus, PRStatus, ReviewStatus


def _pr_payload(**pr_overrides) -> dict:
    pr = {
        "number": 7,
        "html_url": "https://github.com/o/customer-web/pull/7",
        "head": {"ref": "foundry/lin-123-favourites"},
        "state": "open",
        "draft": False,
        "merged": False,
    }
    pr.update(pr_overrides)
    return {"pull_request": pr, "repository": {"full_name": "o/customer-web"}}


def test_pull_request_open_maps_to_state() -> None:
    state = GitHubConnector().pr_state_from_event("pull_request", _pr_payload())
    assert state is not None
    assert state.repo == "o/customer-web"
    assert state.pr_number == 7
    assert state.branch == "foundry/lin-123-favourites"
    assert state.status is PRStatus.OPEN


def test_draft_and_merged_status() -> None:
    gh = GitHubConnector()
    assert gh.pr_state_from_event("pull_request", _pr_payload(draft=True)).status is PRStatus.DRAFT
    assert (
        gh.pr_state_from_event("pull_request", _pr_payload(merged=True)).status
        is PRStatus.MERGED
    )


def test_files_enriched_via_transport() -> None:
    def transport(method: str, path: str):
        assert method == "GET"
        assert path == "/repos/o/customer-web/pulls/7/files?per_page=100&page=1"
        return [{"filename": "src/a.ts"}, {"filename": "migrations/x.sql"}]

    state = GitHubConnector(transport=transport).pr_state_from_event(
        "pull_request", _pr_payload()
    )
    assert state.files_changed == ["src/a.ts", "migrations/x.sql"]


def test_list_pr_files_paginates_until_short_page() -> None:
    """A forbidden file beyond the first page must still reach the gate."""
    pages = {
        1: [{"filename": f"src/file_{i}.py"} for i in range(100)],
        2: [{"filename": f"src/more_{i}.py"} for i in range(49)]
        + [{"filename": "migrations/0042.py"}],
    }
    requested: list[str] = []

    def transport(method: str, path: str):
        requested.append(path)
        page = int(path.rsplit("page=", 1)[1])
        return pages[page]

    files = GitHubConnector(transport=transport).list_pr_files("o/customer-web", 7)
    assert len(files) == 150
    assert "migrations/0042.py" in files
    assert requested == [
        "/repos/o/customer-web/pulls/7/files?per_page=100&page=1",
        "/repos/o/customer-web/pulls/7/files?per_page=100&page=2",
    ]


def test_list_pr_files_stops_after_exact_page_boundary() -> None:
    """Exactly 100 files: one follow-up request returning empty, then stop."""
    pages = {
        1: [{"filename": f"src/file_{i}.py"} for i in range(100)],
        2: [],
    }

    def transport(method: str, path: str):
        return pages[int(path.rsplit("page=", 1)[1])]

    files = GitHubConnector(transport=transport).list_pr_files("o/customer-web", 7)
    assert len(files) == 100


def test_bot_review_maps_to_bot_reviewed() -> None:
    payload = _pr_payload()
    payload["review"] = {"state": "commented", "user": {"type": "Bot", "login": "coderabbitai"}}
    state = GitHubConnector().pr_state_from_event("pull_request_review", payload)
    assert state.review_status is ReviewStatus.BOT_REVIEWED


def test_human_changes_requested_review() -> None:
    payload = _pr_payload()
    payload["review"] = {"state": "changes_requested", "user": {"type": "User"}}
    state = GitHubConnector().pr_state_from_event("pull_request_review", payload)
    assert state.review_status is ReviewStatus.CHANGES_REQUESTED


def test_dismissed_review_is_ignored() -> None:
    """A dismissed review must not read as a completed (human/bot) review."""
    payload = _pr_payload()
    payload["action"] = "dismissed"
    payload["review"] = {"state": "dismissed", "user": {"type": "User"}}
    assert (
        GitHubConnector().pr_state_from_event("pull_request_review", payload) is None
    )


def test_submitted_action_still_maps() -> None:
    payload = _pr_payload()
    payload["action"] = "submitted"
    payload["review"] = {"state": "approved", "user": {"type": "User"}}
    state = GitHubConnector().pr_state_from_event("pull_request_review", payload)
    assert state.review_status is ReviewStatus.APPROVED


def test_check_suite_maps_ci_status() -> None:
    payload = {
        "check_suite": {
            "conclusion": "failure",
            "pull_requests": [
                {"number": 7, "url": "https://api/pull/7", "head": {"ref": "foundry/lin-123"}}
            ],
        },
        "repository": {"full_name": "o/customer-web"},
    }
    state = GitHubConnector().pr_state_from_event("check_suite", payload)
    assert state.ci_status is CIStatus.FAILING


def test_check_suite_fork_pr_keeps_ci_via_head_branch() -> None:
    """Fork PRs arrive with an empty pull_requests list; CI must still map."""
    payload = {
        "check_suite": {
            "conclusion": "failure",
            "head_branch": "foundry/lin-123-favourites",
            "pull_requests": [],
        },
        "repository": {"full_name": "o/customer-web"},
    }
    state = GitHubConnector().pr_state_from_event("check_suite", payload)
    assert state is not None
    assert state.ci_status is CIStatus.FAILING
    assert state.branch == "foundry/lin-123-favourites"


def test_check_suite_with_no_prs_and_no_branch_is_ignored() -> None:
    payload = {
        "check_suite": {"conclusion": "success", "pull_requests": []},
        "repository": {"full_name": "o/customer-web"},
    }
    assert GitHubConnector().pr_state_from_event("check_suite", payload) is None


def test_check_suite_picks_pr_matching_suite_head() -> None:
    """With multiple linked PRs, the one matching the suite head wins, not [0]."""
    payload = {
        "check_suite": {
            "conclusion": "success",
            "head_branch": "feature-b",
            "pull_requests": [
                {"number": 1, "url": "u1", "head": {"ref": "feature-a"}},
                {"number": 2, "url": "u2", "head": {"ref": "feature-b"}},
            ],
        },
        "repository": {"full_name": "o/customer-web"},
    }
    state = GitHubConnector().pr_state_from_event("check_suite", payload)
    assert state.pr_number == 2
    assert state.branch == "feature-b"


def test_unhandled_event_returns_none() -> None:
    assert GitHubConnector().pr_state_from_event("push", {}) is None


def test_failing_check_summaries_collects_failures() -> None:
    def transport(method, path):
        assert path == (
            "/repos/o/customer-web/check-suites/99/check-runs?per_page=100&page=1"
        )
        return {
            "check_runs": [
                {
                    "name": "unit tests",
                    "conclusion": "failure",
                    "output": {"summary": "2 tests failed in tests/test_x.py"},
                },
                {"name": "lint", "conclusion": "success", "output": {}},
                {"name": "build", "conclusion": "timed_out", "output": {}},
            ]
        }

    summary = GitHubConnector(transport=transport).failing_check_summaries(
        "o/customer-web", 99
    )
    assert "unit tests: 2 tests failed" in summary
    assert "build" in summary
    assert "lint" not in summary


def test_check_suite_failure_attaches_summaries_via_transport() -> None:
    def transport(method, path):
        return {
            "check_runs": [
                {"name": "pytest", "conclusion": "failure", "output": {"summary": "boom"}}
            ]
        }

    payload = {
        "check_suite": {
            "id": 5,
            "conclusion": "failure",
            "pull_requests": [
                {"number": 7, "url": "u", "head": {"ref": "foundry/lin-123"}}
            ],
        },
        "repository": {"full_name": "o/customer-web"},
    }
    state = GitHubConnector(transport=transport).pr_state_from_event(
        "check_suite", payload
    )
    assert state.ci_status is CIStatus.FAILING
    assert "pytest: boom" in state.summary


def test_failing_check_summaries_empty_without_transport() -> None:
    assert GitHubConnector().failing_check_summaries("o/r", 1) == ""


def test_failing_check_summaries_paginates_until_short_page() -> None:
    """A failing check beyond the first page still reaches remediation context."""
    pages = {
        1: {
            "check_runs": [
                {"name": f"check-{i}", "conclusion": "success", "output": {}}
                for i in range(100)
            ]
        },
        2: {
            "check_runs": [
                {
                    "name": "slow suite",
                    "conclusion": "failure",
                    "output": {"summary": "3 tests failed"},
                }
            ]
        },
    }
    requested: list[str] = []

    def transport(method, path):
        requested.append(path)
        return pages[int(path.rsplit("page=", 1)[1])]

    summary = GitHubConnector(transport=transport).failing_check_summaries(
        "o/customer-web", 99
    )
    assert "slow suite: 3 tests failed" in summary
    assert requested == [
        "/repos/o/customer-web/check-suites/99/check-runs?per_page=100&page=1",
        "/repos/o/customer-web/check-suites/99/check-runs?per_page=100&page=2",
    ]
