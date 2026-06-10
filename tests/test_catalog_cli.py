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
