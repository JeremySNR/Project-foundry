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
        assert path == "/repos/o/customer-web/pulls/7/files"
        return [{"filename": "src/a.ts"}, {"filename": "migrations/x.sql"}]

    state = GitHubConnector(transport=transport).pr_state_from_event(
        "pull_request", _pr_payload()
    )
    assert state.files_changed == ["src/a.ts", "migrations/x.sql"]


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


def test_unhandled_event_returns_none() -> None:
    assert GitHubConnector().pr_state_from_event("push", {}) is None
