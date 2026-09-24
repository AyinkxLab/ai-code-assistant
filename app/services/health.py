"""Project health helpers: CI-config detection and a rough coverage estimate.

Both are best-effort heuristics computed over the indexed file tree. The
coverage value is explicitly an *estimate* — a test-file-to-source-file ratio,
never a measured coverage percentage — and is returned with an ``estimate`` flag
and a human-readable note so the UI can label it honestly.
"""

from __future__ import annotations

# Well-known CI configuration file basenames (matched case-insensitively).
_CI_BASENAMES = {
    ".gitlab-ci.yml",
    ".gitlab-ci.yaml",
    ".travis.yml",
    ".travis.yaml",
    "azure-pipelines.yml",
    "azure-pipelines.yaml",
    "appveyor.yml",
    "bitbucket-pipelines.yml",
    "jenkinsfile",
    ".drone.yml",
    ".drone.yaml",
    "buildkite.yml",
    "buildkite.yaml",
}

# CI configuration directories; any file beneath these is a CI config.
_CI_PREFIXES = (
    ".github/workflows/",
    ".circleci/",
    ".buildkite/",
    ".teamcity/",
)

# Extensions treated as "source" for the coverage ratio.
_SOURCE_EXTS = {
    "py",
    "js",
    "ts",
    "jsx",
    "tsx",
    "java",
    "go",
    "rs",
    "rb",
    "php",
    "c",
    "cc",
    "cpp",
    "h",
    "hpp",
    "cs",
    "kt",
    "swift",
    "scala",
    "sh",
    "vue",
    "svelte",
    "dart",
    "ex",
    "exs",
    "clj",
    "lua",
}


def is_test_path(path: str) -> bool:
    """Return ``True`` for common test-file naming conventions."""
    name = path.rsplit("/", 1)[-1].lower()
    return (
        name.startswith("test_")
        or name.startswith("tests_")
        or name.endswith("_test.py")
        or name.endswith(".test.js")
        or name.endswith(".test.ts")
        or name.endswith(".spec.js")
        or name.endswith(".spec.ts")
        or "tests/" in f"/{path}/"
        or "/test/" in f"/{path}/"
    )


def _extension(path: str) -> str:
    return path.rsplit(".", 1)[-1].lower() if "." in path else ""


def detect_ci_files(paths, *, limit: int = 50) -> list[str]:
    """Return indexed paths that look like CI configuration, sorted."""
    found: set[str] = set()
    for raw in paths:
        path = (raw or "").lstrip("/")
        if not path:
            continue
        if path.rsplit("/", 1)[-1].lower() in _CI_BASENAMES or path.lower().startswith(
            _CI_PREFIXES
        ):
            found.add(raw)
    return sorted(found)[:limit]


def _coverage_label(ratio: float, *, has_source: bool, has_tests: bool) -> str:
    if not has_source:
        return "unknown"
    if not has_tests:
        return "none"
    if ratio < 0.1:
        return "low"
    if ratio < 0.25:
        return "moderate"
    if ratio < 0.5:
        return "good"
    return "high"


def coverage_estimate(paths) -> dict:
    """Return a clearly-labelled estimate from the test/source file ratio.

    The ratio is ``test_files / source_files`` (0 when there are no source
    files). It is a coarse proxy for whether a project has tests, not a measured
    coverage percentage.
    """
    test_count = 0
    source_count = 0
    for raw in paths:
        path = (raw or "").lstrip("/")
        if not path or "\\" in path:
            continue
        if is_test_path(path):
            test_count += 1
            continue
        if _extension(path) in _SOURCE_EXTS:
            source_count += 1

    ratio = round(test_count / source_count, 3) if source_count else 0.0
    return {
        "estimate": True,
        "test_file_count": test_count,
        "source_file_count": source_count,
        "ratio": ratio,
        "label": _coverage_label(ratio, has_source=source_count > 0, has_tests=test_count > 0),
        "note": (
            "Estimated from the test-file-to-source-file ratio; this is not a "
            "measured coverage percentage."
        ),
    }
