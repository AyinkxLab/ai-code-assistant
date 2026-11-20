"""Tests for the advisory migration preflight (#710)."""

from __future__ import annotations

import json
from pathlib import Path

from scripts import migration_preflight

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "migrations"


def _codes(result):
    return {finding["code"] for finding in result["findings"]}


def test_safe_migration_has_no_findings():
    result = migration_preflight.inspect(FIXTURES / "safe")

    assert result["ok"] is True
    assert result["findings"] == []
    assert result["migration_count"] == 1


def test_unsafe_patterns_are_flagged():
    result = migration_preflight.inspect(FIXTURES / "unsafe")

    assert result["ok"] is False
    assert _codes(result) == {
        "index_not_concurrent",
        "not_null_column_with_default",
        "type_rewrite",
        "raw_index_not_concurrent",
    }
    assert all(finding["suggestion"] for finding in result["findings"])


def test_since_limits_checks_to_pending_revisions():
    pending = migration_preflight.inspect(FIXTURES / "chain", since="chain001")
    assert _codes(pending) == {"index_not_concurrent"}

    up_to_date = migration_preflight.inspect(FIXTURES / "chain", since="chain002")
    assert up_to_date["findings"] == []
    assert up_to_date["migration_count"] == 0


def test_unknown_since_revision_is_a_structured_error(capsys):
    assert migration_preflight.main(["--versions", str(FIXTURES / "chain"), "--since", "nope"]) == 2

    error = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert "unknown revision" in error["error"]["message"]


def test_duplicate_revision_is_flagged():
    result = migration_preflight.inspect(FIXTURES / "duplicate")

    assert "duplicate_revision" in _codes(result)


def test_multiple_heads_are_flagged():
    result = migration_preflight.inspect(FIXTURES / "multihead")

    assert "multiple_heads" in _codes(result)


def test_cli_exit_codes(capsys, tmp_path):
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "0001_broken.py").write_text("def upgrade(:\n    pass\n", encoding="utf-8")

    assert migration_preflight.main(["--versions", str(FIXTURES / "safe")]) == 0
    assert migration_preflight.main(["--versions", str(FIXTURES / "unsafe")]) == 1
    assert migration_preflight.main(["--versions", str(FIXTURES / "unsafe"), "--advisory"]) == 0
    assert migration_preflight.main(["--versions", str(broken)]) == 2

    captured = capsys.readouterr()
    error = json.loads(captured.err.strip().splitlines()[-1])
    assert error["error"]["code"] == "preflight_error"
    assert "cannot parse" in error["error"]["message"]


def test_json_output_is_machine_readable(capsys):
    assert migration_preflight.main(["--versions", str(FIXTURES / "unsafe"), "--json"]) == 1

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["advisory"] is False
    assert payload["findings"]
    assert payload["limitations"]


def test_missing_directory_is_a_structured_error(capsys, tmp_path):
    assert migration_preflight.main(["--versions", str(tmp_path / "missing")]) == 2

    error = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert error["error"]["code"] == "preflight_error"
