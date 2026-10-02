"""Persisted upload routes (issue #42).

Uploads are validated with the same extension allowlist used by
``/tools/analyze``, written to ``UPLOAD_FOLDER`` under a random stored name,
and tracked in the ``files`` table. Every read, download, attach, and analyze
is scoped to the owning user; another user receives a 404, never the file.
"""

from __future__ import annotations

import secrets
from pathlib import Path

from flask import current_app, jsonify, request, send_file
from flask_login import current_user, login_required
from werkzeug.utils import secure_filename

from app.extensions import db
from app.files import bp
from app.models import Conversation, StoredFile, WorkspaceMember
from app.models.workspace_member import STATUS_ACTIVE
from app.services.llm import LLMProviderError, get_provider
from app.tools.routes import ACTION_PROMPTS, ALLOWED_EXTENSIONS

#: Text read from a stored file is bounded the same way as /tools/analyze.
MAX_ANALYZE_CHARS = 200_000


def _upload_root() -> Path:
    return Path(current_app.config["UPLOAD_FOLDER"])


def _max_bytes() -> int:
    """The effective size cap: the smaller of the two configured limits."""
    limits = [
        current_app.config.get("MAX_CONTENT_LENGTH") or 0,
        current_app.config.get("UPLOAD_MAX_BYTES") or 0,
    ]
    positive = [limit for limit in limits if limit > 0]
    return min(positive) if positive else 16 * 1024 * 1024


def _is_allowed(filename: str) -> bool:
    """Reuse the /tools/analyze extension allowlist."""
    return Path(filename).suffix.lstrip(".").lower() in ALLOWED_EXTENSIONS


def _owned_or_404(file_id: int) -> StoredFile:
    return StoredFile.query.filter_by(id=file_id, user_id=current_user.id).first_or_404()


def _can_access_workspace(workspace_id: int) -> bool:
    return (
        WorkspaceMember.query.filter_by(
            workspace_id=workspace_id, user_id=current_user.id, status=STATUS_ACTIVE
        ).first()
        is not None
    )


def _stored_path(record: StoredFile) -> Path:
    return _upload_root() / record.stored_name


@bp.route("", methods=["POST"])
@login_required
def upload_file():
    """Persist an uploaded file.

    Accepts ``multipart/form-data`` with a ``file`` field and an optional
    ``workspace_id``. The type is checked against the ``/tools/analyze``
    allowlist and the size against the configured cap before anything is
    written.
    """
    file = request.files.get("file")
    if file is None or not file.filename:
        return jsonify({"error": "No file was uploaded."}), 400
    if not _is_allowed(file.filename):
        return jsonify({"error": "Unsupported file type."}), 400

    raw = file.read()
    if len(raw) == 0:
        return jsonify({"error": "The uploaded file is empty."}), 400
    if len(raw) > _max_bytes():
        return jsonify({"error": "File is too large."}), 400

    workspace_id = request.form.get("workspace_id", type=int)
    if workspace_id is not None and not _can_access_workspace(workspace_id):
        return jsonify({"error": "You do not have access to that workspace."}), 403

    original_name = secure_filename(file.filename) or "upload.txt"
    suffix = Path(original_name).suffix.lower()
    stored_name = f"{secrets.token_hex(16)}{suffix}"

    root = _upload_root()
    root.mkdir(parents=True, exist_ok=True)
    (root / stored_name).write_bytes(raw)

    record = StoredFile(
        user_id=current_user.id,
        workspace_id=workspace_id,
        original_name=original_name,
        stored_name=stored_name,
        size=len(raw),
        content_type=file.mimetype or "application/octet-stream",
    )
    db.session.add(record)
    db.session.commit()
    return jsonify(record.to_dict()), 201


@bp.route("", methods=["GET"])
@login_required
def list_files():
    """List only the current user's uploaded files, newest first."""
    records = (
        StoredFile.query.filter_by(user_id=current_user.id)
        .order_by(StoredFile.created_at.desc(), StoredFile.id.desc())
        .all()
    )
    return jsonify([record.to_dict() for record in records])


@bp.route("/<int:file_id>", methods=["GET"])
@login_required
def get_file(file_id: int):
    """Return metadata for an owned file (404 for anyone else's)."""
    return jsonify(_owned_or_404(file_id).to_dict())


@bp.route("/<int:file_id>/download", methods=["GET"])
@login_required
def download_file(file_id: int):
    """Stream an owned file back to its owner."""
    record = _owned_or_404(file_id)
    path = _stored_path(record)
    if not path.exists():
        return jsonify({"error": "The stored file is no longer available."}), 404
    return send_file(
        path,
        as_attachment=True,
        download_name=record.original_name,
        mimetype=record.content_type,
    )


@bp.route("/<int:file_id>/attach", methods=["POST"])
@login_required
def attach_file(file_id: int):
    """Attach an owned file to one of the caller's conversations.

    Detaching is expressed by passing ``conversation_id: null``.
    """
    record = _owned_or_404(file_id)
    data = request.get_json(silent=True) or {}
    conversation_id = data.get("conversation_id")
    if conversation_id is None:
        record.conversation_id = None
        db.session.commit()
        return jsonify(record.to_dict())
    conversation = Conversation.query.filter_by(id=conversation_id, user_id=current_user.id).first()
    if conversation is None:
        return jsonify({"error": "Conversation not found."}), 404
    record.conversation_id = conversation.id
    db.session.commit()
    return jsonify(record.to_dict())


@bp.route("/<int:file_id>/analyze", methods=["POST"])
@login_required
def analyze_stored_file(file_id: int):
    """Re-analyze a stored file without re-uploading it."""
    record = _owned_or_404(file_id)
    path = _stored_path(record)
    if not path.exists():
        return jsonify({"error": "The stored file is no longer available."}), 404
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return jsonify({"error": "File must be UTF-8 encoded text."}), 400
    except OSError as exc:
        return jsonify({"error": f"Could not read the stored file: {exc}"}), 500

    if len(text) > MAX_ANALYZE_CHARS:
        return jsonify({"error": "File is too large to analyze (max 200,000 characters)."}), 400

    action = (request.get_json(silent=True) or {}).get("action") or "explain"
    if action not in ACTION_PROMPTS or action == "generate":
        action = "explain"
    system = ACTION_PROMPTS[action]

    try:
        provider = get_provider()
        result = provider.complete(
            [
                {"role": "system", "content": "You are a helpful AI coding assistant."},
                {
                    "role": "user",
                    "content": f"{system}\n\nFile: {record.original_name}\n\nCode:\n{text}",
                },
            ]
        )
    except LLMProviderError as exc:
        return jsonify(
            {
                "file_id": record.id,
                "action": action,
                "result": f"[provider error] {exc}",
            }
        )

    return jsonify({"file_id": record.id, "action": action, "result": result})
