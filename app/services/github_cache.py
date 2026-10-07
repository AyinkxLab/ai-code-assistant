"""Bounded in-memory ETag / conditional-request cache for GitHub responses (#76).

Repository listings, trees, and READMEs were re-downloaded in full on every page
load even though GitHub already sends an ``ETag`` for them. This module
revalidates with ``If-None-Match`` instead: a ``304 Not Modified`` replays the
cached body without re-transferring the payload, which also keeps the request
off GitHub's rate-limit quota.

Design notes:

- **Transport level.** :class:`CachedSession` proxies the client's
  ``requests.Session``, so all three request paths (``_request``,
  ``_get_paginated``, ``_get_page``) share one implementation and the ``Link``
  header that pagination depends on is captured and replayed alongside the body.
- **Per-auth cache keys.** The key embeds a SHA-256 fingerprint of the access
  token, so a private repository fetched by one user can never be replayed for
  another. Rotating, refreshing, or revoking a token changes the fingerprint and
  invalidates that account's cached responses.
- **Bounded growth.** Entries expire after ``GITHUB_RESPONSE_CACHE_TTL`` seconds,
  the store holds at most ``GITHUB_RESPONSE_CACHE_MAX_ENTRIES`` entries with LRU
  eviction, and a body larger than
  ``GITHUB_RESPONSE_CACHE_MAX_BODY_BYTES`` is never stored.
- **Opt out per endpoint.** ``/user`` is never cached: it is an authorization
  probe rather than browsing payload, and caching it would delay noticing a
  revoked or downgraded token.

Storage is per-process and non-persistent, which is appropriate for an HTTP
cache: a restart or a second worker simply starts cold.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 300
DEFAULT_MAX_ENTRIES = 256
DEFAULT_MAX_BODY_BYTES = 512 * 1024

#: Headers replayed with a cached body. ``ETag`` is the validator we sent, and
#: ``Link`` is what ``_get_paginated`` / ``_get_page`` parse for navigation, so
#: pagination metadata stays correct across a revalidated response.
REPLAY_HEADERS = ("ETag", "Link", "Content-Type")


@dataclass(frozen=True)
class CachedResponse:
    """A previously downloaded response, replayable when GitHub answers 304."""

    etag: str
    status_code: int
    content: bytes
    headers: dict[str, str]
    expires_at: float


def auth_fingerprint(access_token: str) -> str:
    """Return a stable one-way fingerprint of an access token.

    Used as part of every cache key so entries are scoped to the account that
    fetched them. Hashing rather than storing the token keeps the key safe to
    log, and changing the token automatically changes the key.
    """
    return hashlib.sha256(f"gho:{access_token}".encode()).hexdigest()


def _stringify(value) -> str:
    """Render one query value the way ``requests`` would put it on the wire."""
    if isinstance(value, (list, tuple, set)):
        return ",".join(str(item) for item in value)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _canonical_params(params) -> str:
    """Return a stable representation of a query string's parameters.

    Sorting keeps ``?a=1&b=2`` and ``?b=2&a=1`` on one cache entry; ``None`` and
    an empty mapping both canonicalize to ``""`` so the two spellings of "no
    parameters" do not each get their own entry.
    """
    if not params:
        return ""
    items = params.items() if isinstance(params, dict) else params
    canonical = sorted((str(key), _stringify(value)) for key, value in items)
    return json.dumps(canonical, separators=(",", ":"), default=str)


def cache_key(fingerprint: str, method: str, url: str, params=None) -> str:
    """Build the cache key for a request.

    The key is ``<fingerprint-prefix>:<digest>``. The fingerprint still feeds
    the digest, so for any given URL entries belonging to different accounts can
    never collide; keeping a short prefix in front additionally lets
    :meth:`GitHubResponseCache.invalidate_auth` drop every entry for one account
    by prefix scan, which a bare digest would not allow.
    """
    material = "\n".join((fingerprint, method.upper(), url, _canonical_params(params)))
    digest = hashlib.sha256(material.encode()).hexdigest()
    return f"{fingerprint[:16]}:{digest}"


def is_cacheable_url(url: str) -> bool:
    """Return whether ``url`` may be cached.

    The authenticated-identity endpoint is excluded: it is an authorization
    probe, and serving a stale copy could hide a revoked token for up to the
    cache TTL. A false negative only costs a cache miss.
    """
    path = urlparse(url).path.rstrip("/")
    return not path.endswith("/user")


def is_cacheable_response(response) -> bool:
    """Return whether a response may be stored.

    Honors an explicit ``Cache-Control: no-store``; GitHub's usual
    ``private, max-age=…`` is fine because entries are already keyed per token.
    """
    cache_control = (response.headers.get("Cache-Control") or "").lower()
    return "no-store" not in cache_control


def _replay_headers(response) -> dict[str, str]:
    """Copy the headers that must survive a revalidation onto the cache entry."""
    captured: dict[str, str] = {}
    for name in REPLAY_HEADERS:
        value = response.headers.get(name)
        if value:
            captured[name] = value
    return captured


def _apply_cached_body(response, entry: CachedResponse):
    """Rewrite a ``304`` in place so callers see an ordinary successful response.

    Mutating rather than synthesizing a new ``Response`` keeps the retry and
    error-translation logic in :mod:`app.services.github` untouched: it sees the
    original status, body, and ``Link`` header exactly as if the body had been
    downloaded again.
    """
    response.status_code = entry.status_code
    response._content = entry.content
    # The test doubles expose ``content`` as a plain attribute while
    # ``requests.Response`` exposes it as a read-only property.
    with contextlib.suppress(AttributeError):
        response.content = entry.content
    for name, value in entry.headers.items():
        response.headers[name] = value
    return response


class GitHubResponseCache:
    """Thread-safe, bounded, TTL'd store of GitHub responses keyed by ETag."""

    def __init__(
        self,
        *,
        ttl: int = DEFAULT_TTL_SECONDS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
        enabled: bool = True,
        clock=time.monotonic,
    ) -> None:
        self.ttl = ttl
        self.max_entries = max_entries
        self.max_body_bytes = max_body_bytes
        self.enabled = enabled
        self._clock = clock
        self._entries: OrderedDict[str, CachedResponse] = OrderedDict()
        self._lock = threading.Lock()

        self.hits = 0
        self.misses = 0
        self.sets = 0
        self.evictions = 0
        self.expirations = 0
        self.revalidations = 0
        self.oversized = 0

    def get(self, key: str) -> CachedResponse | None:
        """Return a live entry for ``key``, or ``None`` when absent or expired."""
        if not self.enabled:
            return None
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self.misses += 1
                return None
            if entry.expires_at <= self._clock():
                del self._entries[key]
                self.expirations += 1
                self.misses += 1
                return None
            self._entries.move_to_end(key)
            self.hits += 1
            return entry

    def set(
        self,
        key: str,
        *,
        etag: str,
        status_code: int,
        content: bytes,
        headers: dict[str, str] | None = None,
    ) -> None:
        """Store a response, evicting the least-recently-used entry if needed.

        Responses without an ``ETag`` are skipped: there is no validator to
        revalidate with, so caching them would only risk staleness.
        """
        if not self.enabled or not etag:
            return
        if len(content) > self.max_body_bytes:
            self.oversized += 1
            return
        with self._lock:
            self._entries[key] = CachedResponse(
                etag=etag,
                status_code=status_code,
                content=content,
                headers=dict(headers or {}),
                expires_at=self._clock() + self.ttl,
            )
            self._entries.move_to_end(key)
            self.sets += 1
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)
                self.evictions += 1

    def note_revalidation(self) -> None:
        """Record that a ``304`` was served from cache."""
        with self._lock:
            self.revalidations += 1

    def invalidate_auth(self, fingerprint: str) -> int:
        """Drop every entry belonging to one auth fingerprint.

        Called when a token is revoked, so a disconnected account's private
        repository data does not linger in process memory. Keys carry a short
        fingerprint prefix, which makes this a prefix scan; the return value is
        the number of entries removed.
        """
        prefix = f"{fingerprint[:16]}:"
        with self._lock:
            stale = [key for key in self._entries if key.startswith(prefix)]
            for key in stale:
                del self._entries[key]
        if stale:
            logger.info("Dropped %s cached GitHub response(s) for a revoked token", len(stale))
        return len(stale)

    def clear(self) -> None:
        """Drop every entry (keeps counters for observability)."""
        with self._lock:
            self._entries.clear()

    def stats(self) -> dict:
        """Return counters and configuration for logging or an endpoint."""
        with self._lock:
            return {
                "enabled": self.enabled,
                "ttl_seconds": self.ttl,
                "max_entries": self.max_entries,
                "max_body_bytes": self.max_body_bytes,
                "entries": len(self._entries),
                "hits": self.hits,
                "misses": self.misses,
                "sets": self.sets,
                "evictions": self.evictions,
                "expirations": self.expirations,
                "revalidations": self.revalidations,
                "oversized": self.oversized,
            }


class CachedSession:
    """Session proxy that revalidates ``GET`` responses with ``If-None-Match``.

    Only ``GET`` is cached: writes must not be replayed and GitHub's validators
    for those are not meaningful to store.
    """

    def __init__(self, session, cache: GitHubResponseCache, fingerprint: str) -> None:
        self._session = session
        self._cache = cache
        self._fingerprint = fingerprint
        # GitHubClient sets its auth/version headers on ``session.headers``.
        self.headers = session.headers

    def __getattr__(self, name):
        # Delegate everything else (mount adapters, cookies, close, ...).
        return getattr(self._session, name)

    def get(self, url, **kwargs):
        """Route ``Session.get`` through :meth:`request` so it is cached too.

        ``requests.Session.get`` normally delegates to ``request`` internally,
        but proxying it to the *wrapped* session would skip this cache. Two of
        the client's three request paths (``_get_paginated`` and ``_get_page``)
        call ``session.get`` directly, so this override is what makes
        repository listings, trees, and READMEs cacheable at all.
        """
        return self.request("GET", url, **kwargs)

    def request(self, method, url, *, params=None, **kwargs):
        method = method.upper()
        if method != "GET" or not is_cacheable_url(url):
            return self._session.request(method, url, params=params, **kwargs)

        key = cache_key(self._fingerprint, method, url, params)
        entry = self._cache.get(key)

        headers = kwargs.pop("headers", None)
        if entry is not None:
            merged = dict(headers or {})
            merged["If-None-Match"] = entry.etag
            headers = merged

        if headers is None:
            # Deliberately omit the keyword entirely: some injected test doubles
            # do not accept ``headers``, and a request with no validator to send
            # should look exactly like the uncached call did.
            response = self._session.request(method, url, params=params, **kwargs)
        else:
            response = self._session.request(method, url, params=params, headers=headers, **kwargs)

        if response.status_code == 304:
            if entry is None:
                # Nothing to replay (e.g. the entry expired mid-flight). Surface
                # the 304 as-is so the caller raises rather than inventing data.
                return response
            self._cache.note_revalidation()
            return _apply_cached_body(response, entry)

        if response.status_code == 200 and is_cacheable_response(response):
            etag = response.headers.get("ETag")
            if etag:
                self._cache.set(
                    key,
                    etag=etag,
                    status_code=200,
                    content=bytes(response.content or b""),
                    headers=_replay_headers(response),
                )

        return response


# Process-wide cache. ``configure_github_cache`` rebinds it for an app instance;
# tests can build their own :class:`GitHubResponseCache` or call :func:`reset`.
_cache = GitHubResponseCache()


def configure_github_cache(app) -> None:
    """Apply ``GITHUB_RESPONSE_CACHE_*`` config to the process-wide cache."""
    global _cache
    _cache = GitHubResponseCache(
        ttl=int(app.config.get("GITHUB_RESPONSE_CACHE_TTL", DEFAULT_TTL_SECONDS)),
        max_entries=int(app.config.get("GITHUB_RESPONSE_CACHE_MAX_ENTRIES", DEFAULT_MAX_ENTRIES)),
        max_body_bytes=int(
            app.config.get("GITHUB_RESPONSE_CACHE_MAX_BODY_BYTES", DEFAULT_MAX_BODY_BYTES)
        ),
        enabled=bool(app.config.get("GITHUB_RESPONSE_CACHE_ENABLED", True)),
    )


def get_cache() -> GitHubResponseCache:
    """Return the process-wide cache (used by the client and by tests)."""
    return _cache


def cache_stats() -> dict:
    """Return the process-wide cache's counters."""
    return _cache.stats()


def reset() -> None:
    """Clear all cached GitHub responses (used by tests)."""
    _cache.clear()
