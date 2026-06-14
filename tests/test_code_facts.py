"""Unit tests for catalog code-fact derivation - pure functions, no I/O."""

from __future__ import annotations

import base64
import json
from pathlib import Path

from foundry.catalog.code_facts import (
    MAX_MANIFEST_FETCHES,
    cap_tree_paths,
    derive_conventions,
    derive_languages,
    derive_test_layout,
    find_codeowners_path,
    find_manifest_paths,
    infer_test_commands,
    parse_codeowners,
    parse_manifest,
)

_FIXTURES = Path(__file__).parent / "fixtures"


def _fixture_text(name: str) -> str:
    payload = json.loads((_FIXTURES / name).read_text())
    return base64.b64decode(payload["content"]).decode()


# ---------------------------------------------------------------------------
# Tree path capping
# ---------------------------------------------------------------------------

def test_cap_tree_paths_keeps_root_files_first() -> None:
    paths = [f"src/deep/nested/file{i}.py" for i in range(10)] + ["README.md", "go.mod"]
    capped, was_capped = cap_tree_paths(paths, 5)
    assert was_capped is True
    assert len(capped) == 5
    assert "README.md" in capped and "go.mod" in capped


def test_cap_tree_paths_no_cap_needed() -> None:
    capped, was_capped = cap_tree_paths(["b.py", "a.py"], 10)
    assert was_capped is False
    assert capped == ["a.py", "b.py"]


# ---------------------------------------------------------------------------
# Test layout
# ---------------------------------------------------------------------------

def test_derive_test_layout_detects_dirs_and_patterns() -> None:
    paths = [
        "tests/test_invoice.py",
        "tests/conftest.py",
        "src/pkg/__tests__/widget.test.ts",
        "internal/db/db_test.go",
        "spec/models/user_spec.rb",
    ]
    layout = derive_test_layout(paths)
    assert "tests/" in layout
    assert "src/pkg/__tests__/" in layout
    assert "test_*.py" in layout
    assert "conftest.py" in layout
    assert "*.test.ts" in layout
    assert "*_test.go" in layout
    assert "*_spec.rb" in layout


def test_derive_test_layout_empty_tree() -> None:
    assert derive_test_layout([]) == []


# ---------------------------------------------------------------------------
# Languages and conventions
# ---------------------------------------------------------------------------

def test_derive_languages_histogram() -> None:
    paths = ["a.py", "b.py", "c.ts", "Makefile", ".gitignore", "src/d.py"]
    languages = derive_languages(paths)
    assert languages["py"] == 3
    assert languages["ts"] == 1
    assert "gitignore" not in languages  # dotfiles are not extensions


def test_derive_conventions_markers() -> None:
    paths = [
        ".github/workflows/ci.yml",
        "Dockerfile",
        "tsconfig.json",
        ".pre-commit-config.yaml",
        "src/main.ts",
    ]
    conventions = derive_conventions(paths)
    assert "GitHub Actions CI" in conventions
    assert "Dockerfile" in conventions
    assert "TypeScript" in conventions
    assert "pre-commit hooks" in conventions
    assert "GitLab CI" not in conventions


# ---------------------------------------------------------------------------
# CODEOWNERS
# ---------------------------------------------------------------------------

def test_find_codeowners_path_precedence() -> None:
    assert find_codeowners_path([".github/CODEOWNERS", "CODEOWNERS"]) == ".github/CODEOWNERS"
    assert find_codeowners_path(["CODEOWNERS", "docs/CODEOWNERS"]) == "CODEOWNERS"
    assert find_codeowners_path(["docs/CODEOWNERS"]) == "docs/CODEOWNERS"
    assert find_codeowners_path(["src/main.py"]) is None


def test_parse_codeowners_fixture() -> None:
    rules = parse_codeowners(_fixture_text("github_codeowners_contents.json"))
    assert {"pattern": "*", "owners": ["@org/platform"]} in rules
    assert {"pattern": "src/billing/", "owners": ["@org/payments", "@alice"]} in rules
    # Comments and blank lines are skipped
    assert all(not str(r["pattern"]).startswith("#") for r in rules)


def test_parse_codeowners_caps_rules() -> None:
    text = "\n".join(f"/path{i}/ @owner" for i in range(500))
    assert len(parse_codeowners(text)) == 200


# ---------------------------------------------------------------------------
# Manifests
# ---------------------------------------------------------------------------

def test_find_manifest_paths_root_only_and_capped() -> None:
    paths = [
        "pyproject.toml",
        "package.json",
        "go.mod",
        "Cargo.toml",
        "Gemfile",
        "sub/package.json",  # not root level
    ]
    found = find_manifest_paths(paths)
    assert len(found) == MAX_MANIFEST_FETCHES
    assert "sub/package.json" not in found
    assert found[0] == "pyproject.toml"


def test_parse_pyproject_fixture() -> None:
    manifest = parse_manifest("pyproject.toml", _fixture_text("github_pyproject_contents.json"))
    assert manifest["kind"] == "pyproject"
    assert "fastapi" in manifest["dependencies"]
    assert "stripe" in manifest["dependencies"]
    assert manifest["test_command"] == "pytest"


def test_parse_package_json_fixture() -> None:
    manifest = parse_manifest("package.json", _fixture_text("github_package_json_contents.json"))
    assert manifest["kind"] == "package_json"
    assert "react" in manifest["dependencies"]
    assert manifest["test_command"] == "npm test"


def test_parse_go_mod() -> None:
    text = "module example.com/svc\n\ngo 1.22\n\nrequire (\n\tgithub.com/lib/pq v1.10.9\n)\n"
    manifest = parse_manifest("go.mod", text)
    assert manifest["kind"] == "go_mod"
    assert "github.com/lib/pq" in manifest["dependencies"]
    assert manifest["test_command"] == "go test ./..."


def test_parse_requirements_txt() -> None:
    manifest = parse_manifest("requirements.txt", "requests>=2.31\n# comment\n-r other.txt\nflask\n")
    assert manifest["dependencies"] == ["requests", "flask"]


def test_parse_manifest_malformed_never_raises() -> None:
    for path in ("pyproject.toml", "package.json", "Cargo.toml", "composer.json"):
        manifest = parse_manifest(path, "{{{ not valid [[[")
        assert manifest["dependencies"] == []
        assert manifest["test_command"] is None


def test_parse_manifest_dependency_cap() -> None:
    data = json.dumps({"dependencies": {f"pkg{i:03d}": "1.0" for i in range(80)}})
    manifest = parse_manifest("package.json", data)
    assert len(manifest["dependencies"]) == 50


# ---------------------------------------------------------------------------
# Test-command inference
# ---------------------------------------------------------------------------

def test_infer_test_commands_dedupes() -> None:
    manifests = [
        {"path": "pyproject.toml", "kind": "pyproject", "dependencies": [], "test_command": "pytest"},
        {"path": "go.mod", "kind": "go_mod", "dependencies": [], "test_command": "go test ./..."},
        {"path": "other.toml", "kind": "pyproject", "dependencies": [], "test_command": "pytest"},
        {"path": "Gemfile", "kind": "gemfile", "dependencies": [], "test_command": None},
    ]
    assert infer_test_commands(manifests) == ["pytest", "go test ./..."]
