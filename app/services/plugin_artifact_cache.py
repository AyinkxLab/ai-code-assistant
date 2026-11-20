"""Download-once plugin artifact cache (#686).

A marketplace artifact is fetched at most once and stored under its sha256
digest. Every cache hit is re-hashed before use, so a file that was truncated or
tampered with on disk is rejected and re-downloaded rather than served.

The cache reuses the existing integrity and signing primitives:

* :func:`app.services.plugin_artifacts.download_artifact` — the bounded,
  SSRF-guarded download transport;
* :func:`app.services.plugin_artifacts.compute_sha256` — streaming hashing;
* :func:`app.services.plugin_signatures.enforce_publisher_trust` — optional
  Ed25519 verification before anything is written to disk.

This module never executes plugin code.

Disk usage is bounded by ``capacity_bytes``. Expired entries (older than
``max_age_seconds``) are removed first, then the least-recently-used entries
until the directory fits. A cache root is supplied by the caller, so a CLI or
installer can point several workers at a shared directory.
"""

from __future__ import annotations

import contextlib
import hmac
import os
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.services.plugin_artifacts import (
    ArtifactError,
    compute_sha256,
    download_artifact,
    normalize_sha256,
)

#: Default total cache capacity (bytes).
DEFAULT_CAPACITY_BYTES = 64 * 1024 * 1024

#: Default age after which an entry is stale, in seconds (7 days).
DEFAULT_MAX_AGE_SECONDS = 7 * 24 * 60 * 60

#: Suffix used for cached artifacts.
_CACHE_SUFFIX = ".zip"


class PluginArtifactCacheError(ArtifactError):
    """Base class for cache failures. ``code`` is stable for callers."""

    code = "artifact_cache_error"


class ArtifactNotFoundError(PluginArtifactCacheError):
    """The artifact is not available at the configured source."""

    code = "not_found"


class ArtifactTooLargeError(PluginArtifactCacheError):
    """The artifact exceeds the configured size cap."""

    code = "too_large"


class ArtifactHashMismatchError(PluginArtifactCacheError):
    """The downloaded bytes do not match the declared sha256 digest."""

    code = "hash_mismatch"

    def __init__(self, expected: str, actual: str, *, plugin_id: str | None = None) -> None:
        label = f" for plugin {plugin_id}" if plugin_id else ""
        super().__init__(f"Artifact sha256 mismatch{label}: expected {expected}, got {actual}")
        self.expected = expected
        self.actual = actual
        self.plugin_id = plugin_id


@dataclass(frozen=True)
class ArtifactCacheEntry:
    """A verified artifact on disk."""

    path: Path
    sha256: str
    size: int
    cached: bool

    def to_dict(self) -> dict[str, Any]:
        """Return JSON-serializable metadata."""
        return {
            "path": str(self.path),
            "sha256": self.sha256,
            "size": self.size,
            "cached": self.cached,
        }


#: A fetcher returns the raw artifact bytes for ``source``.
ArtifactFetcher = Callable[[str], bytes]


class PluginArtifactCache:
    """Content-addressed cache for plugin artifacts."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        capacity_bytes: int = DEFAULT_CAPACITY_BYTES,
        max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
        fetcher: ArtifactFetcher | None = None,
    ) -> None:
        if not isinstance(capacity_bytes, int) or capacity_bytes <= 0:
            raise PluginArtifactCacheError("Cache capacity must be a positive integer")
        if not isinstance(max_age_seconds, int) or max_age_seconds < 0:
            raise PluginArtifactCacheError("Cache max age must be a non-negative integer")
        self.root = Path(root)
        self.capacity_bytes = capacity_bytes
        self.max_age_seconds = max_age_seconds
        self.fetcher = fetcher

    def get(
        self,
        source: str,
        expected_sha256: str,
        *,
        max_bytes: int | None = None,
        manifest: Mapping[str, Any] | None = None,
        signature: Mapping[str, str] | None = None,
        trusted_publishers: Mapping[str, str | bytes] | None = None,
        trust_policy: str = "if-present",
    ) -> ArtifactCacheEntry:
        """Return the verified artifact, downloading it on a cache miss.

        Args:
            source: Operator-controlled artifact URL (or a test fetcher key).
            expected_sha256: Catalog digest (any spelling accepted by
                :func:`normalize_sha256`).
            max_bytes: Per-artifact cap; defaults to the cache capacity.
            manifest: Detached-signature manifest body, required when
                ``signature`` is supplied.
            signature: Detached Ed25519 signature metadata to verify before
                caching.
            trusted_publishers: Publisher id to configured public key.
            trust_policy: ``if-present`` or ``required``.

        Raises:
            ArtifactNotFoundError: The source could not be read/produced.
            ArtifactTooLargeError: The artifact exceeds ``max_bytes``.
            ArtifactHashMismatchError: The bytes do not match the digest.
            PluginArtifactCacheError: For an invalid cache configuration.
            PublisherSignatureError: When signature verification fails.
        """
        digest = normalize_sha256(expected_sha256)
        limit = self.capacity_bytes if max_bytes is None else int(max_bytes)
        if limit <= 0:
            raise PluginArtifactCacheError("Artifact max_bytes must be positive")

        self.root.mkdir(parents=True, exist_ok=True)
        target = self.root / f"{digest}{_CACHE_SUFFIX}"

        if target.is_file():
            if compute_sha256(target) == digest:
                _touch(target)
                self._evict(keep=target)
                return ArtifactCacheEntry(target, digest, target.stat().st_size, True)
            # Corrupt or truncated entry: never serve it, fetch again.
            target.unlink(missing_ok=True)

        data = self._fetch(source)
        if len(data) > limit:
            raise ArtifactTooLargeError(
                f"Artifact exceeds the {limit} byte cap (got {len(data)} bytes)"
            )

        if signature is not None:
            if manifest is None:
                raise PluginArtifactCacheError(
                    "A manifest is required to verify a detached signature"
                )
            from app.services.plugin_signatures import enforce_publisher_trust

            enforce_publisher_trust(
                manifest,
                signature,
                trusted_publishers=trusted_publishers,
                policy=trust_policy,
            )

        actual = compute_sha256(data)
        if not hmac.compare_digest(actual, digest):
            raise ArtifactHashMismatchError(digest, actual)

        self._write_atomic(target, data)
        self._evict(keep=target)
        return ArtifactCacheEntry(target, digest, len(data), False)

    def _fetch(self, source: str) -> bytes:
        if self.fetcher is not None:
            try:
                data = self.fetcher(source)
            except FileNotFoundError as exc:
                raise ArtifactNotFoundError(f"Artifact not found: {source}") from exc
        else:
            try:
                result = download_artifact(source)
            except FileNotFoundError as exc:
                raise ArtifactNotFoundError(f"Artifact not found: {source}") from exc
            data = result.get("content")
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise PluginArtifactCacheError("Artifact fetcher must return bytes")
        return bytes(data)

    @staticmethod
    def _write_atomic(target: Path, data: bytes) -> None:
        handle, temporary = tempfile.mkstemp(prefix=".artifact-", dir=target.parent)
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(data)
            os.replace(temporary, target)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise

    def _entries(self) -> list[Path]:
        try:
            return [path for path in self.root.iterdir() if path.suffix == _CACHE_SUFFIX]
        except OSError:
            return []

    def _evict(self, *, keep: Path | None = None) -> None:
        """Remove expired and then least-recently-used entries over capacity."""
        now = time.time()
        entries = self._entries()
        survivors: list[Path] = []
        for path in entries:
            try:
                age = now - path.stat().st_mtime
            except OSError:
                continue
            if self.max_age_seconds and age > self.max_age_seconds and path != keep:
                path.unlink(missing_ok=True)
            else:
                survivors.append(path)

        survivors.sort(key=lambda path: _mtime(path))
        total = sum(_size(path) for path in survivors)
        for path in survivors:
            if total <= self.capacity_bytes:
                break
            if path == keep:
                continue
            total -= _size(path)
            path.unlink(missing_ok=True)


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _touch(path: Path) -> None:
    with contextlib.suppress(OSError):
        os.utime(path, None)


__all__ = [
    "DEFAULT_CAPACITY_BYTES",
    "DEFAULT_MAX_AGE_SECONDS",
    "ArtifactCacheEntry",
    "ArtifactFetcher",
    "ArtifactHashMismatchError",
    "ArtifactNotFoundError",
    "ArtifactTooLargeError",
    "PluginArtifactCache",
    "PluginArtifactCacheError",
]
