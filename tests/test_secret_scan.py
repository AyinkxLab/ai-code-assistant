"""Secret scanner tests (#624).

Synthetic credentials are assembled at runtime so no live-looking value is
committed to the repository.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = REPO_ROOT / "scripts" / "secret_scan.py"
_spec = importlib.util.spec_from_file_location("secret_scan", _SCRIPT)
secret_scan = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = secret_scan
_spec.loader.exec_module(secret_scan)

# Split so the scanner itself does not see a complete credential in this file.
SYNTHETIC = "abcd1234" + "EFGH5678" + "ijkl9012"


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _allowlist(tmp_path: Path, pattern: str, value: str) -> Path:
    path = tmp_path / "allowlist.json"
    path.write_text(
        json.dumps({"version": 1, "allow": [{"pattern": pattern, "value": value}]}),
        encoding="utf-8",
    )
    return path


def test_generic_synthetic_credential_is_caught(tmp_path):
    path = _write(tmp_path, "config.py", f'api_key = "{SYNTHETIC}"\n')

    findings = secret_scan.scan_paths([path])

    assert len(findings) == 1
    assert findings[0]["pattern"] == "generic_secret_assignment"
    assert findings[0]["line"] == 1


def test_allowlisted_credential_is_not_reported(tmp_path):
    path = _write(tmp_path, "config.py", f'api_key = "{SYNTHETIC}"\n')
    allowlist_path = _allowlist(tmp_path, "generic_secret_assignment", SYNTHETIC)

    allowlist = secret_scan.load_allowlist(allowlist_path, required=True)
    findings = secret_scan.scan_paths([path], allowlist=allowlist)

    assert findings == []


def test_known_token_shapes_are_caught(tmp_path):
    aws = "AKIA" + "ABCDEFGHIJKLMNOP"
    private_key = "-----BEGIN " + "PRIVATE KEY-----"
    path = _write(tmp_path, "keys.txt", f"{aws}\n{private_key}\n")

    findings = secret_scan.scan_paths([path])
    patterns = {finding["pattern"] for finding in findings}

    assert "aws_access_key" in patterns
    assert "private_key" in patterns


def test_placeholders_and_low_entropy_values_are_ignored(tmp_path):
    path = _write(
        tmp_path,
        "config.py",
        'api_key = "your-api-key-placeholder-here"\npassword = "aaaaaaaaaaaaaaaaaaaa"\n',
    )

    assert secret_scan.scan_paths([path]) == []


def test_cli_exit_codes_and_redacted_output(tmp_path, capsys):
    path = _write(tmp_path, "config.py", f'api_key = "{SYNTHETIC}"\n')

    assert secret_scan.main([str(path), "--json"]) == secret_scan.EXIT_FINDINGS
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["count"] == 1
    assert SYNTHETIC not in json.dumps(payload)

    allowlist_path = _allowlist(tmp_path, "generic_secret_assignment", SYNTHETIC)
    assert (
        secret_scan.main([str(path), "--json", "--allowlist", str(allowlist_path)])
        == secret_scan.EXIT_CLEAN
    )
    assert json.loads(capsys.readouterr().out)["ok"] is True

    assert secret_scan.main([str(tmp_path / "missing"), "--json"]) == secret_scan.EXIT_ERROR
    assert json.loads(capsys.readouterr().out)["code"] == "invalid_input"


def test_malformed_allowlist_is_an_input_error(tmp_path, capsys):
    path = _write(tmp_path, "allowlist.json", '{"allow": "not-a-list"}')

    assert secret_scan.main([str(tmp_path), "--allowlist", str(path)]) == secret_scan.EXIT_ERROR
    with pytest.raises(secret_scan.SecretScanError):
        secret_scan.load_allowlist(path, required=True)


def test_repository_has_no_unallowlisted_findings():
    allowlist = secret_scan.load_allowlist(REPO_ROOT / secret_scan.DEFAULT_ALLOWLIST)
    roots = [REPO_ROOT / name for name in ("app", "scripts", "docs", ".github", "tests")]

    findings = secret_scan.scan_paths(roots, allowlist=allowlist)

    assert findings == []
