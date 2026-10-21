"""Read-only marketplace catalog (Phase 9, issue #360).

The marketplace advertises installable plugins without installing anything.
A catalog is either:

* a local JSON file (``PLUGIN_MARKETPLACE_CATALOG``, default
  ``plugins/marketplace.json``), which works fully offline; or
* an optional remote HTTPS document (``PLUGIN_MARKETPLACE_URL``) fetched only
  through :mod:`app.services.plugin_network` (https-only, allowlisted host,
  bounded timeout and size, no redirects).

Every entry uses the installer catalog shape understood by
:mod:`app.services.plugin_catalog`::

    {
      "version": 1,
      "plugins": [
        {
          "id": "hello-world",
          "version": "0.1.0",
          "source": "https://plugins.example.com/hello-world-0.1.0.zip",
          "integrity": "sha256:...",
          "manifest": { ...plugins/plugin.schema.json... },
          "signature": {"publisher": "acme", "algorithm": "ed25519", "value": "..."}
        }
      ]
    }

The embedded manifest is validated against ``plugins/plugin.schema.json``. The
catalog is **untrusted data**: it is never executed and never used to grant a
capability. Installation still goes through the existing signed,
allowlisted, integrity-checked install path.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from packaging.version import Version

from app.config import BASE_DIR
from app.services.plugin_catalog import (
    CATALOG_VERSION,
    MAX_CATALOG_BYTES,
    MAX_CATALOG_ENTRIES,
    CatalogEntry,
    CatalogError,
    CatalogUnavailableError,
    load_catalog_file,
    load_remote_catalog,
    parse_catalog_entry,
)
from app.services.schema_validation import schema_errors

#: Default offline catalog shipped with the application (empty by default).
DEFAULT_CATALOG_PATH = BASE_DIR / "plugins" / "marketplace.json"

#: Manifest schema every advertised entry is validated against.
MANIFEST_SCHEMA_PATH = BASE_DIR / "plugins" / "plugin.schema.json"


class MarketplaceError(ValueError):
    """The marketplace catalog is unavailable or invalid.

    Every error carries a stable ``code``, HTTP ``status`` and sanitized
    ``user_message``. Raw details remain in ``str(exc)`` for logs and tests but
    are never copied into an API response; use :func:`marketplace_error_payload`.
    """

    code = "invalid"
    status = 400
    user_message = "The plugin catalog is invalid. Check the catalog file and try again."


class MarketplaceDisabledError(MarketplaceError):
    """The marketplace is disabled by configuration."""

    code = "disabled"
    status = 404
    user_message = "The plugin marketplace is disabled."


class MarketplaceUnavailableError(MarketplaceError):
    """The configured catalog cannot be read."""

    code = "unavailable"
    status = 404
    user_message = "The plugin catalog is unavailable. Please try again later."


class MarketplaceEntryNotFoundError(MarketplaceError):
    """The requested plugin is not advertised."""

    code = "not_found"
    status = 404
    user_message = "The requested plugin is not in the marketplace."


def marketplace_error_payload(exc: MarketplaceError) -> dict[str, str]:
    """Return the UI-safe error payload for a marketplace failure.

    Route handlers must use this instead of ``str(exc)`` so filesystem paths,
    remote URLs and parser details never reach the browser.
    """
    return {"error": exc.user_message, "code": exc.code}


@dataclass(frozen=True)
class MarketplaceEntry:
    """One validated, flattened marketplace entry."""

    plugin_id: str
    name: str
    version: str
    description: str
    author: str
    capabilities: tuple[str, ...]
    permissions: tuple[str, ...]
    dependencies: tuple[str, ...]
    compatibility: str | None
    integrity: str
    source: str
    signature: Mapping[str, str] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return the public entry shape (never includes executable content)."""
        data: dict[str, Any] = {
            "id": self.plugin_id,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "author": self.author,
            "capabilities": list(self.capabilities),
            "permissions": list(self.permissions),
            "dependencies": list(self.dependencies),
            "compatibility": self.compatibility,
            "integrity": self.integrity,
            "source": self.source,
        }
        if self.signature is not None:
            data["signature"] = dict(self.signature)
        return data


class MarketplaceCatalog:
    """An immutable collection of validated marketplace entries."""

    def __init__(self, entries: list[MarketplaceEntry]):
        self._entries = tuple(
            sorted(entries, key=lambda entry: (entry.plugin_id, Version(entry.version)))
        )
        by_id: dict[str, list[MarketplaceEntry]] = {}
        for entry in self._entries:
            by_id.setdefault(entry.plugin_id, []).append(entry)
        self._by_id = {key: tuple(value) for key, value in by_id.items()}

    def __len__(self) -> int:
        return len(self._entries)

    def list_entries(self) -> tuple[MarketplaceEntry, ...]:
        """Return every entry, sorted by plugin id then version."""
        return self._entries

    def get(self, plugin_id: str, version: str | None = None) -> MarketplaceEntry:
        """Return one entry, or the newest version when ``version`` is omitted."""
        matches = self._by_id.get(plugin_id, ())
        if not matches:
            raise MarketplaceEntryNotFoundError(f"Plugin {plugin_id!r} is not in the marketplace.")
        if version is None:
            return matches[-1]
        for entry in matches:
            if entry.version == version:
                return entry
        raise MarketplaceEntryNotFoundError(
            f"Plugin {plugin_id!r} version {version!r} is not in the marketplace."
        )


@lru_cache(maxsize=4)
def _load_manifest_schema(path: str) -> Mapping[str, Any]:
    try:
        with Path(path).open(encoding="utf-8") as handle:
            schema = json.load(handle)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MarketplaceError(f"plugin manifest schema is unreadable: {exc}") from exc
    if not isinstance(schema, Mapping):
        raise MarketplaceError("plugins/plugin.schema.json must be a JSON object")
    return schema


def _flatten(entry: CatalogEntry, index: int) -> MarketplaceEntry:
    """Validate an entry's manifest and flatten it for the read API."""
    schema = _load_manifest_schema(str(MANIFEST_SCHEMA_PATH))
    errors = schema_errors(entry.manifest, schema, f"plugins[{index}].manifest")
    if errors:
        raise MarketplaceError("; ".join(errors))

    manifest = entry.manifest
    return MarketplaceEntry(
        plugin_id=entry.plugin_id,
        name=str(manifest["name"]),
        version=entry.version,
        description=str(manifest["description"]),
        author=str(manifest["author"]),
        capabilities=tuple(str(item) for item in manifest.get("capabilities", ())),
        permissions=tuple(str(item) for item in manifest.get("permissions", ())),
        dependencies=tuple(str(item) for item in manifest.get("dependencies", ())),
        compatibility=(str(manifest["compatibility"]) if manifest.get("compatibility") else None),
        integrity=entry.integrity,
        source=entry.source,
        signature=entry.signature,
    )


def _catalog_source(config: Mapping[str, Any]) -> tuple[str, str]:
    """Return ``(kind, value)`` for the configured catalog source."""
    configured = config.get("PLUGIN_MARKETPLACE_CATALOG")
    if isinstance(configured, str) and configured.strip():
        return "file", configured.strip()
    url = config.get("PLUGIN_MARKETPLACE_URL")
    if isinstance(url, str) and url.strip():
        return "remote", url.strip()
    return "file", str(DEFAULT_CATALOG_PATH)


#: Process-local last-known-good catalogs, keyed by ``(source kind, source)``.
#: An offline source falls back to its last good catalog instead of disappearing
#: (#380). The cache is deliberately small and never persisted.
_LAST_GOOD_CATALOGS: dict[tuple[str, str], MarketplaceCatalog] = {}
_CATALOG_CACHE_LOCK = threading.Lock()


def reset_marketplace_cache() -> None:
    """Forget cached catalogs (tests and explicit configuration changes)."""
    with _CATALOG_CACHE_LOCK:
        _LAST_GOOD_CATALOGS.clear()


def _remember_catalog(kind: str, value: str, catalog: MarketplaceCatalog) -> None:
    with _CATALOG_CACHE_LOCK:
        _LAST_GOOD_CATALOGS[(kind, value)] = catalog


def _last_good_catalog(kind: str, value: str) -> MarketplaceCatalog | None:
    with _CATALOG_CACHE_LOCK:
        return _LAST_GOOD_CATALOGS.get((kind, value))


def load_marketplace(
    config: Mapping[str, Any],
    *,
    resolver: Any = None,
    transport: Any = None,
) -> MarketplaceCatalog:
    """Load and validate the configured marketplace catalog.

    Raises:
        MarketplaceDisabledError: When ``PLUGIN_MARKETPLACE_ENABLED`` is false.
        MarketplaceUnavailableError: When the catalog cannot be read or fetched.
        MarketplaceError: When the catalog or one of its manifests is invalid.
    """
    if not config.get("PLUGIN_MARKETPLACE_ENABLED", True):
        raise MarketplaceDisabledError("The plugin marketplace is disabled.")

    kind, value = _catalog_source(config)
    try:
        if kind == "remote":
            catalog = load_remote_catalog(config, resolver=resolver, transport=transport)
        else:
            catalog = load_catalog_file(value)
    except CatalogUnavailableError as exc:
        raise MarketplaceUnavailableError(str(exc)) from exc
    except CatalogError as exc:
        raise MarketplaceError(str(exc)) from exc

    entries = [_flatten(entry, index) for index, entry in enumerate(catalog.list_entries())]
    result = MarketplaceCatalog(entries)
    _remember_catalog(kind, value, result)
    return result


#: Bounded warnings returned by the lenient loader (#376).
MAX_MARKETPLACE_WARNINGS = 20


@dataclass(frozen=True)
class MarketplaceLoadResult:
    """A lenient catalog load: entries, bounded warnings and fallback state."""

    catalog: MarketplaceCatalog
    warnings: tuple[str, ...] = ()
    stale: bool = False
    unavailable: bool = False

    @property
    def state(self) -> str:
        """Return ``ok``, ``empty``, ``stale`` or ``unavailable`` for the API."""
        if self.stale:
            return "stale"
        if self.unavailable:
            return "unavailable"
        return "empty" if len(self.catalog) == 0 else "ok"


def _unavailable_result(
    kind: str,
    value: str,
    message: str,
    warnings: list[str],
) -> MarketplaceLoadResult:
    """Return the last good catalog, or an empty one, for an offline source."""
    _add_warning(warnings, message)
    cached = _last_good_catalog(kind, value)
    if cached is None:
        return MarketplaceLoadResult(MarketplaceCatalog([]), tuple(warnings), unavailable=True)
    _add_warning(warnings, "serving the last known good marketplace catalog")
    return MarketplaceLoadResult(cached, tuple(warnings), stale=True)


def _read_local_document(path: str) -> dict[str, Any] | None:
    """Read a local catalog, returning ``None`` for missing/unreadable files."""
    try:
        file_path = Path(path)
        if not file_path.is_file() or file_path.stat().st_size > MAX_CATALOG_BYTES:
            return None
        raw = file_path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise MarketplaceError("catalog file must be UTF-8 encoded") from exc
    except OSError:
        return None
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MarketplaceError(f"invalid catalog JSON: {exc}") from exc
    if not isinstance(document, Mapping):
        raise MarketplaceError("catalog must be a JSON object")
    if document.get("version", CATALOG_VERSION) != CATALOG_VERSION:
        raise MarketplaceError(
            f"unsupported catalog version {document.get('version')!r} "
            f"(expected {CATALOG_VERSION})"
        )
    entries = document.get("plugins")
    if not isinstance(entries, list):
        raise MarketplaceError("catalog 'plugins' must be a list")
    if len(entries) > MAX_CATALOG_ENTRIES:
        raise MarketplaceError(f"catalog must contain at most {MAX_CATALOG_ENTRIES} entries")
    return document


def _add_warning(warnings: list[str], message: str) -> None:
    if len(warnings) < MAX_MARKETPLACE_WARNINGS:
        warnings.append(message)


def load_marketplace_catalog(
    config: Mapping[str, Any],
    *,
    resolver: Any = None,
    transport: Any = None,
) -> MarketplaceLoadResult:
    """Load a catalog leniently for discovery (#374/#376).

    Missing or unreadable local files yield an **empty catalog without error**.
    Invalid individual entries are skipped and reported as bounded warnings;
    a malformed JSON document, wrong version, or non-list ``plugins`` still
    raise :class:`MarketplaceError` so operators can fix it.

    Remote catalogs use the same bounded transport as the strict loader.
    """
    if not config.get("PLUGIN_MARKETPLACE_ENABLED", True):
        raise MarketplaceDisabledError("The plugin marketplace is disabled.")

    warnings: list[str] = []
    kind, value = _catalog_source(config)

    if kind == "remote":
        try:
            catalog = load_remote_catalog(config, resolver=resolver, transport=transport)
        except CatalogUnavailableError as exc:
            return _unavailable_result(kind, value, str(exc), warnings)
        except CatalogError as exc:
            raise MarketplaceError(str(exc)) from exc
        entries = [_flatten(entry, index) for index, entry in enumerate(catalog.list_entries())]
        result = MarketplaceCatalog(entries)
        _remember_catalog(kind, value, result)
        return MarketplaceLoadResult(result, tuple(warnings))

    document = _read_local_document(value)
    if document is None:
        return _unavailable_result(
            kind,
            value,
            f"catalog file is missing or unreadable: {Path(value).name}",
            warnings,
        )

    entries: list[MarketplaceEntry] = []
    for index, raw_entry in enumerate(document["plugins"]):
        try:
            entry = parse_catalog_entry(raw_entry)
            entries.append(_flatten(entry, index))
        except (ValueError, TypeError, KeyError) as exc:
            _add_warning(warnings, f"plugins[{index}]: {exc}")
    result = MarketplaceCatalog(entries)
    _remember_catalog(kind, value, result)
    return MarketplaceLoadResult(result, tuple(warnings))


def filter_entries(
    catalog: MarketplaceCatalog,
    *,
    query: str | None = None,
    capability: str | None = None,
) -> list[MarketplaceEntry]:
    """Filter entries by free-text query and/or required capability.

    Filtering is case-insensitive and only inspects advertised metadata.
    """
    needle = (query or "").strip().lower()
    wanted = (capability or "").strip().upper()
    matches = []
    for entry in catalog.list_entries():
        if wanted and wanted not in {item.upper() for item in entry.capabilities}:
            continue
        if needle:
            haystack = " ".join(
                [entry.plugin_id, entry.name, entry.description, entry.author]
            ).lower()
            if needle not in haystack:
                continue
        matches.append(entry)
    return matches


__all__ = [
    "DEFAULT_CATALOG_PATH",
    "MANIFEST_SCHEMA_PATH",
    "MAX_MARKETPLACE_WARNINGS",
    "MarketplaceCatalog",
    "MarketplaceDisabledError",
    "MarketplaceEntry",
    "MarketplaceEntryNotFoundError",
    "MarketplaceError",
    "MarketplaceLoadResult",
    "MarketplaceUnavailableError",
    "filter_entries",
    "load_marketplace",
    "load_marketplace_catalog",
    "marketplace_error_payload",
    "reset_marketplace_cache",
]
