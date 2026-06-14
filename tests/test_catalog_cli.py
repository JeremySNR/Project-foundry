"""Tests for the foundry-catalog CLI exit paths - offline, no network."""

from __future__ import annotations

import pytest

from foundry.catalog.cli import main

_FOUNDRY_ENV_VARS = [
    "FOUNDRY_CONFIG",
    "FOUNDRY_CONTEXT_ORG",
    "FOUNDRY_GITHUB_API_TOKEN",
]


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _FOUNDRY_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def test_sync_without_org_exits_2(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setattr("sys.argv", ["foundry-catalog", "sync"])

    with pytest.raises(SystemExit) as excinfo:
        main()

    assert excinfo.value.code == 2
    assert "no org specified" in capsys.readouterr().err


def test_sync_without_token_exits_2(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setattr("sys.argv", ["foundry-catalog", "sync", "--org", "acme"])

    with pytest.raises(SystemExit) as excinfo:
        main()

    assert excinfo.value.code == 2
    assert "FOUNDRY_GITHUB_API_TOKEN" in capsys.readouterr().err


class _FakeSync:
    """Records constructor kwargs; returns an empty report."""

    last_kwargs: dict | None = None

    def __init__(self, session_factory, transport, **kwargs):
        type(self).last_kwargs = kwargs

    def sync(self, org, *, bootstrap=False):
        from foundry.catalog.sync import SyncReport

        return SyncReport(
            repos_listed=0, deep_fetched=0, deleted=0, calls_used=0,
            budget_exhausted=False,
        )


def _run_cli(monkeypatch: pytest.MonkeyPatch, argv: list[str], env: dict[str, str]) -> None:
    _clear_env(monkeypatch)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("FOUNDRY_DATABASE_URL", "sqlite+pysqlite:///:memory:")
    monkeypatch.setattr("foundry.catalog.sync.CatalogSync", _FakeSync)
    monkeypatch.setattr("sys.argv", ["foundry-catalog", *argv])
    with pytest.raises(SystemExit) as excinfo:
        main()
    assert excinfo.value.code == 0


def test_sync_code_facts_flag_enables_fetch(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    _run_cli(
        monkeypatch,
        ["sync", "--org", "acme", "--code-facts"],
        {"FOUNDRY_GITHUB_API_TOKEN": "t"},
    )
    assert _FakeSync.last_kwargs["fetch_code_facts"] is True
    assert _FakeSync.last_kwargs["tree_max_paths"] == 2000


def test_sync_code_facts_off_by_default(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    _run_cli(
        monkeypatch,
        ["sync", "--org", "acme"],
        {"FOUNDRY_GITHUB_API_TOKEN": "t"},
    )
    assert _FakeSync.last_kwargs["fetch_code_facts"] is False


def test_sync_code_facts_implied_by_code_provider(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    _run_cli(
        monkeypatch,
        ["sync", "--org", "acme"],
        {"FOUNDRY_GITHUB_API_TOKEN": "t", "FOUNDRY_CONTEXT_PROVIDER": "code"},
    )
    assert _FakeSync.last_kwargs["fetch_code_facts"] is True
