"""Dependency-aware install ordering and the plugin lockfile (#362)."""

import base64
import hashlib
import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.extensions import db
from app.models import Plugin, PluginLockEntry, User, Workspace
from app.services.plugin_deps import (
    PluginDependencyCycleError,
    PluginDependencyVersionError,
    resolve_install_order,
)
from app.services.plugin_ops import install_from_marketplace
from app.services.plugin_signatures import canonical_manifest_body


def _manifest(plugin_id, version="1.0.0", dependencies=None):
    manifest = {
        "id": plugin_id,
        "name": plugin_id,
        "version": version,
        "description": "Dependency test plugin",
        "entry_point": f"plugins.{plugin_id}:Plugin",
        "capabilities": [],
    }
    if dependencies is not None:
        manifest["dependencies"] = dependencies
    return manifest


def _entry(artifact, manifest, key):
    return {
        "id": manifest["id"],
        "version": manifest["version"],
        "source": f"https://plugins.example.com/{manifest['id']}-{manifest['version']}.zip",
        "integrity": "sha256:" + hashlib.sha256(artifact).hexdigest(),
        "manifest": manifest,
        "signature": {
            "publisher": "acme",
            "algorithm": "ed25519",
            "value": base64.b64encode(key.sign(canonical_manifest_body(manifest))).decode(),
        },
    }


def _publisher_key():
    key = Ed25519PrivateKey.generate()
    raw = key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return key, base64.b64encode(raw).decode()


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


def _catalog(tmp_path, entries):
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"version": 1, "plugins": entries}), encoding="utf-8")
    from app.services.plugin_catalog import load_catalog_file

    return load_catalog_file(path)


def test_install_order_is_dependency_first(tmp_path):
    key, _ = _publisher_key()
    entries = [
        _entry(b"a", _manifest("plugin-a"), key),
        _entry(b"b", _manifest("plugin-b", dependencies=["plugin-a>=1.0"]), key),
        _entry(b"c", _manifest("plugin-c", dependencies=["plugin-b>=1.0"]), key),
    ]
    catalog = _catalog(tmp_path, entries)

    order = [entry.plugin_id for entry in resolve_install_order(catalog, catalog.get("plugin-c"))]

    assert order == ["plugin-a", "plugin-b", "plugin-c"]


def test_cycle_is_refused_before_any_write(tmp_path):
    key, _ = _publisher_key()
    entries = [
        _entry(b"a", _manifest("plugin-a", dependencies=["plugin-b"]), key),
        _entry(b"b", _manifest("plugin-b", dependencies=["plugin-a"]), key),
    ]
    catalog = _catalog(tmp_path, entries)

    with pytest.raises(PluginDependencyCycleError, match="cycle"):
        resolve_install_order(catalog, catalog.get("plugin-a"))


def test_missing_satisfying_version_is_refused(tmp_path):
    key, _ = _publisher_key()
    entries = [
        _entry(b"a", _manifest("plugin-a", "1.0.0"), key),
        _entry(b"b", _manifest("plugin-b", dependencies=["plugin-a>=2.0"]), key),
    ]
    catalog = _catalog(tmp_path, entries)

    with pytest.raises(PluginDependencyVersionError):
        resolve_install_order(catalog, catalog.get("plugin-b"))


def test_cyclic_install_leaves_no_partial_state(tmp_path, db):
    key, public = _publisher_key()
    entries = [
        _entry(b"a", _manifest("plugin-a", dependencies=["plugin-b"]), key),
        _entry(b"b", _manifest("plugin-b", dependencies=["plugin-a"]), key),
    ]
    config = _config(tmp_path, entries, public)
    user = User(username="lock", email="lock@example.com")
    user.set_password("supersecret123")
    db.session.add(user)
    db.session.commit()
    workspace = Workspace(user_id=user.id, name="WS")
    db.session.add(workspace)
    db.session.commit()
    transport = _transport(
        {
            entries[0]["source"]: b"a",
            entries[1]["source"]: b"b",
        }
    )

    with pytest.raises(Exception, match="cycle"):
        install_from_marketplace(
            "plugin-a",
            workspace_id=workspace.id,
            config=config,
            transport=transport,
        )

    assert Plugin.query.count() == 0
    assert PluginLockEntry.query.count() == 0


def test_lockfile_round_trip_via_api_and_cli(tmp_path, app, client, make_user, login):
    user = make_user()
    login()
    workspace = Workspace(user_id=user.id, name="WS")
    db.session.add(workspace)
    db.session.commit()
    db.session.add(
        PluginLockEntry(
            workspace_id=workspace.id,
            owner_id=user.id,
            plugin_id="plugin-a",
            version="1.0.0",
            source="https://plugins.example.com/a.zip",
            integrity="sha256:" + "b" * 64,
        )
    )
    db.session.commit()

    response = client.get(f"/plugins/api/marketplace/lock?workspace_id={workspace.id}")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["plugins"][0]["plugin_id"] == "plugin-a"
    assert payload["plugins"][0]["integrity"] == "sha256:" + "b" * 64

    runner = app.test_cli_runner()
    result = runner.invoke(args=["plugins", "lock", "--json"])
    assert result.exit_code == 0, result.output
    cli_payload = json.loads(result.output)
    assert cli_payload["plugins"][0]["version"] == "1.0.0"
