"""Guard against malicious assets, auto-run editor tasks, and unsafe
supply-chain changes.

This repository was once used to ship an obfuscated JavaScript payload disguised
as a web font (``public/fonts/fa-solid-500.woff2``) together with a
``.vscode/tasks.json`` that executed it whenever the folder was opened in
VS Code. This check fails CI if either pattern ever comes back:

* a file with a font extension (``.woff``/``.woff2``/``.ttf``/``.otf``/``.eot``/
  ``.svg``) is not actually a font of that type, or contains script markers; and
* a ``.vscode`` JSON file runs a task on ``folderOpen`` or enables
  ``task.allowAutomaticTasks``.

The guard was later extended to cover the wider supply-chain surface of this
repository, because a malicious pull request does not have to ship a payload
in a font file to cause damage:

* ``.github/workflows/`` — privileged triggers (``pull_request_target``,
  ``workflow_run``), missing or over-broad ``permissions``, third-party
  actions that are not pinned to a full commit SHA, ``secrets.*`` used from a
  pull-request-triggered workflow, and checkouts that leave the
  ``GITHUB_TOKEN`` in ``.git/config``.
* dependency manifests — requirements without an exact pin, extra package
  indexes, direct URL requirements, npm install lifecycle scripts, lockfile
  entries resolved from a non-npm host, and manifests that disagree with
  their lockfile.
* ``Dockerfile`` / ``docker-compose.yml`` / installation scripts — remote
  scripts piped into a shell, unpinned installs, ``ADD`` from a URL, and
  insecure download flags.
* the guard itself — a tripwire that fires if the checks below are removed.

Limitations
-----------
This is a *lint*, not a malware scanner. It cannot prove that a file is
benign, and a determined attacker can always find a payload shape it does not
recognise. Its purpose is to make the previously used attack shapes
impossible to reintroduce silently, and to make every change to a
security-sensitive file visible in the diff. It complements — and does not
replace — code review, GitHub's secret scanning, dependency review, and
CodeQL.

It has no third-party dependencies so it can run before anything is installed.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Directories that never contain first-party source worth scanning.
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache"}

#: Directory *prefixes* that are skipped as well (e.g. ``.venv-sec``).
SKIP_DIR_PREFIXES = (".venv",)

FONT_EXTENSIONS = {".woff", ".woff2", ".ttf", ".otf", ".eot", ".svg"}

#: Bytes that should never appear inside a real font or its SVG licence wrapper.
SCRIPT_MARKERS = (b"eval(", b"spawn(", b"child_process", b"require(")

# ---------------------------------------------------------------------------
# Workflow policy
# ---------------------------------------------------------------------------

#: Triggers that run with repository write access and secrets even for a fork
#: pull request. They are only safe when the workflow never checks out and
#: executes untrusted code, which this repository has no use case for.
DANGEROUS_WORKFLOW_TRIGGERS = ("pull_request_target", "workflow_run")

#: Write scopes each workflow is allowed to request. Anything else has to be
#: added here deliberately, with a reason, so that a pull request cannot widen
#: a token scope silently.
ALLOWED_WRITE_PERMISSIONS: dict[str, set[str]] = {
    # CodeQL needs to upload SARIF results to the code scanning backend.
    "codeql.yml": {"security-events"},
}

#: A fully qualified commit SHA (40 hex characters).
SHA_RE = re.compile(r"^[0-9a-f]{40}$")

#: ``uses: owner/repo@ref``
USES_RE = re.compile(r"^\s*-?\s*uses:\s*(?P<ref>[^\s#]+)", re.MULTILINE)

#: ``scope: write`` / ``scope: write-all`` inside a permissions mapping.
WRITE_PERMISSION_RE = re.compile(
    r"^\s*(?P<scope>[a-z][a-z0-9-]*)\s*:\s*(?P<value>write-all|write)\s*$", re.MULTILINE
)

#: A top-level ``permissions:`` key (column 0).
TOP_LEVEL_PERMISSIONS_RE = re.compile(r"^permissions\s*:", re.MULTILINE)

#: ``secrets.NAME`` or ``secrets.NAME.value`` inside a workflow.
SECRET_REF_RE = re.compile(r"\bsecrets\.[A-Za-z_][A-Za-z0-9_]*")


def _strip_comment_lines(text: str) -> str:
    """Drop whole-line YAML comments so prose cannot trip a policy rule."""
    kept = []
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        kept.append(line)
    return "\n".join(kept)


# ---------------------------------------------------------------------------
# Dependency manifest policy
# ---------------------------------------------------------------------------

#: The only package index the pip requirements may use.
ALLOWED_PIP_INDEXES = ("https://pypi.org/simple", "https://pypi.python.org/simple")

#: The only registry the npm lockfile may resolve packages from.
ALLOWED_NPM_REGISTRY = "https://registry.npmjs.org/"

#: npm lifecycle scripts that run automatically on ``npm install`` / ``npm ci``.
NPM_LIFECYCLE_SCRIPTS = (
    "preinstall",
    "install",
    "postinstall",
    "preprepare",
    "prepare",
    "postprepare",
    "prepublish",
    "prepublishOnly",
    "prepublishonly",
)

#: Shell metacharacters that turn a download into remote code execution.
PIPE_TO_SHELL_RE = re.compile(
    r"(curl|wget)[^\n|]*\|\s*(sudo\s+)?(ba|z|k|d)?sh\b|"
    r"(curl|wget)[^\n|]*\|\s*(sudo\s+)?(python[0-9.]*|node|ruby|perl)\b",
    re.IGNORECASE,
)

#: Flags that disable transport security for a download.
INSECURE_DOWNLOAD_RE = re.compile(r"--trusted-host\b|curl[^\n]*\s-k\b|--insecure\b")

#: ``ADD https://...`` pulls a remote file straight into the image with no
#: checksum verification.
DOCKER_ADD_URL_RE = re.compile(r"^\s*ADD\s+(https?://|\S+@https?://)", re.MULTILINE | re.IGNORECASE)


def _is_skipped_dir(name: str) -> bool:
    return name in SKIP_DIRS or name.startswith(SKIP_DIR_PREFIXES)


def _iter_files(root: Path):
    """Yield every file under ``root``, pruning directories that are skipped."""
    collected: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not _is_skipped_dir(d)]
        for name in filenames:
            collected.append(Path(dirpath) / name)
    return sorted(collected)


def _looks_like_font(path: Path) -> bool:
    """Return whether ``path`` is a real font of its declared extension."""
    ext = path.suffix.lower()
    head = path.read_bytes()[:8]

    if ext == ".woff":
        return head[:4] == b"wOFF"
    if ext == ".woff2":
        return head[:4] == b"wOF2"
    if ext in {".ttf", ".otf"}:
        return head[:4] in {b"\x00\x01\x00\x00", b"OTTO", b"true", b"ttcf"}
    if ext == ".eot":
        # An EOT file starts with its own total size as a little-endian uint32.
        return int.from_bytes(head[:4], "little") == path.stat().st_size
    if ext == ".svg":
        stripped = path.read_bytes()[:256].lstrip()
        return stripped.startswith(b"<?xml") or stripped.startswith(b"<svg")
    return True


# ---------------------------------------------------------------------------
# Individual check groups
# ---------------------------------------------------------------------------


def _check_fonts_and_editor_tasks(root: Path, files: list[Path]) -> list[str]:
    """Original guard: fake fonts, script markers, and VS Code auto-run tasks."""
    problems: list[str] = []
    vscode_dir = root / ".vscode"

    for path in files:
        if path.suffix.lower() in FONT_EXTENSIONS:
            if not _looks_like_font(path):
                problems.append(f"{path.relative_to(root)} is not a valid {path.suffix} font")
                continue
            blob = path.read_bytes()[:4096]
            marker = next((m for m in SCRIPT_MARKERS if m in blob), None)
            if marker is not None:
                problems.append(f"{path.relative_to(root)} contains script marker {marker!r}")

        if path.parent == vscode_dir and path.suffix.lower() == ".json":
            text = path.read_text(encoding="utf-8", errors="replace")
            if '"folderOpen"' in text or "folderOpen" in text:
                problems.append(f"{path.relative_to(root)} runs a task on folderOpen")
            if "task.allowAutomaticTasks" in text:
                problems.append(f"{path.relative_to(root)} enables task.allowAutomaticTasks")
            # A task or launch entry that shells out to a downloader or a
            # nested interpreter is the same class of problem as folderOpen.
            if re.search(
                r'"command"\s*:\s*"[^"]*(?:curl|wget|powershell|cmd\s+/c|bash\s+-c|sh\s+-c'
                r"|Invoke-Expression|\biex\b)",
                text,
                re.IGNORECASE,
            ):
                problems.append(f"{path.relative_to(root)} defines a command that shells out")

    return problems


def _check_workflows(root: Path, files: list[Path]) -> list[str]:
    """Enforce the GitHub Actions policy for every workflow in the repository."""
    problems: list[str] = []
    workflows_dir = root / ".github" / "workflows"

    for path in files:
        if path.parent != workflows_dir or path.suffix.lower() not in {".yml", ".yaml"}:
            continue

        rel = path.relative_to(root).as_posix()
        raw = path.read_text(encoding="utf-8", errors="replace")
        text = _strip_comment_lines(raw)
        allowed_write = ALLOWED_WRITE_PERMISSIONS.get(path.name, set())

        # 1. Privileged triggers.
        for trigger in DANGEROUS_WORKFLOW_TRIGGERS:
            if re.search(rf"\b{re.escape(trigger)}\b", text):
                problems.append(
                    f"{rel} uses the privileged '{trigger}' trigger; "
                    "use 'pull_request' so fork pull requests stay unprivileged"
                )

        # 2. Least-privilege token scope must be declared explicitly.
        if not TOP_LEVEL_PERMISSIONS_RE.search(text):
            problems.append(
                f"{rel} does not declare a top-level 'permissions:' block; "
                "set 'permissions: contents: read' unless it needs more"
            )

        # 3. Write scopes have to be justified in ALLOWED_WRITE_PERMISSIONS.
        for match in WRITE_PERMISSION_RE.finditer(text):
            scope = match.group("scope")
            if scope in {"id-token", "contents", "pull-requests", "issues", "packages", "actions"}:
                problems.append(
                    f"{rel} requests the '{scope}' write permission; "
                    "CI must not be able to modify the repository"
                )
            elif scope not in allowed_write:
                problems.append(
                    f"{rel} requests the '{scope}' write permission, which is not on the allowlist"
                )

        # 4. Third-party actions must be pinned to a full commit SHA.
        for match in USES_RE.finditer(text):
            ref = match.group("ref")
            if ref.startswith("./"):
                continue  # local composite action, nothing to pin
            if "@" not in ref:
                problems.append(f"{rel} references {ref!r} without a version")
                continue
            _, _, version = ref.rpartition("@")
            if not SHA_RE.match(version):
                problems.append(
                    f"{rel} references {ref!r} which is not pinned to a full commit SHA"
                )

        # 5. Secrets must never be reachable from a pull-request-triggered run.
        if re.search(r"^\s*pull_request\s*:", text, re.MULTILINE) and SECRET_REF_RE.search(text):
            problems.append(
                f"{rel} reads secrets.* from a pull_request-triggered workflow; "
                "CI must work for anonymous fork pull requests"
            )

        # 6. Every checkout must drop the token instead of persisting it.
        checkouts = len(re.findall(r"uses:\s*actions/checkout@", text))
        disabled = len(re.findall(r"persist-credentials:\s*false", text))
        enabled = len(re.findall(r"persist-credentials:\s*true", text))
        if enabled:
            problems.append(f"{rel} sets persist-credentials: true on a checkout")
        if checkouts and disabled < checkouts:
            problems.append(
                f"{rel} has {checkouts} checkout step(s) but only {disabled} "
                "persist-credentials: false; the GITHUB_TOKEN would be written to .git/config"
            )

    return problems


def _check_requirements(root: Path, files: list[Path]) -> list[str]:
    """Reject unpinned, redirected, or direct-URL pip requirements."""
    problems: list[str] = []

    for path in files:
        if path.name != "requirements.txt" and not path.name.startswith("requirements-"):
            continue
        rel = path.relative_to(root).as_posix()

        for lineno, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("-"):
                # An option such as "-r requirements.txt" or "--hash=...".
                option = line.split()[0]
                if option in {"--extra-index-url", "--index-url"}:
                    url = line[len(option) :].strip().strip("'\"")
                    if not url.startswith(ALLOWED_PIP_INDEXES):
                        problems.append(
                            f"{rel}:{lineno} points pip at '{url}' instead of the public PyPI index"
                        )
                continue

            if "@" in line or line.startswith(("git+", "http://", "https://")):
                problems.append(
                    f"{rel}:{lineno} installs '{line}' from a direct URL; "
                    "a dependency must be an exact version from PyPI"
                )
                continue

            if "==" not in line:
                problems.append(
                    f"{rel}:{lineno} requirement '{line}' has no exact '==' pin; "
                    "an application dependency must be pinned"
                )

    return problems


def _check_npm_manifests(root: Path, files: list[Path]) -> list[str]:
    """Reject npm install lifecycle scripts and lockfile/manifest drift."""
    problems: list[str] = []

    package_json = root / "package.json"
    lock_path = root / "package-lock.json"

    manifest: dict = {}
    if package_json in files:
        rel = package_json.relative_to(root).as_posix()
        try:
            manifest = json.loads(package_json.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            problems.append(f"{rel} is not valid JSON: {exc}")
            manifest = {}

        scripts = manifest.get("scripts") or {}
        for name in scripts:
            if name in NPM_LIFECYCLE_SCRIPTS:
                problems.append(
                    f"{rel} defines the '{name}' lifecycle script, which runs "
                    "automatically on 'npm install'/'npm ci'"
                )

    lock: dict = {}
    if lock_path in files:
        rel = lock_path.relative_to(root).as_posix()
        try:
            lock = json.loads(lock_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            problems.append(f"{rel} is not valid JSON: {exc}")
            lock = {}

        packages = lock.get("packages") or {}
        for name, entry in packages.items():
            if not name:
                continue  # the root project entry
            resolved = entry.get("resolved")
            if resolved and not resolved.startswith(ALLOWED_NPM_REGISTRY):
                problems.append(
                    f"{rel} resolves '{name}' from '{resolved}' instead of {ALLOWED_NPM_REGISTRY}"
                )
            if resolved and not entry.get("integrity"):
                problems.append(f"{rel} has no integrity hash for '{name}'")

        # The lockfile must agree with the manifest. Only the "declared but
        # not locked" direction is an error: the lockfile legitimately also
        # holds the transitive tree, which package.json never declares.
        if manifest:
            declared = set(manifest.get("dependencies") or {}) | set(
                manifest.get("devDependencies") or {}
            )
            locked = {name.rsplit("node_modules/", 1)[-1] for name in packages if name}
            for missing in sorted(declared - locked):
                problems.append(f"{rel} does not contain the declared dependency '{missing}'")

    return problems


def _check_container_and_install_scripts(root: Path, files: list[Path]) -> list[str]:
    """Reject remote-code-execution shapes in Dockerfiles and install scripts."""
    problems: list[str] = []

    for path in files:
        name = path.name
        is_dockerfile = name == "Dockerfile" or name.startswith("Dockerfile.")
        is_compose = name.startswith("docker-compose") and path.suffix.lower() in {".yml", ".yaml"}
        is_script = path.suffix.lower() in {".sh", ".bash", ".bat", ".ps1", ".cmd"}
        if not (is_dockerfile or is_compose or is_script):
            continue

        rel = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))

        if PIPE_TO_SHELL_RE.search(code):
            problems.append(f"{rel} pipes a download straight into a shell")
        if INSECURE_DOWNLOAD_RE.search(code):
            problems.append(f"{rel} disables transport security for a download")
        if DOCKER_ADD_URL_RE.search(code):
            problems.append(f"{rel} uses ADD with a remote URL, which skips checksum verification")

        for lineno, line in enumerate(code.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue

            pip_install = re.search(r"\bpip3?\s+install\b(?P<args>[^|;&]*)", stripped)
            if (
                pip_install
                and "==" not in pip_install.group("args")
                and not re.search(r"-r\s+\S+", pip_install.group("args"))
            ):
                problems.append(f"{rel}:{lineno} installs a pip package without an exact '==' pin")

            npm_install = re.search(r"\bnpm\s+(?:i|install|ci)\s+(?P<args>[^|;&]*)", stripped)
            if npm_install:
                args = npm_install.group("args")
                # "npm ci" with no package argument installs from the lockfile
                # and is fine; "npm install <pkg>" must name a pinned version.
                if args.strip() and "@" not in args:
                    problems.append(
                        f"{rel}:{lineno} installs an npm package without a pinned '@version'"
                    )

    return problems


def _check_gitignore(root: Path, files: list[Path]) -> list[str]:
    """A .gitignore that ignores itself can hide later changes to the rules."""
    problems: list[str] = []

    gitignore = root / ".gitignore"
    if gitignore in files:
        rel = gitignore.relative_to(root).as_posix()
        for lineno, line in enumerate(gitignore.read_text(encoding="utf-8").splitlines(), 1):
            if line.strip() == ".gitignore":
                problems.append(
                    f"{rel}:{lineno} ignores itself, which can hide later changes "
                    "to the ignore rules"
                )

    return problems


def _check_guard_integrity(root: Path) -> list[str]:
    """Tripwire: fail if the guard or its test is removed or gutted.

    This only applies to a tree that actually carries the guard. A tree that
    has no ``scripts/check_assets.py`` is not a repository checkout, so there
    is nothing to verify.
    """
    problems: list[str] = []

    required_markers = (
        "folderOpen",
        "task.allowAutomaticTasks",
        "SCRIPT_MARKERS",
        "FONT_EXTENSIONS",
        "find_violations",
        "pull_request_target",
        "workflow_run",
        "persist-credentials",
    )
    guard = root / "scripts" / "check_assets.py"
    if guard.is_file():
        text = guard.read_text(encoding="utf-8", errors="replace")
        for marker in required_markers:
            if marker not in text:
                problems.append(
                    f"scripts/check_assets.py no longer contains '{marker}'; "
                    "the asset guard may have been weakened"
                )
        if not (root / "tests" / "test_asset_guard.py").is_file():
            problems.append("tests/test_asset_guard.py is missing")

    return problems


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def find_violations(root: Path = REPO_ROOT) -> list[str]:
    """Return human-readable descriptions of every suspicious file or setting."""
    root = Path(root)
    files = _iter_files(root)

    problems: list[str] = []
    problems += _check_fonts_and_editor_tasks(root, files)
    problems += _check_workflows(root, files)
    problems += _check_requirements(root, files)
    problems += _check_npm_manifests(root, files)
    problems += _check_container_and_install_scripts(root, files)
    problems += _check_gitignore(root, files)
    problems += _check_guard_integrity(root)

    return problems


def main() -> int:
    problems = find_violations(REPO_ROOT)
    if problems:
        print("Blocked: suspicious assets or auto-run tasks found:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("No suspicious assets or auto-run tasks found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
