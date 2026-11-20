# Plugin artifact download and cache

`app/services/plugin_artifact_cache.py` downloads a plugin artifact once and
keeps it on disk under its sha256 digest.

## Flow

1. Normalize the catalog digest (`sha256:<hex>`, `sha256-<hex>`, or bare hex).
2. On a hit, re-hash the cached file. A corrupt or truncated entry is deleted
   and downloaded again; it is never served.
3. On a miss, download through the existing bounded, SSRF-guarded transport in
   `plugin_artifacts.download_artifact`.
4. Optionally verify a detached Ed25519 publisher signature with the existing
   `plugin_signatures.enforce_publisher_trust` path.
5. Reject the artifact when it exceeds `max_bytes`.
6. Compare the computed and declared digests in constant time. A mismatch raises
   `ArtifactHashMismatchError` and nothing is written.
7. Write atomically (temp file + rename), then bound the cache directory.

## Usage

```python
from app.services.plugin_artifact_cache import PluginArtifactCache

cache = PluginArtifactCache(
    "/var/cache/aica/plugins",
    capacity_bytes=256 * 1024 * 1024,
    max_age_seconds=7 * 24 * 3600,
)
entry = cache.get("https://plugins.example.com/demo-1.0.0.zip", "sha256:...")
print(entry.path, entry.cached)
```

Pass `manifest`, `signature`, `trusted_publishers`, and `trust_policy` to
verify a signed artifact before it is cached. A test or offline caller can pass
`fetcher=callable` to replace the network download.

## Errors

All failures are `PluginArtifactCacheError` subclasses with a stable `code`:

| Error | `code` | Meaning |
| --- | --- | --- |
| `ArtifactNotFoundError` | `not_found` | Source is missing/unreadable |
| `ArtifactTooLargeError` | `too_large` | Artifact exceeds the size cap |
| `ArtifactHashMismatchError` | `hash_mismatch` | Bytes do not match the digest |

Invalid configuration and a signature supplied without a manifest raise
`PluginArtifactCacheError`; signature failures keep their existing
`PublisherSignatureError` types.

## Bounds and eviction

`capacity_bytes` bounds the total size of `*.zip` entries in the cache root.
Expired entries (older than `max_age_seconds`) are removed first, then the
least-recently-used entries until the directory fits. The entry just written or
served is never evicted during that call.

## Tests

`tests/test_plugin_artifact_cache.py` covers cache hits (download once),
corrupt-entry repair, hash mismatch, size bound, missing artifact, LRU and stale
eviction, and signature acceptance/rejection.
