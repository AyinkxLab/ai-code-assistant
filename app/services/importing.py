"""Project import service.

Extracts project snapshots from uploaded archives (`.zip`, `.tar`,
`.tar.gz`, `.tgz`, `.7z` and `.rar`) or a connected GitHub repository
into bounded, sanitized metadata rows. Never writes extracted files to disk:
entries are validated in-memory and only their metadata plus a capped copy of
plain-text content is stored in the database.

Security invariants enforced here:

* absolute paths, `..` traversal, and symlinks inside archives are rejected;
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


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------

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
    "cs": "C",
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

    Returns `""` for empty entries and raises :class:`ProjectImportError`
    when an entry tries to escape the project directory.
    """
    cleaned = (path or "").replace("\\\\", "/").strip()
    if not cleaned:
        return ""
    if cleaned.startswith("/") or re.match(r"^[A-Za-z]:'", cleaned):
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
    """Return `True` when a path should not be imported.

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
    """Return `True` when the first bytes contain a NUL character."""
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


# ----------------------------------------------------------------------------
# Duplicate detection
# ----------------------------------------------------------------------------


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

    `owner/name` is compared case-insensitively because GitHub identifiers are
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


# ----------------------------------------------------------------------------
# Archive extraction
# ----------------------------------------------------------------------------


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
        with tarfile.open(fileobj=fileobj, mode="r!*") as archive:
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


def _extract_7z(fileobj: io.BytesIO | io.BytesIO, limits: dict) -> list[dict]:
    """Extract a `.7z` archive in memory (no filesystem writes)."""
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
        raise ProjectImportError(f"The uploaded file is not a valid 7z archive: {exc}") from exc
    except Exception as exc:
        raise ProjectImportError(f"Could not read the archive: {exc}") from exc


def _extract_rar(fileobj: io.BytesIO, limits: dict) -> list[dict]:
    """Extract a `.rar` archive in memory (no filesystem writes)."""
    try:
        import rarfile
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise ProjectImportError("RAR archive support is unavailable.") from exc

    try:
        with rarfile.RarFile.from_fileobj(fileobj) as archive:
            rows: list[dict] = []
            total = 0
            for info in archive.infolist():
                if info.is_dir():
                    continue
                path = sanitize_member_path(info.filename)
                if not path or should_skip(path):
                    continue
                size = info.file_size or 0
                if size > limits["max_total"]:
                    raise ProjectImportError(
                        "Archive contains a file larger than the project size limit."
                    )
                if total + size > limits["max_total"]:
                    raise ProjectImportError("Archive expands beyond the project size limit.")
                if len(rows) >= limits["max_files"]:
                    raise ProjectImportError("Archive contains too many files to import.")
                raw = info.read()
                total += len(raw)
                rows.append(_to_file_row(path, raw, max_chars=limits["max_chars"]))
            return rows
    except ProjectImportError:
        raise
    except rarfile.RarCannotOpenAsNeeded as exc:
        raise ProjectImportError(f"The uploaded file is not a valid RAR archive: {exc}") from exc
    except Exception as exc:
        raise ProjectImportError(f"Could not read the archive: {exc}") from exc


def extract_archive(fileobj: io.BytesIO, filename: str) -> list[dict]:
    """Dispatch to the correct extractor based on the archive extension."""
    limits = _limits()
    lower = (filename or "").lower()
    if lower.endswith(".zip"):
        return _extract_zip(fileobj, limits)
    if lower.endswith(".rar"):
        return _extract_rar(fileobj, limits)
    if lower.endswith(".7z"):
        return _extract_7z(fileobj, limits)
    if lower.endswith((".tar", ".tar.gz", ".tgz", ".txz", ".tbz")):
        return _extract_tar(fileobj, limits)
    raise ProjectImportError("Unsupported archive type.")


# ----------------------------------------------------------------------------
# Persistence
# ----------------------------------------------------------------------------


def _replace_project_files(project: Project, rows: list[dict]) -> None:
    """Replace all stored file rows for a project."""
    ProjectFile.query.filter_by(project_id=project.id).delete(synchronize_session=False)
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


def _rebuild_search_index(project: Project) -> None:
    """Rebuild the full-text index for a project after files change.

    Falls back gracefully when the storage engine lacks FTS
    support: the call is a no-op and search falls back to LIKE.
    """
    try:
        from app.services.search import rebuild_project_index
    except ImportError:  # pragma: no cover - optional module
        return
    rebuild_project_index(project.id)


def _finalize_import(project: Project, rows: list[dict]) -> Project:
    """Persist extracted rows and rebuild the search index."""
    _replace_project_files(project, rows)
    project.file_count = len(rows)
    project.status = STATUS_READY
    project.imported_at = datetime.now(UTC)
    db.session.flush()
    _rebuild_search_index(project)
    return project


def import_archive(
    workspace_id: int,
    filename: str,
    raw: bytes,
    *,
    name: str | None = None,
) -> Project:
    """Import an archive into a new or existing project row."""
    content_hash = archive_hash(raw)
    existing = find_duplicate_archive(workspace_id, content_hash)
    if existing is not None:
        return existing

    rows = extract_archive(io.BytesIO((raw)), filename)
    project = Project(
        workspace_id=workspace_id,
        name=name or filename,
        source="archive",
        content_hash=content_hash,
        status=STATUS_READY,
    )
    db.session.add(project)
    db.session.flush()
    return _finalize_import(project, rows)


def import_github_repo(
    workspace_id: int,
    full_name: str,
    default_branch: str,
    fetcher,
) -> Project:
    """Import a GitHub repository via a callable fetcher."""
    existing = find_duplicate_github(workspace_id, full_name, default_branch)
    if existing is not None:
        return existing

    try:
        rows = fetcher(full_name, default_branch)
    except GitHubError as exc:
        raise ProjectImportError(str(exc)) from exc

    project = Project(
        workspace_id=workspace_id,
        name=full_name,
        source=SOURCE_GITHUB,
        source_url=full_name,
        default_branch=default_branch,
        status=STATUS_READY,
    )
    db.session.add(project)
    db.session.flush()
    return _finalize_import(project, rows)
