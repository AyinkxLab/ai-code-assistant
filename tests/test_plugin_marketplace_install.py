"""Signed marketplace artifact installation (#361).

The service resolves an allowlisted catalog entry, verifies the detached
Ed25519 signature and the artifact sha256 through the bounded plugin transport,
and registers only the validated manifest - never code and never a grant.
"""

import base64
import hashlib
import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.models import ActivityEvent, CapabilityGrant, Plugin, PluginLockEntry, User, Workspace
from app.services.plugin_ops import MarketplaceInstallRefused, install_from_marketplace
from app.services.plugin_signatures import canonical_manifest_body

PUBLISHER = "acme-labs"


def _manifest(plugin_id="signed-plugin", version="1.0.0", **overrides):
    manifest = {
        "id": plugin_id,
        "name": "Signed Plugin",
        "version": version,
        "description": "A signed test plugin",
        "author": "Tester",
        "entry_point": "plugins.signed:SignedPlugin",
        "capabilities": ["PROJECT_READ"],
    }
    manifest.update(overrides)
    return manifest


def _publisher():
    key = Ed25519PrivateKey.generate()
    raw = key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return key, base64.b64encode(raw).decode()


def _sign(key, manifest, publisher=PUBLISHER):
    return {
        "publisher": publisher,
        "algorithm": "ed25519",
        "value": base64.b64encode(key.sign(canonical_manifest_body(manifest))).decode(),
    }


def _entry(artifact, manifest, key=None, *, integrity=None, signature=True, **overrides):
    entry = {
        "id": manifest["id"],
        "version": manifest["version"],
        "source": f"https://plugins.example.com/{manifest['id']}-{manifest['version']}.zip",
        "integrity": integrity or "sha256:" + hashlib.sha256(artifact).hexdigest(),
        "manifest": manifest,
    }
    if signature and key is not None:
        entry["signature"] = _sign(key, manifest)
    entry.update(overrides)
    return entry


class _Response:
    def __init__(self, content, status_code=200, headers=None):
        self.content = content
        self.status_code = status_code
        self.headers = headers or {}


class _Transport:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def _config(tmp_path, entries, *, policy="required"):
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"version": 1, "plugins": entries}), encoding="utf-8")
    return {
        "PLUGIN_CATALOG_FILE": str(catalog),
        "PLUGIN_NETWORK_ALLOWLIST": "plugins.example.com",
        "PLUGIN_NETWORK_TIMEOUT": 5,
        "PLUGIN_NETWORK_MAX_BYTES": 4096,
        "PLUGIN_TRUSTED_KEYS": {},
        "PLUGIN_TRUST_POLICY": policy,
    }


def _workspace(db):
    user = User(username="owner", email="owner@example.com")
    user.set_password("supersecret123")
    db.session.add(user)
    db.session.commit()
    workspace = Workspace(user_id=user.id, name="WS")
    db.session.add(workspace)
    db.session.commit()
    return workspace


def test_signed_artifact_installs_metadata_and_audits(tmp_path, db):
    workspace = _workspace(db)
    key, public = _publisher()
    artifact = b"zipped-plugin-bytes"
    manifest = _manifest()
    config = _config(tmp_path, [_entry(artifact, manifest, key)])
    config["PLUGIN_TRUSTED_KEYS"] = {PUBLISHER: public}
    transport = _Transport(_Response(artifact))

    plugin, entry = install_from_marketplace(
        "signed-plugin",
        "1.0.0",
        workspace_id=workspace.id,
        config=config,
        transport=transport,
    )

    assert plugin.id == "signed-plugin"
    assert plugin.trust_state == "Trusted"
    assert entry.integrity == "sha256:" + hashlib.sha256(artifact).hexdigest()
    assert len(transport.calls) == 1
    assert PluginLockEntry.query.filter_by(workspace_id=workspace.id).one().version == "1.0.0"
    assert CapabilityGrant.query.count() == 0
    assert ActivityEvent.query.filter_by(event_type="plugin.capability.granted").count() == 1


def test_hash_mismatch_refuses_and_leaves_no_plugin(tmp_path, db):
    workspace = _workspace(db)
    key, public = _publisher()
    artifact = b"good"
    manifest = _manifest()
    config = _config(
        tmp_path,
        [_entry(artifact, manifest, key, integrity="sha256:" + "a" * 64)],
    )
    config["PLUGIN_TRUSTED_KEYS"] = {PUBLISHER: public}

    with pytest.raises(MarketplaceInstallRefused, match="hash mismatch"):
        install_from_marketplace(
            "signed-plugin",
            config=config,
            workspace_id=workspace.id,
            transport=_Transport(_Response(b"tampered")),
        )

    assert Plugin.query.count() == 0
    assert PluginLockEntry.query.count() == 0
    assert ActivityEvent.query.filter_by(event_type="plugin.denied").count() == 1


def test_required_policy_refuses_unsigned_artifact(tmp_path, db):
    workspace = _workspace(db)
    artifact = b"unsigned"
    manifest = _manifest()
    config = _config(tmp_path, [_entry(artifact, manifest, signature=False)])
    transport = _Transport(_Response(artifact))

    with pytest.raises(Exception, match="signature is required"):
        install_from_marketplace(
            "signed-plugin",
            config=config,
            workspace_id=workspace.id,
            transport=transport,
        )

    assert transport.calls == []
    assert Plugin.query.count() == 0
    assert ActivityEvent.query.filter_by(event_type="plugin.denied").count() == 1


def test_if_present_policy_accepts_unsigned_artifact(tmp_path, db):
    workspace = _workspace(db)
    artifact = b"unsigned"
    manifest = _manifest()
    config = _config(
        tmp_path,
        [_entry(artifact, manifest, signature=False)],
        policy="if-present",
    )

    plugin, _ = install_from_marketplace(
        "signed-plugin",
        config=config,
        workspace_id=workspace.id,
        transport=_Transport(_Response(artifact)),
    )

    assert plugin.trust_state == "Unverified"


def test_untrusted_publisher_is_refused(tmp_path, db):
    workspace = _workspace(db)
    key, _public = _publisher()
    artifact = b"signed"
    manifest = _manifest()
    config = _config(tmp_path, [_entry(artifact, manifest, key)])

    with pytest.raises(Exception, match="signature"):
        install_from_marketplace(
            "signed-plugin",
            config=config,
            workspace_id=workspace.id,
            transport=_Transport(_Response(artifact)),
        )

    assert Plugin.query.count() == 0


def test_allowlist_refusal_happens_before_the_transport(tmp_path, db):
    workspace = _workspace(db)
    key, public = _publisher()
    artifact = b"signed"
    manifest = _manifest()
    entry = _entry(artifact, manifest, key, source="https://evil.example.com/x.zip")
    config = _config(tmp_path, [entry])
    config["PLUGIN_TRUSTED_KEYS"] = {PUBLISHER: public}
    transport = _Transport(_Response(artifact))

    with pytest.raises(MarketplaceInstallRefused, match="allowlist"):
        install_from_marketplace(
            "signed-plugin",
            config=config,
            workspace_id=workspace.id,
            transport=transport,
        )

    assert transport.calls == []
    assert Plugin.query.count() == 0
