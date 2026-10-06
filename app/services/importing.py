"""Project import service.

Extracts project snapshots from uploaded archives (``.zip``, ``.tar``,
``.tar.gz``, ``.tgz``, ``.7z`` and ``.rar``) or a connected GitHub repository
into bounded, sanitized metadata rows. Never writes extracted files to disk:
entries are validated in-memory and only their metadata plus a capped copy of
plain-text content is stored in the database.

Security invariants enforced here:

* absolute paths, ``..`` traversal, and symlinks inside archives are rejected;
* uncompressed size and file-count caps protect against archive bombs;
* VCS/vendor directories and obvious secret files are skipped;
* binary and oversized files keep metadata but no searchable content.
"""

from __future__ import annotations

import hashlib
import io
import re
import tarfile
import zipfile
from datetime import UTC, datetime

from app.extensions import db
from app.models import Project, ProjectFile
from app.models.project import SOURCE_GITHUB, STATUS_READY
from app.services.github import GitHubError


class ProjectImportError(RuntimeError):
    """Raised when an archive or import violates project import rules."""


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

LANGUAGE_BY_EXT = {
    "py": "Python",
    "js": "JavaScript",
    "jsx": "JavaScript",
    "ts": "TypeScript",
    "tsx": "TypeScript",
    "html": "HTML",
    "css": "CSS",
    "sql": "SQL",
    "java": "Java",
    "go": "Go",
    "rs": "Rust",
    "rb": "Ruby",
    "php": "PHP",
    "c": "C",
    "h": "C",
    "cpp": "C++",
    "hpp": "C++",
    "cs": "C#",
    "sh": "Shell",
    "json": "JSON",
    "yaml": "YAML",
    "yml": "YAML",
    "toml": "TOML",
    "md": "Markdown",
    "txt": "Text",
    "xml": "XML",
    "vue": "Vue",
    "svelte": "Svelte",
    "swift": "Swift",
    "kt": "Kotlin",
    "lua": "Lua",
    "r": "R",
    "pl": "Perl",
    "dart": "Dart",
    "dockerfile": "Dockerfile",
}

_ARCHIVE_EXT_RE = re.compile(r"\.(?:zip|tar|tar\.gz|tgz|7z|rar)$", re.IGNORECASE)


def _config_set(name: str) -> set[str]:
    """Read a comma-separated config value as a set of lowercased strings."""
    from flask import current_app

    raw = current_app.config.get(name) or ""
    return {part.strip().lower() for part in raw.split(",") if part.strip()}


def sanitize_member_path(path: str) -> str:
    """Normalize an archive entry path, rejecting traversal and absolutes.

    Returns ``""`` for empty entries and raises :class:`ProjectImportError`
    when an entry tries to escape the project directory.
    """
    cleaned = (path or "").replace("\\", "/").strip()
    if not cleaned:
        return ""
    if cleaned.startswith("/") or re.match(r"^[A-Za-z]:", cleaned):
        raise ProjectImportError(
            "Archive contains an entry with an absolute path and was rejected."
        )
    parts = [part for part in cleaned.split("/") if part not in ("", ".")]
    if not parts:
        return ""
    if any(part == ".." for part in parts):
        raise ProjectImportError("Archive contains an entry that escapes the project directory.")
    return "/".join(parts)


def should_skip(path: str) -> bool:
    """Return ``True`` when a path should not be imported.

    Skips VCS/vendor directories and files that commonly hold credentials.
    """
    parts = path.split("/")
    skip_dirs = _config_set("PROJECT_SKIP_DIRS")
    skip_secret = _config_set("PROJECT_SKIP_SECRET_FILES")
    if any(part.lower() in skip_dirs for part in parts):
        return True
    name = parts[-1].lower()
    for entry in skip_secret:
        if entry.startswith(".") and (name.endswith(entry) or name.startswith(entry)):
            return True
        if name == entry:
            return True
    return False


def looks_binary(raw: bytes) -> bool:
    """Return ``True`` when the first bytes contain a NUL character."""
    return b"\x00" in raw[:8000]


def detect_language(path: str) -> str | None:
    """Return a display language for a path based on its extension."""
    lower = path.lower()
    if lower.endswith((".py", ".pyw")):
        return "Python"
    if lower.endswith(("dockerfile",)):
        return "Dockerfile"
    if "." in path:
        ext = lower.rsplit(".", 1)[1]
        return LANGUAGE_BY_EXT.get(ext)
    return None


def _to_file_row(path: str, raw: bytes, *, max_chars: int) -> dict:
    """Build a sanitized file row from raw bytes."""
    is_binary = looks_binary(raw)
    language = None if is_binary else detect_language(path)
    content = None
    if not is_binary:
        text = raw.decode("utf-8", errors="replace")
        if len(text) <= max_chars:
            content = text
    return {
        "path": path,
        "size": len(raw),
        "is_binary": is_binary,
        "language": language,
        "content": content,
    }


# --------------------------------------------------------------------------
# Duplicate detection
# --------------------------------------------------------------------------


def archive_hash(raw: bytes) -> str:
    """Return the SHA-256 hex digest of an archive payload."""
    return hashlib.sha256(raw).hexdigest()


def find_duplicate_archive(workspace_id: int, content_hash: str) -> Project | None:
    """Return an existing archive import with the same content hash, if any.

    Scoped to the workspace: the same archive imported into another workspace is
    a separate project.
    """
    return (
        Project.query.filter_by(workspace_id=workspace_id, content_hash=content_hash)
        .order_by(Project.created_at)
        .first()
    )


def find_duplicate_github(workspace_id: int, full_name: str, default_branch: str) -> Project | None:
    """Return an existing GitHub import of the same repo + default branch.

    ``owner/name`` is compared case-insensitively because GitHub identifiers are
    case-insensitive.
    """
    return (
        Project.query.filter(
            Project.workspace_id == workspace_id,
            Project.source == SOURCE_GITHUB,
            db.func.lower(Project.source_url) == full_name.lower(),
            Project.default_branch == default_branch,
        )
        .order_by(Project.created_at)
        .first()
    )


# --------------------------------------------------------------------------
# Archive extraction
# --------------------------------------------------------------------------


def _limits():
    from flask import current_app

    return {
        "max_archive": current_app.config["PROJECT_MAX_ARCHIVE_BYTES"],
        "max_total": current_app.config["PROJECT_MAX_SIZE_BYTES"],
        "max_files": current_app.config["PROJECT_MAX_FILE_COUNT"],
        "max_chars": current_app.config["PROJECT_MAX_FILE_CHARS"],
    }


def _extract_zip(fileobj: io.BytesIO, limits: dict) -> list[dict]:
    rows: list[dict] = []
    total = 0
    try:
        with zipfile.ZipFile(fileobj) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                mode = (info.external_attr >> 16) & 0o170000
                if mode == 0o120000:  # symbolic link
                    continue
                path = sanitize_member_path(info.filename)
                if not path or should_skip(path):
                    continue
                if info.file_size > limits["max_total"]:
                    raise ProjectImportError(
                        "Archive contains a file larger than the project size limit."
                    )
                if total + info.file_size > limits["max_total"]:
                    raise ProjectImportError("Archive expands beyond the project size limit.")
                if len(rows) >= limits["max_files"]:
                    raise ProjectImportError("Archive contains too many files to import.")
                raw = archive.read(info)
                total += len(raw)
                rows.append(_to_file_row(path, raw, max_chars=limits["max_chars"]))
    except zipfile.BadZipFile as exc:
        raise ProjectImportError("The uploaded file is not a valid ZIP archive.") from exc
    except NotImplementedError as exc:
        raise ProjectImportError("The archive uses an unsupported compression method.") from exc
    except RuntimeError as exc:
        raise ProjectImportError(f"Could not read the archive: {exc}") from exc
    return rows


def _extract_tar(fileobj: io.BytesIO, limits: dict) -> list[dict]:
    rows: list[dict] = []
    total = 0
    try:
        with tarfile.open(fileobj=fileobj, mode="r:*") as archive:
            for member in archive:
                if not member.isfile():
                    continue  # skip dirs, symlinks, and special files
                path = sanitize_member_path(member.name)
                if not path or should_skip(path):
                    continue
                if member.size > limits["max_total"]:
                    raise ProjectImportError(
                        "Archive contains a file larger than the project size limit."
                    )
                if total + member.size > limits["max_total"]:
                    raise ProjectImportError("Archive expands beyond the project size limit.")
                if len(rows) >= limits["max_files"]:
                    raise ProjectImportError("Archive contains too many files to import.")
                extracted = archive.extractfile(member)
                if extracted is None:
                    continue
                raw = extracted.read()
                total += len(raw)
                rows.append(_to_file_row(path, raw, max_chars=limits["max_chars"]))
    except tarfile.TarError as exc:
        raise ProjectImportError(f"The uploaded file is not a valid TAR archive: {exc}") from exc
    return rows


def _extract_7z(fileobj: io.BytesIO, limits: dict) -> list[dict]:
    """Extract a ``.7z`` archive in memory (no filesystem writes)."""
    try:
        import py7zr
        from py7zr.io import BytesIOFactory
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise ProjectImportError("7z archive support is unavailable.") from exc

    try:
        with py7zr.SevenZipFile(fileobj, mode="r") as archive:
            selected: list[str] = []
            total = 0
            for info in archive.list():
                if getattr(info, "is_directory", False):
                    continue
                path = sanitize_member_path(info.filename)
                if not path or should_skip(path):
                    continue
                size = info.uncompressed or 0
                if size > limits["max_total"]:
                    raise ProjectImportError(
                        "Archive contains a file larger than the project size limit."
                    )
                if total + size > limits["max_total"]:
                    raise ProjectImportError("Archive expands beyond the project size limit.")
                if len(selected) >= limits["max_files"]:
                    raise ProjectImportError("Archive contains too many files to import.")
                total += size
                selected.append(info.filename)

            if not selected:
                return []

            archive.reset()
            factory = BytesIOFactory(limit=limits["max_total"])
            archive.extract(targets=selected, factory=factory)

            rows: list[dict] = []
            for name in selected:
                product = factory.products.get(name)
                if product is None:
                    continue
                path = sanitize_member_path(name)
                if not path or should_skip(path):
                    continue
                product.seek(0)
                rows.append(_to_file_row(path, product.read(), max_chars=limits["max_chars"]))
            return rows
    except ProjectImportError:
        raise
    except py7zr.exceptions.Bad7zFile as exc:
        raise ProjectImportError("The uploaded file is not a valid 7z archive.") from exc
    except py7zr.exceptions.PasswordRequired as exc:
        raise ProjectImportError("Password-protected 7z archives are not supported.") from exc
    except py7zr.exceptions.DecompressionBombError as exc:
        raise ProjectImportError("Archive expands beyond the project size limit.") from exc
    except py7zr.exceptions.ArchiveError as exc:
        raise ProjectImportError(f"Could not read the 7z archive: {exc}") from exc


def _extract_rar(fileobj: io.BytesIO, limits: dict) -> list[dict]:
    """Extract a ``.rar`` archive in memory (stored entries only).

    ``rarfile`` reads uncompressed ("stored") entries directly from the
    in-memory buffer. Archives whose entries use a compression method that
    requires an external ``unrar``/``bsdtar`` binary are rejected with a clear
    error rather than shelling out, preserving the in-memory guarantee.
    """
    try:
        import rarfile
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise ProjectImportError("RAR archive support is unavailable.") from exc

    rows: list[dict] = []
    total = 0
    try:
        with rarfile.RarFile(fileobj) as archive:
            for info in archive.infolist():
                if info.isdir():
                    continue
                path = sanitize_member_path(info.filename)
                if not path or should_skip(path):
                    continue
                if info.file_size > limits["max_total"]:
                    raise ProjectImportError(
                        "Archive contains a file larger than the project size limit."
                    )
                if total + info.file_size > limits["max_total"]:
                    raise ProjectImportError("Archive expands beyond the project size limit.")
                if len(rows) >= limits["max_files"]:
                    raise ProjectImportError("Archive contains too many files to import.")
                with archive.open(info) as handle:
                    raw = handle.read()
                total += len(raw)
                rows.append(_to_file_row(path, raw, max_chars=limits["max_chars"]))
    except ProjectImportError:
        raise
    except rarfile.PasswordRequired as exc:
        raise ProjectImportError("Password-protected RAR archives are not supported.") from exc
    except rarfile.NeedFirstVolume as exc:
        raise ProjectImportError("Multi-volume RAR archives are not supported.") from exc
    except rarfile.RarCannotExec as exc:
        raise ProjectImportError(
            "This RAR archive uses a compression method that requires an external "
            "extractor; only uncompressed RAR archives are supported."
        ) from exc
    except rarfile.Error as exc:
        raise ProjectImportError(f"The uploaded file is not a valid RAR archive: {exc}") from exc
    return rows


def extract_archive(fileobj, filename: str) -> list[dict]:
    """Extract a supported archive upload into sanitized file rows.

    Supports ``.zip``, ``.tar``, ``.tar.gz``, ``.tgz``, ``.7z`` and ``.rar``.
    Every format goes through the same path-traversal, secret-file, size, and
    file-count checks, and nothing is ever written to disk. Raises
    :class:`ProjectImportError` on unsupported types, size/count violations,
    traversal attempts, or archives that cannot be read.
    """
    from flask import current_app

    raw = fileobj.read()
    if len(raw) > current_app.config["PROJECT_MAX_ARCHIVE_BYTES"]:
        raise ProjectImportError("Archive exceeds the maximum allowed upload size.")

    name = (filename or "").lower()
    if not _ARCHIVE_EXT_RE.search(name):
        raise ProjectImportError(
            "Unsupported archive type. Upload a .zip, .tar, .tar.gz, .tgz, .7z or .rar file."
        )

    limits = _limits()
    buffer = io.BytesIO(raw)
    if name.endswith(".zip"):
        return _extract_zip(buffer, limits)
    if name.endswith(".7z"):
        return _extract_7z(buffer, limits)
    if name.endswith(".rar"):
        return _extract_rar(buffer, limits)
    return _extract_tar(buffer, limits)


def build_manifest_rows(files: list[dict]) -> list[dict]:
    """Validate a client-built file manifest (files/folder dragged from the OS).

    Each entry is ``{"path": str, "content": str}``. Paths go through the same
    :func:`sanitize_member_path` as archives and the Phase 5 size/count caps are
    enforced, so a manifest cannot bypass the archive import rules. Skips the
    same VCS/vendor and obvious-secret paths as archives.
    """
    if not isinstance(files, list) or not files:
        raise ProjectImportError("No files were provided in the import manifest.")

    limits = _limits()
    rows: list[dict] = []
    total = 0
    for entry in files:
        if not isinstance(entry, dict):
            continue
        path = sanitize_member_path(str(entry.get("path") or ""))
        if not path or should_skip(path):
            continue
        content = entry.get("content")
        raw = str(content if content is not None else "").encode("utf-8", errors="replace")
        if len(raw) > limits["max_total"]:
            raise ProjectImportError("A dropped file is larger than the project size limit.")
        if total + len(raw) > limits["max_total"]:
            raise ProjectImportError("Dropped files exceed the project size limit.")
        if len(rows) >= limits["max_files"]:
            raise ProjectImportError("Too many files to import.")
        total += len(raw)
        rows.append(_to_file_row(path, raw, max_chars=limits["max_chars"]))
    return rows


# --------------------------------------------------------------------------
# GitHub import
# --------------------------------------------------------------------------


def import_github_repo(project, full_name: str, client, *, repo: dict | None = None) -> None:
    """Import a GitHub repository into ``project`` using an authenticated client.

    Stores bounded metadata + content for the repository's blob tree and marks
    the project ready on success. Raises :class:`GitHubError` for API-level
    failures, which the caller can surface as a failed project.

    ``repo`` may be a repository payload already fetched by the caller (this
    avoids a duplicate API call when the default branch was needed for duplicate
    detection); when omitted it is fetched here.
    """
    from flask import current_app

    if repo is None:
        repo = client.get_repository(full_name)
    default_branch = repo.get("default_branch") or "HEAD"
    tree = client.get_tree(full_name, default_branch, recursive=True)

    limits = _limits()
    max_files = limits["max_files"]
    max_chars = limits["max_chars"]
    max_context_fetches = current_app.config["PROJECT_GITHUB_MAX_FILES"]

    rows: list[dict] = []
    total = 0
    fetched = 0
    for entry in tree.get("tree", []):
        if entry.get("type") != "blob":
            continue
        path = entry.get("path") or ""
        try:
            path = sanitize_member_path(path)
        except ProjectImportError:
            continue
        if not path or should_skip(path):
            continue
        if len(rows) >= max_files:
            break
        size = entry.get("size") or 0
        if size > limits["max_total"] - total:
            continue

        language = detect_language(path)
        if not should_fetch_text(size, max_chars, fetched, max_context_fetches):
            row = {
                "path": path,
                "size": size,
                "is_binary": True,
                "language": language,
                "content": None,
            }
        else:
            fetched += 1
            try:
                text = client.get_file_text(full_name, path, ref=default_branch)
            except GitHubError:
                text = None
            if text is None:
                row = {
                    "path": path,
                    "size": size,
                    "is_binary": True,
                    "language": language,
                    "content": None,
                }
            else:
                row = {
                    "path": path,
                    "size": size,
                    "is_binary": False,
                    "language": language,
                    "content": text[:max_chars],
                }
        total += size
        rows.append(row)

    store_project_files(project, rows)


def should_fetch_text(size: int, max_chars: int, fetched: int, max_fetches: int) -> bool:
    """Return ``True`` when a file's contents should be fetched from GitHub.

    Files larger than the content budget are skipped entirely, and the total
    number of content fetches is bounded so importing a huge repository does
    not cause an unbounded number of API calls.
    """
    if size > max_chars * 2:
        return False
    return fetched < max_fetches


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------


def store_project_files(project, rows: list[dict]) -> None:
    """Replace a project's files with ``rows`` and recompute its statistics."""
    ProjectFile.query.filter_by(project_id=project.id).delete()
    for row in rows:
        db.session.add(
            ProjectFile(
                project_id=project.id,
                path=row["path"],
                size=row["size"],
                is_binary=row["is_binary"],
                language=row["language"],
                content=row["content"],
            )
        )
    project.file_count = len(rows)
    project.total_size_bytes = sum(row["size"] for row in rows)
    project.status = STATUS_READY
    project.error_message = None
    project.indexed_at = datetime.now(UTC)
    db.session.commit()
