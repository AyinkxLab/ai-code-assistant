"""Tests for the read-only project detection CLI (issue #191).

``flask project detect-stellar <path>`` walks a local directory, applies the
import skip rules, and reports Stellar/Soroban detection. These tests build
throwaway directory trees in ``tmp_path``; nothing is written to the project or
the database.
"""

import json

from app.services.project_cli import walk_project_files


def _runner(app):
    return app.test_cli_runner()


def _write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _soroban_project(root):
    contract = root / "contracts" / "hello"
    _write(
        contract / "Cargo.toml",
        "[package]\n"
        'name = "hello"\n'
        'version = "0.1.0"\n'
        'edition = "2021"\n\n'
        "[dependencies]\n"
        'soroban-sdk = "21"\n',
    )
    _write(
        contract / "src" / "lib.rs",
        "#![no_std]\n"
        "use soroban_sdk::{contract, contractimpl, Env};\n\n"
        "#[contract]\n"
        "pub struct Hello;\n",
    )
    return root


def _plain_rust_project(root):
    _write(
        root / "Cargo.toml",
        "[package]\n"
        'name = "plain"\n'
        'version = "0.1.0"\n'
        'edition = "2021"\n\n'
        "[dependencies]\n"
        'serde = "1"\n',
    )
    _write(root / "src" / "main.rs", 'fn main() { println!("hi"); }\n')
    return root


class TestDetectStellarCLI:
    def test_soroban_directory_reports_likely(self, app, tmp_path):
        root = _soroban_project(tmp_path / "soroban")
        result = _runner(app).invoke(args=["project", "detect-stellar", str(root)])
        assert result.exit_code == 0
        assert "confidence: likely" in result.output
        assert "is_soroban: True" in result.output
        assert "evidence:" in result.output

    def test_plain_rust_directory_reports_none(self, app, tmp_path):
        root = _plain_rust_project(tmp_path / "plain")
        result = _runner(app).invoke(args=["project", "detect-stellar", str(root)])
        assert result.exit_code == 0
        assert "confidence: none" in result.output
        assert "is_stellar: False" in result.output

    def test_json_output_is_parseable(self, app, tmp_path):
        root = _soroban_project(tmp_path / "soroban")
        result = _runner(app).invoke(
            args=["project", "detect-stellar", str(root), "--json"]
        )
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["confidence"] == "likely"
        assert payload["is_soroban"] is True
        assert payload["files_scanned"] >= 2
        assert payload["path"].endswith("soroban")

    def test_missing_directory_is_rejected(self, app, tmp_path):
        result = _runner(app).invoke(
            args=["project", "detect-stellar", str(tmp_path / "does-not-exist")]
        )
        assert result.exit_code == 3

    def test_file_path_is_rejected(self, app, tmp_path):
        target = _write(tmp_path / "file.txt", "hello\n")
        result = _runner(app).invoke(args=["project", "detect-stellar", str(target)])
        assert result.exit_code == 3

    def test_skip_rules_exclude_vendor_dirs_and_secret_files(self, app, tmp_path):
        root = tmp_path / "proj"
        _write(root / "node_modules" / "pkg" / "index.js", "module.exports = 1;\n")
        _write(root / ".env", "SECRET=1\n")
        _write(root / "src" / "lib.rs", "// plain rust\n")

        stubs = walk_project_files(str(root))
        paths = sorted(stub.path for stub in stubs)
        # node_modules/ and .env are skipped, leaving only src/lib.rs.
        assert paths == ["src/lib.rs"]

    def test_binary_file_is_stubbed_without_content(self, app, tmp_path):
        root = tmp_path / "bin"
        (root).mkdir(parents=True)
        (root / "blob.bin").write_bytes(b"\x00\x01\x02\x03")

        stubs = walk_project_files(str(root))
        assert [stub.path for stub in stubs] == ["blob.bin"]
        assert stubs[0].content is None
