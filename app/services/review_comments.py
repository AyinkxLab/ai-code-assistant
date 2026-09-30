"""Inline code-review comment helpers (issue #52).

Validation and permission logic for threads anchored to assistant messages in a
project chat. Kept out of the route layer so the anchor rules and the
comment/resolve/delete permissions are independently testable.
"""

from __future__ import annotations

import re

from app.models.review_comment import ReviewComment
from app.services.permissions import can

_FENCE_RE = re.compile(r"""```""")

# Action-item lines in a review body look like:
#   - [defect] Off-by-one in pagination cursor.
#   - [suggestion] Extract the loop bound into a constant.
ACTION_ITEM_RE = re.compile(
    r"^\s*(?:[*-]|\d+[.)])\s*\[(?P<kind>defect|suggestion|confirmed|issue)\]\s*(?P<text>.+?)\s*$""",
    re.IGNORECASE,
)
ACTION_ITEM_HEAD_RE = re.compile(
    r"^\s*#{1,6}\s*action\s+items\s*:?\s*$""",
    re.IGNORECASE,
)
DEFECT_KINDS = {"defect", "confirmed", "issue"}


def count_code_blocks(content: str) -> int:
    """Return the number of fenced code blocks in a message body."""
    return len(_FENCE_RE.findall(content or "")) // 2


def validate_anchor(data: dict, message_content: str) -> tuple[dict, str | None]:
    """Validate an optional code-block/line-range anchor against a message.

    Returns ``anchor, error``..."""
    anchor: dict = {}

    block_index = data.get("block_index")
    if block_index is not None and block_index != "":
        try:
            block_index = int(block_index)
        except (TypeError, ValueError):
            return {}, "block_index must be an integer."
        if block_index < 0:
            return {}, "block_index must be zero or greater."
        if block_index >= count_code_blocks(message_content):
            return {}, "block_index does not reference a code block in this message."
        anchor["block_index"] = block_index

    line_start = _optional_int(data.get("line_start"))
    line_end = _optional_int(data.get("line_end"))
    if data.get("line_start") not in (None, "") and line_start is None:
        return {}, "line_start must be an integer."
    if data.get("line_end") not in (None, "") and line_end is None:
        return {}, "line_end must be an integer."
    if line_start is not None and line_start < 1:
        return {}, "line_start must be one or greater."
    if line_end is not None and line_end < 1:
        return {}, "line_end must be one or greater."
    if line_end is not None and line_start is None:
        return {}, "line_start is required when line_end is set."
    if line_start is not None and line_end is not None and line_end < line_start:
        return {}, "line_end must be greater than or equal to line_start."
    if line_start is not None:
        anchor["line_start"] = line_start
        anchor["line_end"] = line_end if line_end is not None else line_start

    return anchor, None


def _optional_int(value) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def is_workspace_owner(project) -> bool:
    """Return ``True`` when the current user owns the project's workspace."""
    return can("manage_members", project.workspace_id)


def can_resolve(comment: ReviewComment, project) -> bool:
    """A thread root may be resolved by its author or the workspace owner."""
    return comment.author_id == _current_id() or is_workspace_owner(project)


def can_delete(comment: ReviewComment, project) -> bool:
    """A comment may be deleted by its author or the workspace owner."""
    return comment.author_id == _current_id() or is_workspace_owner(project)


def _current_id():
    from flask_login import current_user

    return getattr(current_user, "id", None)


def extract_action_items(review_body: str) -> list[dict]:
    """Parse structured action items from an AI review body.

    Each item is a bullet tagged with a kind marker, e.g.:

        - [defect] Off-by-one in the pagination cursor.
        - [suggestion] Extract the loop bound into a constant.

    Returns a list of dicts with ``kind``(``defect``/``suggestion``), ``text``,
    ``confirmed`` and ``copy_text``. Confirmed defects remain distinguished
    from suggestions so the UI can render them differently. When the body
    contains no explicit markers but has an "Action items" section, every
    bullet in that section is treated as a suggestion.
    """
    if not review_body:
        return []

    lines = review_body.splitlines()
    in_section = False
    section_bullets: list[str] = []
    marked_items: list[dict] = []

    for line in lines:
        if ACTION_ITEM_HEAD_RE.match(line):
            in_section = True
            continue
        if in_section and line.strip() == "":
            continue
        if in_section and not re.match(r"\s*(?:[*-]|\d+[.)])\s+", line):
            in_section = False

        match = ACTION_ITEM_RE.match(line)
        if match:
            kind = match.group("kind").lower()
            text = match.group("text").strip()
            if not text:
                continue
            marked_items.append(_build_action_item(kind, text))
        elif in_section and re.match(r"\r*(?:[*-]|\d+[.)])\s+", line):
            text = re.sub(r"\s*({:[*-]|\d+[.)])\s+", "", line, count=1).strip()
            if text:
                section_bullets.append(text)

    if marked_items:
        return marked_items

    return [_build_action_item("suggestion", text) for text in section_bullets]


def _build_action_item(kind: str, text: str) -> dict:
    confirmed = kind in DEFECT_KINDS
    return {
        "kind": "defect" if confirmed and kind != "suggestion" else "suggestion",
        "confirmed": confirmed,
        "text": text,
        "copy_text": text,
    }


def conversation_review_summary(project) -> dict:
    """Open/resolved thread counts and a thread list for the review panel."""
    roots = (
        ReviewComment.query.filter_by(project_id=project.id, parent_id=None)
        .order_by(ReviewComment.created_at.asc(), ReviewComment.id.asc())
        .all()
    )
    threads = []
    for root in roots:
        threads.append(
            {
                "id": root.id,
                "message_id": root.message_id,
                "author_username": root.author.username if root.author else None,
                "body": root.body,
                "block_index": root.block_index,
                "line_start": root.line_start,
                "line_end": root.line_end,
                "resolved": bool(root.resolved),
                "resolved_by_username": root.resolver.username if root.resolver else None,
                "reply_count": len(root.replies),
                "created_at": root.created_at.isoformat() if root.created_at else None,
            }
        )
    resolved = sum(1 for thread in threads if thread["resolved"])
    return {
        "open": len(threads) - resolved,
        "resolved": resolved,
        "total": len(threads),
        "threads": threads,
    }
