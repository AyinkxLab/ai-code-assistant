"""Tests for additional archive import formats (#90): .tar, .tgz, .7z, .rar.

Every format must go through the same in-memory, traversal-safe extraction
path (path-traversal rejection, secret/vendor skips, size and file-count caps),
and malformed archives must fail with a clear 400.
"""

import binascii
import io
import struct
import tarfile

import py7zr
import pytest
import rarfile

from app.extensions import db
from app.models import Project, ProjectFile, Workspace
from app.models.project import SOURCE_ARCHIVE, STATUS_READY


def _upload(client, workspace_id, data, filename):
    return client.post(
        f"/workspaces/api/workspaces/{workspace_id}/projects",
        data={"file": (io.BytesIO(data), filename)},
        content_type="multipart/form-data",
    )


@pytest.fixture()
def workspace(app, make_user, login):
    user = make_user()
    login()
    ws = Workspace(user_id=user.id, name="Archive formats workspace")
    db.session.add(ws)
    db.session.commit()
    return ws


def _tar_bytes(entries, mode="w"):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode=mode) as archive:
        for name, data in entries:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _7z_bytes(entries):
    buffer = io.BytesIO()
    with py7zr.SevenZipFile(buffer, "w") as archive:
        for name, data in entries:
            archive.writestr(data, name)
    return buffer.getvalue()


def _crc16(payload: bytes) -> int:
    return binascii.crc32(payload) & 0xFFFF


def _crc32(payload: bytes) -> int:
    return binascii.crc32(payload) & 0xFFFFFFFF


def _rar_bytes(entries):
    """Build a minimal RAR4 archive with uncompressed ("stored") entries.

    Hand-crafted so the tests do not depend on an external ``rar`` tool (which
    is the only way to *create* RAR archives); the field order and CRCs follow
    the RAR4 block/file header layout parsed by ``rarfile``.
    """
    out = bytearray(rarfile.RAR_ID)
    main_body = struct.pack("<BHH", rarfile.RAR_BLOCK_MAIN, 0, 13) + b"\x00" * 6
    out += struct.pack("<H", _crc16(main_body)) + main_body
    for name, data in entries:
        name_bytes = name.encode("utf-8")
        header_size = 32 + len(name_bytes)
        body = struct.pack("<BHH", rarfile.RAR_BLOCK_FILE, rarfile.RAR_LONG_BLOCK, header_size)
        body += struct.pack(
            "<LLBLLBBHL",
            len(data),
            len(data),
            2,  # host OS (Unix)
            _crc32(data),
            0,  # DOS timestamp
            20,  # minimum extraction version
            rarfile.RAR_M0,  # stored (no compression)
            len(name_bytes),
            0,  # attributes
        )
        body += name_bytes
        out += struct.pack("<H", _crc16(body)) + body + data
    return bytes(out)


class TestTarFormats:
    def test_import_tar(self, client, workspace):
        payload = _tar_bytes([("app.py", b"print('tar')\n"), ("docs/readme.md", b"# hi\n")])

        response = _upload(client, workspace.id, payload, "project.tar")

        assert response.status_code == 201
        project = Project.query.first()
        assert project.status == STATUS_READY
        assert project.source == SOURCE_ARCHIVE
        assert project.file_count == 2

    def test_import_tgz(self, client, workspace):
        payload = _tar_bytes([("app.py", b"print('tgz')\n")], mode="w:gz")

        response = _upload(client, workspace.id, payload, "project.tgz")

        assert response.status_code == 201
        assert Project.query.first().file_count == 1

    def test_tar_traversal_rejected(self, client, workspace):
        payload = _tar_bytes([("../evil.py", b"malicious")])

        response = _upload(client, workspace.id, payload, "project.tar")

        assert response.status_code == 400
        assert "escapes" in response.get_json()["error"]
        assert Project.query.count() == 0


class TestSevenZipFormats:
    def test_import_7z(self, client, workspace):
        payload = _7z_bytes([("src/app.py", b"print('7z')\n"), ("docs/readme.md", b"# hi\n")])

        response = _upload(client, workspace.id, payload, "project.7z")

        assert response.status_code == 201
        app_py = ProjectFile.query.filter_by(path="src/app.py").first()
        assert app_py.content == "print('7z')\n"
        assert app_py.language == "Python"

    def test_import_7z_skips_secret_files(self, client, workspace):
        payload = _7z_bytes([(".env", b"SECRET=1\n"), ("app.py", b"ok\n")])

        response = _upload(client, workspace.id, payload, "project.7z")

        assert response.status_code == 201
        assert {f.path for f in ProjectFile.query.all()} == {"app.py"}

    def test_invalid_7z_rejected(self, client, workspace):
        response = _upload(client, workspace.id, b"not a 7z archive", "project.7z")

        assert response.status_code == 400
        assert "not a valid 7z" in response.get_json()["error"]
        assert Project.query.count() == 0

    def test_7z_file_count_limit(self, client, workspace, app):
        app.config["PROJECT_MAX_FILE_COUNT"] = 1
        payload = _7z_bytes([("a.py", b"a\n"), ("b.py", b"b\n")])

        response = _upload(client, workspace.id, payload, "project.7z")

        assert response.status_code == 400
        assert "too many files" in response.get_json()["error"]


class TestRarFormats:
    def test_import_rar_stored(self, client, workspace):
        payload = _rar_bytes([("src/app.py", b"print('rar')\n")])

        response = _upload(client, workspace.id, payload, "project.rar")

        assert response.status_code == 201
        app_py = ProjectFile.query.filter_by(path="src/app.py").first()
        assert app_py.content == "print('rar')\n"
        assert app_py.language == "Python"

    def test_rar_traversal_rejected(self, client, workspace):
        payload = _rar_bytes([("../evil.py", b"malicious")])

        response = _upload(client, workspace.id, payload, "project.rar")

        assert response.status_code == 400
        assert "escapes" in response.get_json()["error"]
        assert Project.query.count() == 0

    def test_invalid_rar_rejected(self, client, workspace):
        response = _upload(client, workspace.id, b"not a rar archive", "project.rar")

        assert response.status_code == 400
        assert "not a valid RAR" in response.get_json()["error"]


class TestSupportedFormatsListedInUi:
    def test_import_ui_lists_every_supported_format(self, client, workspace):
        response = client.get(f"/workspaces/{workspace.id}")

        assert response.status_code == 200
        body = response.data.decode()
        for extension in (".zip", ".tar", ".tar.gz", ".tgz", ".7z", ".rar"):
            assert extension in body
