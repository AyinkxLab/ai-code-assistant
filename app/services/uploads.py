"""Persistence and validation for user uploads (issue #42).

The allowlist and text-size cap are the **same** ones the ``/tools/analyze``
route uses, so any file that can be analyzed inline can also be persisted here
and re-analyzed later.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from flask import current_app
from werkzeug.utils import secure_filename

from app.extensions import db
from app.models import UploadedFile

#: Source extensions accepted by ``/tools/analyze`` (single source of truth).
ALLOWED_EXTENSIONS = {
    "py",
    "js",
    "ts",
    "jsx",
    "tsx",
    "html",
    "css",
    "sql",
    "java",
    "go",
    "rs",
    "rb",
    "php",
    "c",
    "cpp",
    "h",
    "hpp",
    "cs",
    "sh",
    "json",
    "yaml",
    "yml",
    "toml",
    "md",
    "txt",
}

#: Maximum decoded text size, mirroring ``/tools/analyze``.
MAX_UPLOAD_CHARS = 200_000


class UploadError(Exception):
    """Raised when an upload fails validation."""


def is_allowed(filename: str) -> bool:
    """Return ``True`` when ``filename`` has an allowed extension."""
    return Path(filename).suffix.lstrip(".").lower() in ALLOWED_EXTENSIONS


def upload_dir() -> Path:
    """Return (creating on demand) the configured upload directory."""
    folder = Path(current_app.config["UPLOAD_FOLDER"])
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _new_stored_name(original: str) -> str:
    suffix = Path(original).suffix.lower()
    return f"{uuid.uuid4().hex}{suffix}"


def save_upload(
    file_storage,
    *,
    workspace_id: int,
    user_id: int,
    conversation_id: int | None = None,
) -> UploadedFile:
    """Validate and persist an uploaded file, returning its metadata row.

    Raises :class:`UploadError` with a safe message when validation fails.
    """
    if file_storage is None or not file_storage.filename:
        raise UploadError("No file was uploaded.")
    if not is_allowed(file_storage.filename):
        raise UploadError("Unsupported file type.")
    try:
        raw = file_storage.read()
    except OSError as exc:
        raise UploadError(f"Could not read the uploaded file: {exc}") from exc
    if not raw:
        raise UploadError("The uploaded file is empty.")
    max_bytes = int(current_app.config.get("MAX_CONTENT_LENGTH", 16 * 1024 * 1024))
    if len(raw) > max_bytes:
        raise UploadError("File is too large.")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UploadError("File must be UTF-8 encoded text.") from exc
    if len(text) > MAX_UPLOAD_CHARS:
        raise UploadError("File is too large to analyze (max 200,000 characters).")

    original_name = secure_filename(file_storage.filename) or "upload"
    stored_name = _new_stored_name(original_name)
    (upload_dir() / stored_name).write_bytes(raw)

    record = UploadedFile(
        workspace_id=workspace_id,
        user_id=user_id,
        conversation_id=conversation_id,
        original_name=original_name,
        stored_name=stored_name,
        size=len(raw),
        content_type=file_storage.mimetype or "application/octet-stream",
    )
    db.session.add(record)
    db.session.commit()
    return record


def path_for(record: UploadedFile) -> Path:
    """Return the on-disk path for ``record``."""
    return upload_dir() / record.stored_name


def read_text(record: UploadedFile) -> str:
    """Return the stored file's UTF-8 text, re-running the analyze checks."""
    path = path_for(record)
    if not path.exists():
        raise UploadError("The stored file is missing.")
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise UploadError("File must be UTF-8 encoded text.") from exc
    if len(text) > MAX_UPLOAD_CHARS:
        raise UploadError("File is too large to analyze (max 200,000 characters).")
    return text
