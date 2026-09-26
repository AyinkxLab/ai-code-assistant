"""Per-user LLM response cache (issue #18).

Identical completion requests — same user, provider, model, params and prompt —
are served from an in-process cache instead of calling the provider again. This
cuts cost and latency for repeated questions without ever sharing a response
between users: the cache key includes the requesting user id.

The cache is deliberately conservative:

- It only caches non-streaming ``chat``/``complete`` calls. Streaming bypasses it.
- Entries expire after ``LLM_CACHE_TTL`` seconds and the store keeps at most
  ``LLM_CACHE_MAX_ENTRIES`` entries (LRU eviction).
- The key embeds a fingerprint of the user's active provider key, so rotating or
  replacing a key invalidates that user's cached responses. Changing the model
  is part of the key too.
- Callers can bypass it per request with ``no_cache=True``.
- Hit/miss/set/eviction counts are tracked and logged; :func:`cache_stats`
  exposes them for an endpoint or an operator.

Storage is per-process and non-persistent, which is appropriate for a response
cache: a restart or a second worker simply starts cold.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

from app.services.providers import get_provider
from app.services.providers.base import (
    ProviderResponse,
    message_content,
    message_role,
)

logger = logging.getLogger(__name__)

DEFAULT_TTL_SECONDS = 300
DEFAULT_MAX_ENTRIES = 256


def _canonical_messages(messages) -> list[dict]:
    """Canonicalize messages, keeping every field (e.g. image attachments).

    Message objects are reduced to ``role``/``content``; dict messages keep all
    of their keys so two requests that differ only by attached images do not
    collide in the cache.
    """
    canonical: list[dict] = []
    for message in messages:
        if isinstance(message, dict):
            canonical.append({key: message[key] for key in sorted(message)})
        else:
            canonical.append({"role": message_role(message), "content": message_content(message)})
    return canonical


def prompt_hash(messages) -> str:
    """Return a stable hash of the full message list."""
    canonical = json.dumps(
        _canonical_messages(messages),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _canonical_params(params) -> str:
    return json.dumps(
        params or {},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def request_signature(
    *,
    user_id,
    provider: str,
    model: str | None,
    params,
    messages,
    key_version: str = "",
) -> str:
    """Build the cache key for one completion request.

    Every field that can change the answer is included, and ``user_id`` makes the
    key user-scoped so responses can never be shared across accounts.
    """
    material = "\x1f".join(
        [
            str(user_id),
            str(provider or ""),
            str(model or ""),
            _canonical_params(params),
            prompt_hash(messages),
            str(key_version or ""),
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass
class _Entry:
    response: ProviderResponse
    expires_at: float


class LLMResponseCache:
    """A small, thread-safe, TTL + LRU response cache."""

    def __init__(
        self,
        *,
        ttl: int = DEFAULT_TTL_SECONDS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        enabled: bool = True,
        clock=time.monotonic,
    ) -> None:
        self._lock = threading.Lock()
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._clock = clock
        self.ttl = max(0, int(ttl))
        self.max_entries = max(1, int(max_entries))
        self.enabled = bool(enabled)
        self.hits = 0
        self.misses = 0
        self.sets = 0
        self.evictions = 0
        self.expirations = 0

    def get(self, signature: str) -> ProviderResponse | None:
        """Return a live cached response, or ``None`` on miss/expiry/disabled."""
        if not self.enabled:
            return None
        with self._lock:
            entry = self._entries.get(signature)
            if entry is None:
                self.misses += 1
                return None
            if entry.expires_at <= self._clock():
                del self._entries[signature]
                self.expirations += 1
                self.misses += 1
                return None
            self._entries.move_to_end(signature)
            self.hits += 1
            return entry.response

    def set(self, signature: str, response: ProviderResponse) -> None:
        """Store ``response`` and evict the least-recently-used entry if needed."""
        if not self.enabled:
            return
        with self._lock:
            self._entries[signature] = _Entry(response, self._clock() + self.ttl)
            self._entries.move_to_end(signature)
            self.sets += 1
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)
                self.evictions += 1

    def clear(self) -> None:
        """Drop every entry (keeps counters for observability)."""
        with self._lock:
            self._entries.clear()

    def stats(self) -> dict:
        """Return counters and configuration for logging/an endpoint."""
        with self._lock:
            return {
                "enabled": self.enabled,
                "ttl_seconds": self.ttl,
                "max_entries": self.max_entries,
                "entries": len(self._entries),
                "hits": self.hits,
                "misses": self.misses,
                "sets": self.sets,
                "evictions": self.evictions,
                "expirations": self.expirations,
            }


# Process-wide cache. Tests can build their own :class:`LLMResponseCache`, and
# ``configure_cache`` rebinds the module cache for an app instance.
_cache = LLMResponseCache()


def configure_cache(app) -> None:
    """Apply ``LLM_CACHE_*`` config to the process-wide cache."""
    global _cache
    _cache = LLMResponseCache(
        ttl=int(app.config.get("LLM_CACHE_TTL", DEFAULT_TTL_SECONDS)),
        max_entries=int(app.config.get("LLM_CACHE_MAX_ENTRIES", DEFAULT_MAX_ENTRIES)),
        enabled=bool(app.config.get("LLM_CACHE_ENABLED", True)),
    )


def get_cache() -> LLMResponseCache:
    """Return the process-wide cache (used by tests and the stats endpoint)."""
    return _cache


def cache_stats() -> dict:
    """Return the process-wide cache's counters."""
    return _cache.stats()


def _user_key_version(user, provider_name: str) -> str:
    """Fingerprint the user's active key for ``provider_name``.

    Rotating or replacing the key changes this string, which changes the cache
    key and therefore invalidates the user's cached responses.
    """
    if user is None:
        return ""
    try:
        from app.models import ApiKey
    except Exception:  # pragma: no cover - models are always importable in-app
        return ""
    key = (
        ApiKey.query.filter_by(
            user_id=getattr(user, "id", None), provider=provider_name, is_active=True
        )
        .order_by(ApiKey.updated_at.desc())
        .first()
    )
    if key is None:
        return ""
    updated = key.updated_at.isoformat() if key.updated_at else ""
    return f"{key.id}:{updated}"


def cached_chat(
    user,
    messages,
    *,
    provider=None,
    model: str | None = None,
    params=None,
    no_cache: bool = False,
) -> ProviderResponse:
    """Run a completion through the cache when enabled.

    Falls back to a direct provider call when caching is disabled, when the
    caller passes ``no_cache=True``, or when there is no user to scope the entry
    to.
    """
    provider = provider or get_provider()
    if not _cache.enabled or no_cache or user is None:
        return provider.chat(messages, model=model, params=params)

    signature = request_signature(
        user_id=getattr(user, "id", None),
        provider=provider.name,
        model=model,
        params=params,
        messages=messages,
        key_version=_user_key_version(user, provider.name),
    )
    cached = _cache.get(signature)
    if cached is not None:
        logger.info("llm_cache hit user=%s provider=%s", getattr(user, "id", None), provider.name)
        return cached

    response = provider.chat(messages, model=model, params=params)
    _cache.set(signature, response)
    logger.info("llm_cache miss user=%s provider=%s", getattr(user, "id", None), provider.name)
    return response


def cached_complete(
    user,
    messages,
    *,
    provider=None,
    model: str | None = None,
    params=None,
    no_cache: bool = False,
) -> str:
    """Convenience wrapper returning just the cached content string."""
    return cached_chat(
        user,
        messages,
        provider=provider,
        model=model,
        params=params,
        no_cache=no_cache,
    ).content
