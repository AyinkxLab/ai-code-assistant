"""Bulk import/export of prompt templates (#47).

Imports accept a ``.json`` file (an array of ``{title, content, category}``
objects) or a ``.csv`` file with ``title``/``content`` (and optional
``category``) columns. Every row is validated independently: valid rows become
prompts owned by the importing user and invalid rows are reported per-row,
without aborting the whole import.

The parser is intentionally strict about the *shape* of the payload (malformed
JSON, a non-array, a missing header, or an unsupported file type is a hard
error) but lenient about individual rows, so a partially-invalid file still
imports what it can.
"""

from __future__ import annotations

import csv
import io
import json
from typing import Any

from app.extensions import db
from app.models import Prompt
from app.services import prompt_versions as prompt_versions_service

#: Hard cap on the number of rows accepted in a single import.
MAX_IMPORT_ROWS = 1000
#: Field limits mirrored from the prompt API (title <= 200, category <= 80).
MAX_TITLE_LENGTH = 200
MAX_CATEGORY_LENGTH = 80
DEFAULT_CATEGORY = "General"

_ALLOWED_EXTENSIONS = (".json", ".csv")


class PromptImportError(ValueError):
    """Raised when an import payload cannot be parsed at all."""


def _normalize_row(raw: Any) -> tuple[dict | None, str | None]:
    """Validate a single mapping; return ``(data, error)`` (one is None)."""
    if not isinstance(raw, dict):
        return None, "row is not an object"
    title = str(raw.get("title") or "").strip()
    content = str(raw.get("content") or "").strip()
    category = str(raw.get("category") or "").strip()
    if not title:
        return None, "missing title"
    if not content:
        return None, "missing content"
    return (
        {
            "title": title[:MAX_TITLE_LENGTH],
            "content": content,
            "category": category[:MAX_CATEGORY_LENGTH] or DEFAULT_CATEGORY,
        },
        None,
    )


def _parse_json(raw: bytes) -> list:
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PromptImportError("The file is not valid JSON.") from exc
    if not isinstance(data, list):
        raise PromptImportError("JSON import must be an array of prompt objects.")
    return data


def _parse_csv(raw: bytes) -> list:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise PromptImportError("The file is not valid UTF-8 text.") from exc
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise PromptImportError("CSV import must have a header row.")

    columns = {name.strip().lower(): name for name in reader.fieldnames if name}
    if "title" not in columns or "content" not in columns:
        raise PromptImportError("CSV import must include 'title' and 'content' columns.")

    rows = []
    for record in reader:
        rows.append(
            {
                "title": record.get(columns["title"]),
                "content": record.get(columns["content"]),
                "category": record.get(columns["category"]) if "category" in columns else None,
            }
        )
    return rows


def import_prompts(user_id: int, filename: str, raw: bytes) -> dict:
    """Import prompts for ``user_id`` from ``raw``.

    Returns ``{"imported", "skipped", "errors", "message"}``. Raises
    :class:`PromptImportError` for a payload that cannot be parsed at all.
    """
    name = (filename or "").lower()
    if name.endswith(".json"):
        rows = _parse_json(raw)
    elif name.endswith(".csv"):
        rows = _parse_csv(raw)
    else:
        raise PromptImportError("Unsupported file type. Upload a .json or .csv file.")

    if len(rows) > MAX_IMPORT_ROWS:
        raise PromptImportError(f"Too many rows (maximum {MAX_IMPORT_ROWS}).")

    imported = 0
    errors: list[dict] = []
    for index, raw_row in enumerate(rows, start=1):
        data, error = _normalize_row(raw_row)
        if error is not None:
            errors.append({"row": index, "error": error})
            continue
        prompt = Prompt(
            user_id=user_id,
            title=data["title"],
            content=data["content"],
            category=data["category"],
        )
        db.session.add(prompt)
        db.session.flush()
        prompt_versions_service.record_version(prompt, changed_by=user_id)
        imported += 1

    db.session.commit()

    message = f"{imported} imported, {len(errors)} skipped"
    if errors:
        details = "; ".join(f"row {item['row']}: {item['error']}" for item in errors)
        message = f"{message} ({details})"
    return {"imported": imported, "skipped": len(errors), "errors": errors, "message": message}
