"""Bounded in-process cache for hot, non-secret reads (#698).

The cache is intentionally small and dumb: a TTL plus an LRU entry cap, one
process-local store, and explicit invalidation. It is only for data that is
safe to serve slightly stale and contains no secrets or prompt text.

Key namespace
-------------
Every logical key is stored under ``READ_CACHE_NAMESPACE`` (default
``app.read``), so a key looks like::

    app.read:workspace:<id>:settings

Callers pass only the logical part (``workspace:<id>:settings``). Prefix
invalidation operates on that logical part and cannot accidentally clear
another namespace.

The workspace-settings read in ``app/services/workspace_settings_cache.py`` is
the first user: it is read on every member/notification page, changes rarely,
and is invalidated whenever the row is created or updated.
"""

from __future__ import annotations

import copy
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

DEFAULT_NAMESPACE = "app.read"
DEFAULT_TTL_SECONDS = 30
DEFAULT_MAX_ENTRIES = 128


@dataclass
class _Entry:
    value: object
    expires_at: float


class ReadCache:
    """A small, thread-safe, TTL + LRU cache with an explicit namespace."""

    def __init__(
        self,
        *,
        namespace: str = DEFAULT_NAMESPACE,
        ttl: int = DEFAULT_TTL_SECONDS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        enabled: bool = True,
        clock=time.monotonic,
    ) -> None:
        self.namespace = namespace
        self.ttl = max(0, int(ttl))
        self.max_entries = max(1, int(max_entries))
        self.enabled = bool(enabled)
        self._clock = clock
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0
        self.sets = 0
        self.evictions = 0
        self.expirations = 0

    def _full_key(self, key: str) -> str:
        return f"{self.namespace}:{key}"

    def get(self, key: str):
        """Return a live cached value, or ``None`` on miss/expiry/disabled."""
        if not self.enabled:
            return None
        full_key = self._full_key(key)
        with self._lock:
            entry = self._entries.get(full_key)
            if entry is None:
                self.misses += 1
                return None
            if entry.expires_at <= self._clock():
                del self._entries[full_key]
                self.expirations += 1
                self.misses += 1
                return None
            self._entries.move_to_end(full_key)
            self.hits += 1
            return copy.deepcopy(entry.value)

    def set(self, key: str, value) -> None:
        """Store ``value`` and evict the least-recently-used entry if needed."""
        if not self.enabled:
            return
        full_key = self._full_key(key)
        with self._lock:
            self._entries[full_key] = _Entry(copy.deepcopy(value), self._clock() + self.ttl)
            self._entries.move_to_end(full_key)
            self.sets += 1
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)
                self.evictions += 1

    def invalidate(self, key: str) -> None:
        """Drop one logical key."""
        with self._lock:
            self._entries.pop(self._full_key(key), None)

    def invalidate_prefix(self, prefix: str) -> None:
        """Drop every logical key starting with ``prefix``."""
        full_prefix = self._full_key(prefix)
        with self._lock:
            for full_key in [key for key in self._entries if key.startswith(full_prefix)]:
                del self._entries[full_key]

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def stats(self) -> dict:
        with self._lock:
            return {
                "enabled": self.enabled,
                "namespace": self.namespace,
                "ttl_seconds": self.ttl,
                "max_entries": self.max_entries,
                "entries": len(self._entries),
                "hits": self.hits,
                "misses": self.misses,
                "sets": self.sets,
                "evictions": self.evictions,
                "expirations": self.expirations,
            }


_cache = ReadCache()


def configure_read_cache(app) -> None:
    """Apply ``READ_CACHE_*`` config to the process-wide cache."""
    global _cache
    _cache = ReadCache(
        namespace=str(app.config.get("READ_CACHE_NAMESPACE", DEFAULT_NAMESPACE)),
        ttl=int(app.config.get("READ_CACHE_TTL", DEFAULT_TTL_SECONDS)),
        max_entries=int(app.config.get("READ_CACHE_MAX_ENTRIES", DEFAULT_MAX_ENTRIES)),
        enabled=bool(app.config.get("READ_CACHE_ENABLED", True)),
    )


def get_cache() -> ReadCache:
    return _cache


def get(key: str):
    return _cache.get(key)


def set(key: str, value) -> None:
    _cache.set(key, value)


def invalidate(key: str) -> None:
    _cache.invalidate(key)


def invalidate_prefix(prefix: str) -> None:
    _cache.invalidate_prefix(prefix)


def cache_stats() -> dict:
    return _cache.stats()


__all__ = [
    "DEFAULT_MAX_ENTRIES",
    "DEFAULT_NAMESPACE",
    "DEFAULT_TTL_SECONDS",
    "ReadCache",
    "cache_stats",
    "configure_read_cache",
    "get",
    "get_cache",
    "invalidate",
    "invalidate_prefix",
    "set",
]
