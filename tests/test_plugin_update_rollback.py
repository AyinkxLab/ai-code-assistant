"""Plugin update and rollback (#363)."""

import base64
import hashlib
import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.models import ActivityEvent, CapabilityGrant, Plugin, PluginLockEntry, User, Workspace
from app.services.plugin_compat import IncompatibleCompatibilityError
from app.services.plugin_ops import install_from_marketplace, rollback_plugin, update_plugin
from app.services.plugin_signatures import canonical_manifest_body


def _manifest(version, *, compatibility=None):
    manifest = {
        "id": "updatable",
        "name": "Updatable",
        "version": version,
        "description": "Updatable test plugin",
        "author": "Tester",
        "entry_point": "plugins.updatable:Plugin",
        "capabilities": ["PROJECT_READ"],
    }
    if compatibility is not None:
        manifest["compatibility"] = compatibility
    return manifest


def _publisher():
    key = Ed25519PrivateKey.generate()
    raw = key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return key, base64.b64encode(raw).decode()


def _entry(artifact, manifest, key):
    return {
        "id": manifest["id"],
        "version": manifest["version"],
        "source": f"https://plugins.example.com/updatable-{manifest['version']}.zip",
        "integrity": "sha256:" + hashlib.sha256(artifact).hexdigest(),
        "manifest": manifest,
        "signature": {
            "publisher": "acme",
            "algorithm": "ed25519",
            "value": base64.b64encode(key.sign(canonical_manifest_body(manifest))).decode(),
        },
    }


class _Response:
    def __init__(self, content):
        self.content = content
        self.status_code = 200
        self.headers = {}


def _transport(artifacts):
    class Transport:
        def __call__(self, url, **kwargs):
            return _Response(artifacts[url])

    return Transport()


def _config(tmp_path, entries, public):
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"version": 1, "plugins": entries}), encoding="utf-8")
    return {
        "PLUGIN_CATALOG_FILE": str(catalog),
        "PLUGIN_NETWORK_ALLOWLIST": "plugins.example.com",
        "PLUGIN_TRUSTED_KEYS": {"acme": public},
        "PLUGIN_TRUST_POLICY": "required",
    }


def _workspace(db):
    user = User(username="updater", email="updater@example.com")
    user.set_password("supersecret123")
    db.session.add(user)
    db.session.commit()
    workspace = Workspace(user_id=user.id, name="WS")
    db.session.add(workspace)
    db.session.commit()
    return workspace


def _install(workspace, tmp_path, version, *, compatibility=None):
    key, public = _publisher()
    artifact = f"artifact-{version}".encode()
    manifest = _manifest(version, compatibility=compatibility)
    entries = [_entry(artifact, manifest, key)]
    config = _config(tmp_path, entries, public)
    plugin, _ = install_from_marketplace(
        "updatable",
        version,
        workspace_id=workspace.id,
        config=config,
        transport=_transport({entries[0]["source"]: artifact}),
    )
    return plugin, config, entries, artifact


def test_update_to_compatible_version_and_rollback(tmp_path, db):
    workspace = _workspace(db)
    _plugin, _config1, _entries1, _artifact1 = _install(workspace, tmp_path, "1.0.0")

    key, public = _publisher()
    artifact = b"artifact-2.0.0"
    manifest = _manifest("2.0.0")
    entries = [_entry(artifact, manifest, key)]
    config = _config(tmp_path, entries, public)

    plugin = update_plugin(
        "updatable",
        "2.0.0",
        workspace_id=workspace.id,
        config=config,
    )
    assert plugin.version == "2.0.0"
    entry = PluginLockEntry.query.filter_by(workspace_id=workspace.id).one()
    assert entry.version == "2.0.0"
    assert entry.previous_version == "1.0.0"
    assert CapabilityGrant.query.count() == 0

    # Rollback works from the retained manifest even without a catalog.
    rolled, _ = rollback_plugin("updatable", workspace_id=workspace.id, config={})
    assert rolled.version == "1.0.0"
    refreshed = PluginLockEntry.query.filter_by(workspace_id=workspace.id).one()
    assert refreshed.version == "1.0.0"
    assert ActivityEvent.query.count() >= 3  # install, update and rollback audits


def test_incompatible_update_is_refused_and_keeps_current(tmp_path, db):
    workspace = _workspace(db)
    _install(workspace, tmp_path, "1.0.0")

    key, public = _publisher()
    artifact = b"artifact-2.0.0"
    manifest = _manifest("2.0.0", compatibility=">=99")
    entries = [_entry(artifact, manifest, key)]
    config = _config(tmp_path, entries, public)

    with pytest.raises(IncompatibleCompatibilityError):
        update_plugin(
            "updatable",
            "2.0.0",
            workspace_id=workspace.id,
            config=config,
        )

    assert db.session.get(Plugin, "updatable").version == "1.0.0"
    assert PluginLockEntry.query.filter_by(workspace_id=workspace.id).one().version == "1.0.0"
