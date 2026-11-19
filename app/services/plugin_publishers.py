"""Publisher profile pages (#691).

A publisher identity is derived from the existing Phase 9 trust metadata: the
verified ``Plugin.trust_publisher`` when present, otherwise a slug of the
manifest author. Verification deliberately reuses ``PLUGIN_TRUSTED_KEYS`` (the
same trusted-publisher key model used by installation); there is no parallel
trust store here.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from app.models import Plugin

_SLUG_RE = re.compile(r"[^a-z0-9._-]+")
MAX_PAGE_LIMIT = 100
DEFAULT_PAGE_LIMIT = 50


def publisher_id_for_plugin(plugin: Plugin) -> str:
    """Return the canonical publisher id for ``plugin``.

    A verified publisher key id is used verbatim (it is already canonical).
    Otherwise the manifest author is lowercased into a stable slug.
    """
    if plugin.trust_publisher:
        return plugin.trust_publisher
    slug = _SLUG_RE.sub("-", (plugin.author or "").strip().lower()).strip("-._")
    return slug or "unknown"


def plugins_by_publisher(publisher_id: str) -> list[Plugin]:
    """Return every registered plugin published by ``publisher_id``."""
    wanted = (publisher_id or "").strip().lower()
    return sorted(
        (plugin for plugin in Plugin.query.all() if publisher_id_for_plugin(plugin) == wanted),
        key=lambda plugin: plugin.id,
    )


def publisher_profile(
    publisher_id: str,
    *,
    page: int = 1,
    limit: int = DEFAULT_PAGE_LIMIT,
    trusted_publishers=(),
    install_counts: Mapping[str, int] | None = None,
) -> dict:
    """Build a paginated publisher profile with trust and install counts.

    ``verified`` is true only when the publisher id is one of the configured
    trusted keys. ``verified_key_ids`` exposes publisher key ids (never key
    material), matching the read-only trusted-publisher API.
    """
    publisher_id = (publisher_id or "").strip().lower()
    page = max(1, page)
    limit = max(1, min(limit, MAX_PAGE_LIMIT))
    verified = publisher_id in set(trusted_publishers or ())
    counts = install_counts or {}
    plugins = plugins_by_publisher(publisher_id)

    total = len(plugins)
    pages = (total + limit - 1) // limit if total else 0
    offset = (page - 1) * limit
    page_plugins = plugins[offset : offset + limit]

    rows = []
    for plugin in page_plugins:
        rows.append(
            {
                "id": plugin.id,
                "name": plugin.name,
                "version": plugin.version,
                "description": plugin.description,
                "author": plugin.author,
                "trust_state": plugin.trust_state,
                "trust_publisher": plugin.trust_publisher,
                "publisher": publisher_id,
                "verified": verified,
                "installs": int(counts.get(plugin.id, 0) or 0),
            }
        )

    name = next((plugin.author for plugin in plugins if plugin.author), None) or publisher_id
    return {
        "publisher": {
            "id": publisher_id,
            "name": name,
            "verified": verified,
            "badge": "verified" if verified else "unverified",
            "verified_key_ids": [publisher_id] if verified else [],
        },
        "plugins": rows,
        "pagination": {
            "page": page,
            "limit": limit,
            "max_limit": MAX_PAGE_LIMIT,
            "total": total,
            "pages": pages,
            "has_next": page < pages,
            "has_prev": page > 1,
        },
    }


__all__ = [
    "DEFAULT_PAGE_LIMIT",
    "MAX_PAGE_LIMIT",
    "plugins_by_publisher",
    "publisher_id_for_plugin",
    "publisher_profile",
]
