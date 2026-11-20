"""Tests for backup/restore automation (issue #709).

The database and the PostgreSQL binaries are mocked. No server, no
``pg_dump``/``pg_restore`` executable, and no network are required.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts import db_backup, db_restore

URL = "postgresql://aica:supersecret@db.example.net:5432/aica"


def _archive(tmp_path: Path, payload: bytes = b"custom-format-archive") -> Path:
    backup = tmp_path / "aica-20260101T000000Z.dump"
    backup.write_bytes(payload)
    db_backup.write_checksum(backup, db_backup.sha256_file(backup))
    return backup


class _Cursor:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, _sql):
        pass

    def fetchall(self):
        return self._rows

    def close(self):
        pass


class _Connection:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self):
        return _Cursor(self._rows)

    def close(self):
        pass


def _connector(rows):
    return lambda **_kwargs: _Connection(rows)


def _fake_dump(written: bytes = b"dump"):
    def run(command, **_kwargs):
        destination = Path(command[command.index("--file") + 1])
        destination.write_bytes(written)
        return subprocess.CompletedProcess(command, 0, "", "")

    return run


def _fake_restore():
    calls = []

    def run(command, **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    return run, calls


def test_checksum_verification_detects_tampering(tmp_path):
    backup = _archive(tmp_path)
    assert db_restore.verify_checksum(backup) == db_backup.sha256_file(backup)

    backup.write_bytes(b"tampered")
    with pytest.raises(db_restore.RestoreError) as error:
        db_restore.verify_checksum(backup)
    assert error.value.code == "checksum_mismatch"


def test_backup_writes_archive_and_checksum_without_leaking_password(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(db_backup.subprocess, "run", _fake_dump(b"archive-bytes"))

    code = db_backup.main(["--database-url", URL, "--output-dir", str(tmp_path), "--json"])
    captured = capsys.readouterr()
    result = json.loads(captured.out)

    assert code == 0
    assert Path(result["backup"]).read_bytes() == b"archive-bytes"
    assert Path(result["checksum_file"]).is_file()
    assert db_backup.sha256_file(Path(result["backup"])) == result["sha256"]
    assert "supersecret" not in captured.out
    assert "supersecret" not in captured.err


def test_backup_failure_is_structured_and_sanitized(tmp_path, monkeypatch, capsys):
    def failing(command, **_kwargs):
        return subprocess.CompletedProcess(
            command,
            1,
            "",
            "password supersecret rejected for postgresql://aica:supersecret@db/aica",
        )

    monkeypatch.setattr(db_backup.subprocess, "run", failing)
    code = db_backup.main(["--database-url", URL, "--output-dir", str(tmp_path)])
    captured = capsys.readouterr()

    assert code == 1
    error = json.loads(captured.err)
    assert error["error"]["code"] == "backup_failed"
    assert "supersecret" not in captured.err


def test_backup_rejects_non_postgresql_url(capsys):
    code = db_backup.main(["--database-url", "sqlite:///instance/app.db", "--output-dir", "unused"])

    assert code == 2
    error = json.loads(capsys.readouterr().err)
    assert error["error"]["code"] == "invalid_config"


def test_backup_reports_output_directory_permission_failure(tmp_path, capsys):
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")

    code = db_backup.main(["--database-url", URL, "--output-dir", str(blocked)])

    assert code == 1
    error = json.loads(capsys.readouterr().err)
    assert error["error"]["code"] == "backup_failed"


def test_restore_refuses_non_empty_database_without_force(tmp_path):
    backup = _archive(tmp_path)
    runner, calls = _fake_restore()

    with pytest.raises(db_restore.RestoreError) as error:
        db_restore.restore_backup(
            URL,
            backup,
            runner=runner,
            connect=_connector([("public.users",), ("public.messages",)]),
        )

    assert error.value.code == "database_not_empty"
    assert "public.users" in str(error.value)
    assert calls == []


def test_force_restore_allows_non_empty_database(tmp_path):
    backup = _archive(tmp_path)
    runner, calls = _fake_restore()

    result = db_restore.restore_backup(
        URL,
        backup,
        force=True,
        runner=runner,
        connect=_connector([("public.users",)]),
    )

    assert result["ok"] is True
    assert result["forced"] is True
    assert len(calls) == 1
    assert "--clean" in calls[0]
    assert "--if-exists" in calls[0]
    assert "supersecret" not in " ".join(calls[0])


def test_restore_cli_refuses_non_empty_database(tmp_path, monkeypatch, capsys):
    backup = _archive(tmp_path)
    monkeypatch.setattr(db_restore, "_connect", _connector([("public.users",)]))

    code = db_restore.main(["--database-url", URL, "--backup", str(backup)])

    assert code == 1
    error = json.loads(capsys.readouterr().err)
    assert error["error"]["code"] == "database_not_empty"


def test_restore_cli_checks_checksum_before_connecting(tmp_path, monkeypatch, capsys):
    backup = _archive(tmp_path)
    backup.write_bytes(b"tampered")
    connected = []
    monkeypatch.setattr(db_restore, "_connect", lambda **kwargs: connected.append(kwargs))

    code = db_restore.main(["--database-url", URL, "--backup", str(backup)])

    assert code == 1
    error = json.loads(capsys.readouterr().err)
    assert error["error"]["code"] == "checksum_mismatch"
    assert connected == []


def test_restore_cli_succeeds_into_empty_database(tmp_path, monkeypatch, capsys):
    backup = _archive(tmp_path)
    monkeypatch.setattr(db_restore, "_connect", _connector([]))
    runner, calls = _fake_restore()
    monkeypatch.setattr(db_restore.subprocess, "run", runner)

    code = db_restore.main(["--database-url", URL, "--backup", str(backup), "--json"])
    captured = capsys.readouterr()

    assert code == 0
    result = json.loads(captured.out)
    assert result["ok"] is True
    assert len(calls) == 1
    assert "supersecret" not in captured.out


def test_restore_reports_permission_failure(tmp_path):
    backup = _archive(tmp_path)

    def denied(_command, **_kwargs):
        raise PermissionError("denied")

    with pytest.raises(db_restore.RestoreError) as error:
        db_restore.restore_backup(
            URL,
            backup,
            runner=denied,
            connect=_connector([]),
        )

    assert error.value.code == "permission_denied"


def test_restore_argument_validation_is_structured(capsys):
    code = db_restore.main(["--database-url", URL])

    assert code == 2
    error = json.loads(capsys.readouterr().err)
    assert error["error"]["code"] == "invalid_config"
