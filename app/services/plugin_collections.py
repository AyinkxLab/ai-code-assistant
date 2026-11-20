"""Maintainer-curated plugin collections (#694).

Collections are marketplace metadata only: a bounded name, description, ordered
plugin ids and a ``featured`` flag. Creating or editing one never installs a
plugin, downloads an artifact or grants a capability. Unknown plugin ids are
kept (the catalog may advertise them before they are registered) and render as
unavailable rather than being silently dropped.

Validation is centralized here so the HTML curation UI and the JSON API cannot
drift. Errors carry a stable ``code`` and HTTP ``status`` and are safe to return
to the client; see :func:`collection_error_payload`.
"""

from __future__ import annotations

import re

from app.extensions import db
from app.models import Plugin, PluginCollection

MAX_SLUG_LENGTH = 64
MAX_NAME_LENGTH = 120
MAX_DESCRIPTION_LENGTH = 280
MAX_PLUGINS_PER_COLLECTION = 50

_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_PLUGIN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class CollectionError(ValueError):
    """Invalid collection input or state."""

    code = "invalid"
    status = 400
    user_message = "The collection is invalid."


class CollectionNotFoundError(CollectionError):
    code = "not_found"
    status = 404
    user_message = "The collection was not found."


class CollectionConflictError(CollectionError):
    code = "conflict"
    status = 409
    user_message = "A collection with this slug already exists."


def collection_error_payload(exc: CollectionError) -> dict[str, str]:
    """Return the safe error shape for the API (never leaks internals)."""
    return {"error": str(exc) or exc.user_message, "code": exc.code}


def slugify(value: str) -> str:
    """Return a bounded, URL-safe slug (empty when nothing usable remains)."""
    slug = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    return slug[:MAX_SLUG_LENGTH].strip("-")


def _require_text(value, field: str, *, max_length: int, required: bool) -> str:
    if value is None:
        if required:
            raise CollectionError(f"{field} is required.")
        return ""
    if not isinstance(value, str):
        raise CollectionError(f"{field} must be a string.")
    text = value.strip()
    if not text and required:
        raise CollectionError(f"{field} is required.")
    if len(text) > max_length:
        raise CollectionError(f"{field} must be at most {max_length} characters.")
    return text


def _normalize_plugin_ids(value) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise CollectionError("plugin_ids must be a list.")
    if len(value) > MAX_PLUGINS_PER_COLLECTION:
        raise CollectionError(
            f"A collection may contain at most {MAX_PLUGINS_PER_COLLECTION} plugins."
        )
    plugin_ids: list[str] = []
    for raw in value:
        if not isinstance(raw, str):
            raise CollectionError("Every plugin id must be a string.")
        plugin_id = raw.strip()
        if not _PLUGIN_ID_RE.fullmatch(plugin_id):
            raise CollectionError(f"Invalid plugin id: {raw[:64]}")
        if plugin_id in plugin_ids:
            raise CollectionError(f"Duplicate plugin id: {plugin_id}")
        plugin_ids.append(plugin_id)
    return plugin_ids


def validate_collection(data: dict, *, partial: bool = False) -> dict:
    """Validate and normalize a collection payload.

    ``partial=True`` only validates the keys that are present (used by update).
    Returns normalized fields; the caller persists them.
    """
    if not isinstance(data, dict):
        raise CollectionError("A JSON object is required.")
    result: dict = {}

    if "name" in data or not partial:
        result["name"] = _require_text(
            data.get("name"), "name", max_length=MAX_NAME_LENGTH, required=True
        )
    if "description" in data or not partial:
        result["description"] = _require_text(
            data.get("description") or "",
            "description",
            max_length=MAX_DESCRIPTION_LENGTH,
            required=False,
        )
    if "featured" in data or not partial:
        featured = data.get("featured", False)
        if not isinstance(featured, bool):
            raise CollectionError("featured must be a boolean.")
        result["featured"] = featured
    if "plugin_ids" in data or not partial:
        result["plugin_ids"] = _normalize_plugin_ids(data.get("plugin_ids"))

    if "slug" in data and data.get("slug") not in (None, ""):
        slug = _require_text(data.get("slug"), "slug", max_length=MAX_SLUG_LENGTH, required=True)
        if not _SLUG_RE.fullmatch(slug):
            raise CollectionError("slug may contain lowercase letters, digits and single dashes.")
        result["slug"] = slug
    elif not partial:
        slug = slugify(result["name"])
        if not slug:
            raise CollectionError("slug could not be derived from name.")
        result["slug"] = slug

    return result


def _ensure_unique_slug(slug: str, *, exclude_id: int | None = None) -> None:
    query = PluginCollection.query.filter_by(slug=slug)
    if exclude_id is not None:
        query = query.filter(PluginCollection.id != exclude_id)
    if query.first() is not None:
        raise CollectionConflictError(f"A collection with slug {slug!r} already exists.")


def create_collection(data: dict, *, actor_id: int | None = None) -> PluginCollection:
    """Validate and persist a new collection."""
    fields = validate_collection(data)
    _ensure_unique_slug(fields["slug"])
    collection = PluginCollection(
        slug=fields["slug"],
        name=fields["name"],
        description=fields["description"],
        featured=fields["featured"],
        plugin_ids=fields["plugin_ids"],
        created_by_id=actor_id,
    )
    db.session.add(collection)
    db.session.commit()
    return collection


def update_collection(collection: PluginCollection, data: dict) -> PluginCollection:
    """Validate and apply a partial update to ``collection``."""
    fields = validate_collection(data, partial=True)
    if "slug" in fields:
        _ensure_unique_slug(fields["slug"], exclude_id=collection.id)
    for key, value in fields.items():
        setattr(collection, key, value)
    db.session.commit()
    return collection


def delete_collection(collection: PluginCollection) -> None:
    db.session.delete(collection)
    db.session.commit()


def get_collection(identifier: str | int) -> PluginCollection:
    """Resolve a collection by numeric id or slug, or raise a typed 404."""
    if isinstance(identifier, int) or (isinstance(identifier, str) and identifier.isdigit()):
        collection = db.session.get(PluginCollection, int(identifier))
    else:
        collection = PluginCollection.query.filter_by(slug=str(identifier)).first()
    if collection is None:
        raise CollectionNotFoundError("The collection was not found.")
    return collection


def list_collections(*, featured_only: bool = False) -> list[PluginCollection]:
    """Return collections, featured first, then by name and id."""
    query = PluginCollection.query
    if featured_only:
        query = query.filter_by(featured=True)
    return query.order_by(
        PluginCollection.featured.desc(),
        PluginCollection.name,
        PluginCollection.id,
    ).all()


def resolved_plugins(collection: PluginCollection) -> list[dict]:
    """Return display metadata for the collection's plugin ids.

    Missing plugins are reported as unavailable with the id as a fallback name;
    they are never invented or hidden. One query covers the whole collection.
    """
    plugin_ids = list(collection.plugin_ids or [])
    if not plugin_ids:
        return []
    rows = Plugin.query.filter(Plugin.id.in_(plugin_ids)).all()
    by_id = {row.id: row for row in rows}
    result = []
    for plugin_id in plugin_ids:
        row = by_id.get(plugin_id)
        if row is None:
            result.append(
                {
                    "id": plugin_id,
                    "name": plugin_id,
                    "version": None,
                    "description": "",
                    "available": False,
                }
            )
            continue
        result.append(
            {
                "id": row.id,
                "name": row.name,
                "version": row.version,
                "description": row.description or "",
                "available": True,
            }
        )
    return result


def serialize_collection(collection: PluginCollection, *, with_plugins: bool = False) -> dict:
    """Serialize a collection for the API/UI, optionally with plugin metadata."""
    payload = collection.to_dict()
    payload["plugins"] = resolved_plugins(collection) if with_plugins else []
    return payload


__all__ = [
    "MAX_DESCRIPTION_LENGTH",
    "MAX_NAME_LENGTH",
    "MAX_PLUGINS_PER_COLLECTION",
    "MAX_SLUG_LENGTH",
    "CollectionConflictError",
    "CollectionError",
    "CollectionNotFoundError",
    "collection_error_payload",
    "create_collection",
    "delete_collection",
    "get_collection",
    "list_collections",
    "resolved_plugins",
    "serialize_collection",
    "slugify",
    "update_collection",
    "validate_collection",
]
