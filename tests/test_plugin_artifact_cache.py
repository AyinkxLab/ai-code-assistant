"""Tests for the download-once plugin artifact cache (#686)."""

import base64
import hashlib
import os
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from app.services.plugin_artifact_cache import (
    ArtifactHashMismatchError,
    ArtifactNotFoundError,
    ArtifactTooLargeError,
    PluginArtifactCache,
    PluginArtifactCacheError,
)
from app.services.plugin_artifacts import ArtifactError
from app.services.plugin_signatures import (
    InvalidPublisherSignatureError,
    canonical_manifest_body,
)

PAYLOAD_A = b"artifact-a" * 10
PAYLOAD_B = b"artifact-b" * 10
PAYLOAD_C = b"artifact-c" * 10
DIGEST_A = hashlib.sha256(PAYLOAD_A).hexdigest()
DIGEST_B = hashlib.sha256(PAYLOAD_B).hexdigest()
DIGEST_C = hashlib.sha256(PAYLOAD_C).hexdigest()


class Fetcher:
    def __init__(self, files):
        self.files = files
        self.calls = []

    def __call__(self, source):
        self.calls.append(source)
        value = self.files[source]
        if isinstance(value, Exception):
            raise value
        return value


def _cache(tmp_path, fetcher, **kwargs):
    return PluginArtifactCache(tmp_path / "cache", fetcher=fetcher, **kwargs)


def _age(path, seconds_ago):
    timestamp = time.time() - seconds_ago
    os.utime(path, (timestamp, timestamp))


class TestCacheHit:
    def test_downloads_once_then_serves_from_cache(self, tmp_path):
        fetcher = Fetcher({"url": PAYLOAD_A})
        cache = _cache(tmp_path, fetcher)

        first = cache.get("url", DIGEST_A)
        second = cache.get("url", DIGEST_A)

        assert first.cached is False
        assert second.cached is True
        assert fetcher.calls == ["url"]
        assert second.path == first.path
        assert second.path.read_bytes() == PAYLOAD_A

    def test_corrupt_cache_entry_is_rejected_and_refetched(self, tmp_path):
        fetcher = Fetcher({"url": PAYLOAD_A})
        cache = _cache(tmp_path, fetcher)
        entry = cache.get("url", DIGEST_A)
        entry.path.write_bytes(b"tampered")

        repaired = cache.get("url", DIGEST_A)

        assert repaired.cached is False
        assert repaired.path.read_bytes() == PAYLOAD_A
        assert fetcher.calls == ["url", "url"]


class TestRejections:
    def test_hash_mismatch_is_structured(self, tmp_path):
        cache = _cache(tmp_path, Fetcher({"url": PAYLOAD_A}))

        with pytest.raises(ArtifactHashMismatchError) as exc:
            cache.get("url", DIGEST_B)

        assert exc.value.code == "hash_mismatch"
        assert exc.value.expected == DIGEST_B
        assert exc.value.actual == DIGEST_A
        assert not list(cache.root.glob("*.zip"))

    def test_oversized_artifact_is_rejected(self, tmp_path):
        cache = _cache(tmp_path, Fetcher({"url": PAYLOAD_A}))

        with pytest.raises(ArtifactTooLargeError) as exc:
            cache.get("url", DIGEST_A, max_bytes=8)

        assert exc.value.code == "too_large"
        assert not list(cache.root.glob("*.zip"))

    def test_missing_artifact_is_structured(self, tmp_path):
        cache = _cache(tmp_path, Fetcher({"url": FileNotFoundError("gone")}))

        with pytest.raises(ArtifactNotFoundError) as exc:
            cache.get("url", DIGEST_A)

        assert exc.value.code == "not_found"

    def test_invalid_digest_is_rejected(self, tmp_path):
        cache = _cache(tmp_path, Fetcher({"url": PAYLOAD_A}))
        with pytest.raises(ArtifactError):
            cache.get("url", "not-a-digest")

    def test_invalid_configuration_is_structured(self, tmp_path):
        with pytest.raises(PluginArtifactCacheError):
            PluginArtifactCache(tmp_path, capacity_bytes=0)


class TestEviction:
    def test_least_recently_used_entry_is_evicted(self, tmp_path):
        fetcher = Fetcher({"a": PAYLOAD_A, "b": PAYLOAD_B, "c": PAYLOAD_C})
        cache = _cache(tmp_path, fetcher, capacity_bytes=250)

        entry_a = cache.get("a", DIGEST_A)
        _age(entry_a.path, 300)
        entry_b = cache.get("b", DIGEST_B)
        _age(entry_b.path, 200)
        entry_c = cache.get("c", DIGEST_C)

        assert entry_a.path.exists() is False
        assert entry_b.path.exists() is True
        assert entry_c.path.exists() is True

    def test_stale_entry_is_evicted_even_below_capacity(self, tmp_path):
        fetcher = Fetcher({"a": PAYLOAD_A, "b": PAYLOAD_B})
        cache = _cache(tmp_path, fetcher, capacity_bytes=10_000, max_age_seconds=60)

        entry_a = cache.get("a", DIGEST_A)
        _age(entry_a.path, 120)
        entry_b = cache.get("b", DIGEST_B)

        assert entry_a.path.exists() is False
        assert entry_b.path.exists() is True


class TestSignatureVerification:
    def _signed(self):
        key = Ed25519PrivateKey.generate()
        public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        manifest = {"id": "demo", "version": "1.0.0", "entry_point": "demo.plugin:Demo"}
        signature = {
            "publisher": "acme",
            "algorithm": "ed25519",
            "value": base64.b64encode(key.sign(canonical_manifest_body(manifest))).decode(),
        }
        trusted = {"acme": base64.b64encode(public).decode()}
        return manifest, signature, trusted

    def test_valid_signature_caches_the_artifact(self, tmp_path):
        manifest, signature, trusted = self._signed()
        cache = _cache(tmp_path, Fetcher({"url": PAYLOAD_A}))

        entry = cache.get(
            "url",
            DIGEST_A,
            manifest=manifest,
            signature=signature,
            trusted_publishers=trusted,
            trust_policy="required",
        )

        assert entry.path.read_bytes() == PAYLOAD_A

    def test_invalid_signature_is_rejected_before_caching(self, tmp_path):
        manifest, signature, trusted = self._signed()
        signature = dict(signature, value=base64.b64encode(b"forged" * 8).decode())
        cache = _cache(tmp_path, Fetcher({"url": PAYLOAD_A}))

        with pytest.raises(InvalidPublisherSignatureError):
            cache.get(
                "url",
                DIGEST_A,
                manifest=manifest,
                signature=signature,
                trusted_publishers=trusted,
                trust_policy="required",
            )

        assert not list(cache.root.glob("*.zip"))

    def test_signature_without_a_manifest_is_rejected(self, tmp_path):
        cache = _cache(tmp_path, Fetcher({"url": PAYLOAD_A}))
        with pytest.raises(PluginArtifactCacheError, match="manifest"):
            cache.get("url", DIGEST_A, signature={"publisher": "acme"})
