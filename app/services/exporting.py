"""Project snapshot export service (#107).

Builds a downloadable ZIP archive of a stored project entirely in memory and
streams it to the client: nothing is ever written to the filesystem. Stored
plain-text file contents are written back verbatim; binary and oversized files
(stored with ``content=None``) are included as small, clearly marked metadata
placeholder files so the archive preserves the full project structure for
backup or re-import purposes without inventing file contents.

Security invariants:

* the caller must already have resolved the project as the owner's own row;
* archives are generated from ``ProjectFile`` rows only — no filesystem,
  subprocess, or network access;
* member paths are re-sanitized with :func:`sanitize_member_path` before they
  are written into the archive, so a poisoned database row can never introduce
  traversal or absolute paths into a downloaded artifact;
* oversized/binary placeholder text is capped by ``PROJECT_EXPORT_PLACEHOLDER_MAX_CHARS``.
"""

from __future__ import annotations

import contextlib
import io
import json
import zipfile
from datetime import UTC, datetime

from flask import current_app

from app.models import ProjectFile
from app.services.importing import sanitize_member_path

PLACEHOLDER_SUFFIX = ".PLACEHOLDER.txt"


class ProjectExportError(RuntimeError):
    """Raised when a project snapshot cannot be exported."""


def _placeholder_text(file: ProjectFile) -> str:
    """Build the clearly marked placeholder body for a non-stored file."""
    max_chars = current_app.config["PROJECT_EXPORT_PLACEHOLDER_MAX_CHARS"]
    reasons = []
    if file.is_binary:
        reasons.append("detected as binary at import time")
    if file.content is None and not file.is_binary:
        reasons.append(
            f"larger than the stored-content cap "
            f"({current_app.config['PROJECT_MAX_FILE_CHARS']} characters)"
        )
    reason = " and ".join(reasons) or "not stored"
    lines = [
        "This file was NOT included in the export.",
        "",
        f"Reason: {reason}.",
        f"Original path: {file.path}",
        f"Original size: {file.size} bytes",
        f"Language: {file.language or 'unknown'}",
        "",
        "The full contents were never stored by the importer (binary or",
        "oversized files keep metadata only), so there is nothing to write",
        "here. Re-import the original project to restore this file.",
    ]
    text = "\n".join(lines) + "\n"
    return text[:max_chars]


def _manifest(project, included: int, placeholders: int, total_size: int) -> str:
    """Serialize a JSON manifest describing the export."""
    payload = {
        "kind": "ai-code-assistant-project-export",
        "version": 1,
        "project": {
            "id": project.id,
            "name": project.name,
            "source": project.source,
            "source_url": project.source_url,
            "status": project.status,
            "file_count": project.file_count,
            "total_size_bytes": project.total_size_bytes,
            "created_at": project.created_at.isoformat() if project.created_at else None,
        },
        "exported_at": datetime.now(UTC).isoformat(),
        "included_files": included,
        "placeholder_files": placeholders,
        "stored_content_bytes": total_size,
        "notes": [
            "Text files under the storage cap are included with their full stored contents.",
            "Binary and oversized files appear as <name>.PLACEHOLDER.txt metadata stubs.",
        ],
    }
    return json.dumps(payload, indent=2) + "\n"


def iter_export_zip(project):
    """Yield the exported archive for ``project`` as a byte generator.

    The generator writes one entry per stored file (plus the manifest) and
    never materializes the whole archive, so exports stay memory-bounded for
    any project size. Content is read per-row via ``.yield_per`` so SQLAlchemy
    streams rows from the database instead of loading them all at once.
    """
    buffer = io.BytesIO()
    archive = zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED)
    included = 0
    placeholders = 0
    stored_bytes = 0
    try:
        query = ProjectFile.query.filter_by(project_id=project.id).order_by(ProjectFile.path)
        for file in query.yield_per(50):
            try:
                path = sanitize_member_path(file.path)
            except ProjectExportError:
                path = ""
            if not path:
                # Unrepresentable path: emit the metadata as a placeholder
                # rather than silently dropping the row.
                path = f"unrepresentable/{file.id or 0}{PLACEHOLDER_SUFFIX}"
                text = _placeholder_text(file)
                placeholders += 1
                archive.writestr(path, text.encode("utf-8"))
                continue

            if file.content is None:
                placeholders += 1
                stub = _placeholder_text(file).encode("utf-8")
                archive.writestr(f"{path}{PLACEHOLDER_SUFFIX}", stub)
            else:
                included += 1
                stored_bytes += len(file.content.encode("utf-8"))
                archive.writestr(path, file.content.encode("utf-8"))

        archive.writestr(
            "EXPORT-MANIFEST.json",
            _manifest(project, included, placeholders, stored_bytes).encode("utf-8"),
        )
        archive.close()
        yield buffer.getvalue()
    finally:
        with contextlib.suppress(Exception):  # pragma: no cover - idempotent close
            archive.close()
        buffer.close()


def export_filename(project) -> str:
    """Build the download filename for a project export."""
    slug = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in project.name.lower())
    slug = slug.strip("-") or "project"
    return f"{slug[:80]}-{project.id}-export.zip"
