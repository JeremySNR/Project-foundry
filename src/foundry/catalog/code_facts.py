"""Pure derivation of code facts from a repository file tree.

Everything in this module is a deterministic function of tree paths or file
text - no I/O, no DB, no transport - so it is unit-testable offline and the
sync layer stays thin. The sync fetches the tree (one Git Trees API call) and
the contents of CODEOWNERS plus a small allowlist of root manifests; this
module turns those into the facts the enricher and downstream engines consume.
"""

from __future__ import annotations

import json
import re
import tomllib
from collections import Counter

# Root-level manifests we are willing to fetch contents for, in priority order.
MANIFEST_ALLOWLIST: tuple[str, ...] = (
    "pyproject.toml",
    "package.json",
    "go.mod",
    "Cargo.toml",
    "pom.xml",
    "Gemfile",
    "composer.json",
    "requirements.txt",
)
MAX_MANIFEST_FETCHES = 4

# CODEOWNERS conventional locations, in GitHub's precedence order.
_CODEOWNERS_LOCATIONS = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")

_MAX_CODEOWNERS_RULES = 200
_MAX_DEPENDENCIES = 50
_MAX_LANGUAGES = 10

# Directory names (any depth) that signal where tests live.
_TEST_DIR_NAMES = frozenset({"tests", "test", "__tests__", "spec", "specs"})

# Filename suffix patterns that signal a test convention.
_TEST_FILE_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"_test\.go$", "*_test.go"),
    (r"\.spec\.[jt]sx?$", "*.spec.ts"),
    (r"\.test\.[jt]sx?$", "*.test.ts"),
    (r"_spec\.rb$", "*_spec.rb"),
    (r"^test_[^/]+\.py$", "test_*.py"),
    (r"Tests?\.cs$", "*Tests.cs"),
    (r"Test\.java$", "*Test.java"),
)

# Marker path -> convention label. Matched against exact root paths or prefixes.
_CONVENTION_MARKERS: tuple[tuple[str, str], ...] = (
    (".github/workflows/", "GitHub Actions CI"),
    (".gitlab-ci.yml", "GitLab CI"),
    ("ruff.toml", "ruff configured"),
    (".ruff.toml", "ruff configured"),
    (".pre-commit-config.yaml", "pre-commit hooks"),
    ("Dockerfile", "Dockerfile"),
    ("docker-compose.yaml", "docker compose"),
    ("docker-compose.yml", "docker compose"),
    ("tsconfig.json", "TypeScript"),
    ("tox.ini", "tox"),
    ("Makefile", "Makefile"),
    (".editorconfig", "editorconfig"),
)


def cap_tree_paths(paths: list[str], limit: int) -> tuple[list[str], bool]:
    """Cap a path list for storage: shallowest first, then lexicographic.

    Root files always survive the cap. Returns ``(capped, was_capped)``.
    """
    if len(paths) <= limit:
        return sorted(paths, key=lambda p: (p.count("/"), p)), False
    ordered = sorted(paths, key=lambda p: (p.count("/"), p))
    return ordered[:limit], True


def derive_test_layout(paths: list[str]) -> list[str]:
    """Detect test directories and test-file naming conventions from paths."""
    layout: list[str] = []
    seen_dirs: set[str] = set()
    seen_patterns: set[str] = set()
    for path in paths:
        parts = path.split("/")
        for i, part in enumerate(parts[:-1]):
            if part in _TEST_DIR_NAMES:
                dir_path = "/".join(parts[: i + 1]) + "/"
                if dir_path not in seen_dirs:
                    seen_dirs.add(dir_path)
        filename = parts[-1]
        for pattern, label in _TEST_FILE_PATTERNS:
            if label not in seen_patterns and re.search(pattern, filename):
                seen_patterns.add(label)
        if filename == "conftest.py" and "conftest.py" not in seen_patterns:
            seen_patterns.add("conftest.py")
    # Shallow dirs first: "tests/" is more useful than "src/pkg/sub/tests/".
    layout.extend(sorted(seen_dirs, key=lambda d: (d.count("/"), d))[:10])
    layout.extend(sorted(seen_patterns))
    return layout


def derive_languages(paths: list[str]) -> dict[str, int]:
    """Extension histogram of the tree, top ``_MAX_LANGUAGES`` entries."""
    counts: Counter[str] = Counter()
    for path in paths:
        filename = path.rsplit("/", 1)[-1]
        if "." not in filename or filename.startswith("."):
            continue
        ext = filename.rsplit(".", 1)[-1].lower()
        if ext and len(ext) <= 12:
            counts[ext] += 1
    return dict(counts.most_common(_MAX_LANGUAGES))


def derive_conventions(paths: list[str]) -> list[str]:
    """Detect repo conventions (CI, linting, containers...) from marker paths."""
    path_set = set(paths)
    conventions: list[str] = []
    for marker, label in _CONVENTION_MARKERS:
        if label in conventions:
            continue
        if marker.endswith("/"):
            if any(p.startswith(marker) for p in paths):
                conventions.append(label)
        elif marker in path_set:
            conventions.append(label)
    return conventions


def find_codeowners_path(paths: list[str]) -> str | None:
    """The CODEOWNERS path to fetch, honouring GitHub's location precedence."""
    path_set = set(paths)
    for location in _CODEOWNERS_LOCATIONS:
        if location in path_set:
            return location
    return None


def parse_codeowners(text: str) -> list[dict[str, object]]:
    """Parse CODEOWNERS text into ``{pattern, owners}`` rules (capped)."""
    rules: list[dict[str, object]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        rules.append({"pattern": parts[0], "owners": parts[1:]})
        if len(rules) >= _MAX_CODEOWNERS_RULES:
            break
    return rules


def find_manifest_paths(paths: list[str]) -> list[str]:
    """Root-level manifests worth fetching, allowlisted and capped."""
    path_set = set(paths)
    found = [name for name in MANIFEST_ALLOWLIST if name in path_set]
    return found[:MAX_MANIFEST_FETCHES]


def parse_manifest(path: str, text: str) -> dict[str, object]:
    """Extract kind / dependencies / test command from one manifest.

    Never raises: malformed manifests degrade to an empty dependency list.
    """
    kind = _manifest_kind(path)
    deps: list[str] = []
    test_command: str | None = None
    try:
        if kind == "pyproject":
            data = tomllib.loads(text)
            project = data.get("project", {}) or {}
            deps = [_dep_name(d) for d in project.get("dependencies", []) or []]
            tool = data.get("tool", {}) or {}
            if "pytest" in tool or _has_pytest_dep(project):
                test_command = "pytest"
        elif kind == "package_json":
            data = json.loads(text)
            deps = sorted((data.get("dependencies") or {}).keys())
            scripts = data.get("scripts") or {}
            if scripts.get("test"):
                test_command = "npm test"
        elif kind == "go_mod":
            deps = re.findall(r"^\t([\w./-]+) v", text, flags=re.MULTILINE)
            test_command = "go test ./..."
        elif kind == "cargo":
            data = tomllib.loads(text)
            deps = sorted((data.get("dependencies") or {}).keys())
            test_command = "cargo test"
        elif kind == "maven":
            deps = re.findall(r"<artifactId>([\w.-]+)</artifactId>", text)
            test_command = "mvn test"
        elif kind == "gemfile":
            deps = re.findall(r"^\s*gem\s+['\"]([\w-]+)['\"]", text, flags=re.MULTILINE)
        elif kind == "composer":
            data = json.loads(text)
            deps = sorted((data.get("require") or {}).keys())
        elif kind == "requirements":
            for line in text.splitlines():
                line = line.strip()
                if line and not line.startswith(("#", "-")):
                    deps.append(_dep_name(line))
    except Exception:
        deps = []
        test_command = None
    return {
        "path": path,
        "kind": kind,
        "dependencies": deps[:_MAX_DEPENDENCIES],
        "test_command": test_command,
    }


def infer_test_commands(manifests: list[dict[str, object]]) -> list[str]:
    """Deduplicated test commands inferred from parsed manifests."""
    commands: list[str] = []
    for manifest in manifests:
        command = manifest.get("test_command")
        if isinstance(command, str) and command and command not in commands:
            commands.append(command)
    return commands


def _manifest_kind(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    return {
        "pyproject.toml": "pyproject",
        "package.json": "package_json",
        "go.mod": "go_mod",
        "Cargo.toml": "cargo",
        "pom.xml": "maven",
        "Gemfile": "gemfile",
        "composer.json": "composer",
        "requirements.txt": "requirements",
    }.get(name, "unknown")


def _dep_name(requirement: str) -> str:
    """'requests>=2.31; extra' -> 'requests'."""
    return re.split(r"[\s<>=!~;\[]", requirement.strip(), maxsplit=1)[0]


def _has_pytest_dep(project: dict[str, object]) -> bool:
    deps = project.get("dependencies", []) or []
    optional = project.get("optional-dependencies", {}) or {}
    all_deps = list(deps)
    if isinstance(optional, dict):
        for group in optional.values():
            all_deps.extend(group or [])
    return any(_dep_name(str(d)) == "pytest" for d in all_deps)
