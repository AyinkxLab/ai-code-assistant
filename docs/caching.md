# Read cache

Issue #698 adds a small in-process cache for hot reads that are safe to serve a
few seconds stale. It is not a response cache, an LLM cache, or a place for
secrets: callers must only cache bounded, non-secret values.

Implementation: `app/services/read_cache.py`.

## Bounds and expiry

| Setting | Default | Meaning |
| ------- | ------- | ------- |
| `READ_CACHE_ENABLED` | `1` | Disables every read/write when `0`. |
| `READ_CACHE_TTL` | `30` | Seconds an entry stays live. |
| `READ_CACHE_MAX_ENTRIES` | `128` | Hard LRU cap; oldest entries are evicted. |
| `READ_CACHE_NAMESPACE` | `app.read` | Prefix applied to every stored key. |

A copy is returned on every `get`, so a caller cannot mutate the cached value.
Stats (`hits`, `misses`, `sets`, `evictions`, `expirations`, `entries`) are
available from `read_cache.cache_stats()`.

## Key namespace

Callers pass a logical key and the cache prefixes it:

```text
app.read:workspace:<id>:settings
app.read:<entity>:<id>:<variant>
```

Use `invalidate_prefix("workspace:")` only when every workspace entry is stale.
Storage is per process and non-persistent; a restart or a second worker starts
cold, which is acceptable for a 30-second read cache.

## First use: workspace collaboration settings

`app/services/workspace_settings_cache.py` caches the serialized settings
snapshot (`workspace:<id>:settings`) used by the member/notification surfaces.
The ORM row is still read and written normally; only the snapshot is cached.

Invalidation is explicit:

- `_settings()` drops the snapshot when it creates the row for a workspace.
- `PUT /api/workspaces/<id>/settings` drops it after the commit.

Tests in `tests/test_read_cache.py` cover TTL expiry, LRU eviction, prefix
invalidation, a stale read after a direct database change, and the update route
returning the new value immediately after invalidation.
