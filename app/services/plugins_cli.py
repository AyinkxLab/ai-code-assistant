"""Operator-facing plugin management Flask CLI commands (#171, #413, #397).

Usage (inside the Flask app context):

    flask plugins list [--json]
    flask plugins inspect <plugin_id> [--json]
    flask plugins enable <plugin_id>
    flask plugins disable <plugin_id>
    flask plugins install <local-path> [--json]
    flask plugins install <plugin-id> --remote [--version V] [--json]
    flask plugins install <plugin-id> --marketplace [--version V] [--workspace-id W] [--json]
    flask plugins scaffold <plugin-id> [--output-dir <path>] [--json]
    flask plugins lockfile <workspace-id> [--json]
    flask plugins lock [--workspace-id W] [--json]
    flask plugins update <plugin-id> <version> [--workspace-id W] [--json]
    flask plugins rollback <plugin-id> [--workspace-id W] [--json]
    flask plugins publishers [--json]
    flask plugins errors [--json]

The CLI acts as the **operator**, not as an end user: it reads/writes the
persisted ``Plugin`` rows and never performs per-user workspace authorization.
Local installation is a filesystem path with a validated ``manifest.json``;
URLs are refused. With ``--remote`` the positional argument is a plugin id from
the operator-controlled catalog and the active ``PLUGIN_TRUST_POLICY`` is
enforced before registration. Nothing here grants capabilities or loads plugin
code.

The ``scaffold`` command generates a valid plugin project from a template,
including manifest.json, entry point, and a minimal test file.

The ``lockfile`` command prints the resolved plugin lockfile for a workspace.

Exit codes: ``0`` success, ``1`` unknown plugin / missing argument, ``2``
manifest validation or other service error. ``--json`` emits stable,
parseable JSON on stdout.
"""

from __future__ import annotations

import json

import click
from flask import Flask, current_app

#: Exit codes (success / not found / validation-or-other error / refused).
EXIT_OK = 0
EXIT_NOT_FOUND = 1
EXIT_ERROR = 2
EXIT_REFUSED = 3


def _write_json(data) -> None:
    click.echo(json.dumps(data, indent=2, sort_keys=True))


def _fail(message: str, code: int = EXIT_ERROR, as_json: bool = False) -> None:
    """Print an error and exit with ``code`` (never returns)."""
    if as_json:
        click.echo(json.dumps({"error": message}, indent=2, sort_keys=True))
    else:
        click.echo(message, err=True)
    raise click.exceptions.Exit(code)


def register_plugins_cli(app: Flask) -> None:
    """Register the ``plugins`` CLI command group on ``app``."""

    @app.cli.group("plugins")
    def plugins_group():
        """Manage plugins (operator context; local installs only)."""

    @plugins_group.command("list")
    @click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
    def list_plugins(as_json: bool) -> int:
        """List registered plugins with id, version, and enabled state."""
        from app.services.plugin_ops import list_plugins

        rows = list_plugins()
        if as_json:
            _write_json(
                [
                    {
                        "id": p.id,
                        "name": p.name,
                        "version": p.version,
                        "enabled": p.enabled,
                        "trust_state": p.trust_state,
                        "trust_publisher": p.trust_publisher,
                        "entry_point": p.entry_point,
                        "declared_capabilities": p.capabilities or [],
                    }
                    for p in rows
                ]
            )
            return EXIT_OK
        for p in rows:
            state = "enabled" if p.enabled else "disabled"
            click.echo(f"{p.id:<24} {p.version:<12} {state:<9} {p.name}")
        click.echo(f"{len(rows)} plugin(s) registered.")
        return EXIT_OK

    @plugins_group.command("inspect")
    @click.argument("plugin_id")
    @click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
    def inspect_plugin(plugin_id: str, as_json: bool) -> int:
        """Show a plugin's full metadata and state."""
        from app.services.plugin_ops import PluginNotFoundError, get_plugin

        try:
            plugin = get_plugin(plugin_id)
        except PluginNotFoundError as exc:
            _fail(str(exc), code=EXIT_NOT_FOUND, as_json=as_json)

        if as_json:
            _write_json(plugin.to_dict())
            return EXIT_OK
        click.echo(f"id: {plugin.id}")
        click.echo(f"name: {plugin.name}")
        click.echo(f"version: {plugin.version}")
        click.echo(f"description: {plugin.description or ''}")
        click.echo(f"author: {plugin.author or ''}")
        click.echo(f"entry_point: {plugin.entry_point}")
        click.echo(f"compatibility: {plugin.compatibility or 'any'}")
        click.echo(f"trust_state: {plugin.trust_state}")
        click.echo(f"trust_publisher: {plugin.trust_publisher or ''}")
        click.echo(f"enabled: {plugin.enabled}")
        click.echo(f"declared_capabilities: {', '.join(plugin.capabilities or [])}")
        click.echo(f"permissions: {', '.join(plugin.permissions or [])}")
        click.echo(f"dependencies: {', '.join(plugin.dependencies or [])}")
        click.echo(f"configuration: {json.dumps(plugin.configuration or {})}")
        return EXIT_OK

    @plugins_group.command("enable")
    @click.argument("plugin_id")
    @click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
    def enable_plugin(plugin_id: str, as_json: bool) -> int:
        """Enable a plugin (operator scope)."""
        from app.services.plugin_ops import (
            PluginDependencyResolutionError,
            PluginNotFoundError,
            set_plugin_enabled,
        )

        try:
            plugin = set_plugin_enabled(plugin_id, True)
        except PluginNotFoundError as exc:
            _fail(str(exc), code=EXIT_NOT_FOUND, as_json=as_json)
        except PluginDependencyResolutionError as exc:
            _fail(str(exc), code=EXIT_ERROR, as_json=as_json)
        if as_json:
            _write_json({"id": plugin.id, "enabled": plugin.enabled})
        else:
            click.echo(f"Enabled plugin: {plugin.id}")
        return EXIT_OK

    @plugins_group.command("disable")
    @click.argument("plugin_id")
    @click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
    def disable_plugin(plugin_id: str, as_json: bool) -> int:
        """Disable a plugin (operator scope)."""
        from app.services.plugin_ops import PluginNotFoundError, set_plugin_enabled

        try:
            plugin = set_plugin_enabled(plugin_id, False)
        except PluginNotFoundError as exc:
            _fail(str(exc), code=EXIT_NOT_FOUND, as_json=as_json)
        if as_json:
            _write_json({"id": plugin.id, "enabled": plugin.enabled})
        else:
            click.echo(f"Disabled plugin: {plugin.id}")
        return EXIT_OK

    @plugins_group.command("install")
    @click.argument("path")
    @click.option(
        "--remote",
        is_flag=True,
        help="Treat PATH as a plugin id from the operator catalog instead of a local path.",
    )
    @click.option(
        "--marketplace",
        is_flag=True,
        help="Fetch and verify the signed artifact for PATH from the catalog.",
    )
    @click.option("--version", default=None, help="Catalog version to install (implies --remote).")
    @click.option(
        "--workspace-id",
        "workspace_id",
        type=int,
        default=None,
        help="Workspace to audit and record the marketplace lock entry for.",
    )
    @click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
    def install_plugin(
        path: str,
        remote: bool,
        marketplace: bool,
        version: str | None,
        workspace_id: int | None,
        as_json: bool,
    ) -> int:
        """Install a plugin from a local manifest or the marketplace catalog.

        Local paths remain the default. Use ``--remote`` (or ``--version``) to
        resolve the positional argument as a catalog plugin id, or
        ``--marketplace`` to additionally fetch and hash-verify the signed
        artifact before registration. The active ``PLUGIN_TRUST_POLICY`` is
        enforced before registration.
        """
        from app.services.plugin_ops import (
            MarketplaceInstallRefused,
            RemoteInstallError,
            install_from_catalog,
            install_from_local_dir,
            install_from_marketplace,
        )
        from app.services.plugins import ManifestValidationError, PluginError

        if marketplace or remote or version is not None:
            from app.services.plugin_catalog import (
                CatalogEntryNotFoundError,
                CatalogError,
                CatalogUnavailableError,
            )
            from app.services.plugin_compat import (
                IncompatibleCompatibilityError,
                InvalidCompatibilityError,
            )
            from app.services.plugin_signatures import PublisherSignatureError
            from app.services.plugin_trust import PluginTrustConfigError

            plugin_id = (path or "").strip()
            if not plugin_id:
                _fail(
                    "A plugin id is required for a remote install.",
                    code=EXIT_ERROR,
                    as_json=as_json,
                )
            try:
                if marketplace:
                    plugin, entry = install_from_marketplace(
                        plugin_id,
                        version,
                        workspace_id=workspace_id,
                    )
                else:
                    plugin, entry = install_from_catalog(plugin_id, version)
            except CatalogEntryNotFoundError as exc:
                _fail(str(exc), code=EXIT_NOT_FOUND, as_json=as_json)
            except MarketplaceInstallRefused as exc:
                _fail(str(exc), code=EXIT_REFUSED, as_json=as_json)
            except CatalogUnavailableError as exc:
                _fail(f"Plugin catalog is not available: {exc}", code=EXIT_ERROR, as_json=as_json)
            except CatalogError as exc:
                _fail(f"Invalid plugin catalog: {exc}", code=EXIT_ERROR, as_json=as_json)
            except (PublisherSignatureError, PluginTrustConfigError) as exc:
                _fail(
                    f"Trust policy refused the plugin: {exc}",
                    code=EXIT_REFUSED,
                    as_json=as_json,
                )
            except (
                IncompatibleCompatibilityError,
                InvalidCompatibilityError,
                ManifestValidationError,
                RemoteInstallError,
                PluginError,
            ) as exc:
                _fail(f"Invalid plugin: {exc}", code=EXIT_ERROR, as_json=as_json)

            if as_json:
                payload = plugin.to_dict()
                payload["source"] = entry.source
                payload["integrity"] = entry.integrity
                _write_json(payload)
            else:
                click.echo(f"Installed plugin from catalog: {plugin.id} v{plugin.version}")
            return EXIT_OK

        try:
            plugin = install_from_local_dir(path)
        except (ManifestValidationError, PluginError) as exc:
            _fail(f"Invalid plugin manifest: {exc}", code=EXIT_ERROR, as_json=as_json)
        if as_json:
            _write_json(plugin.to_dict())
        else:
            click.echo(f"Installed plugin: {plugin.id} v{plugin.version}")
        return EXIT_OK

    @plugins_group.command("update")
    @click.argument("plugin_id")
    @click.argument("new_version")
    @click.option("--workspace-id", "workspace_id", type=int, default=None)
    @click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
    def update(plugin_id: str, new_version: str, workspace_id: int | None, as_json: bool) -> int:
        """Update a plugin to a catalog version after signature/hash checks."""
        from app.services.plugin_catalog import CatalogEntryNotFoundError, CatalogError
        from app.services.plugin_compat import (
            IncompatibleCompatibilityError,
            InvalidCompatibilityError,
        )
        from app.services.plugin_ops import (
            AtomicUpdateError,
            PluginNotFoundError,
            PluginUpdateError,
            update_plugin,
        )
        from app.services.plugin_signatures import PublisherSignatureError
        from app.services.plugin_trust import PluginTrustConfigError

        try:
            plugin = update_plugin(
                plugin_id,
                new_version,
                workspace_id=workspace_id,
            )
        except PluginNotFoundError as exc:
            _fail(str(exc), code=EXIT_NOT_FOUND, as_json=as_json)
        except CatalogEntryNotFoundError as exc:
            _fail(str(exc), code=EXIT_NOT_FOUND, as_json=as_json)
        except (PublisherSignatureError, PluginTrustConfigError) as exc:
            _fail(str(exc), code=EXIT_REFUSED, as_json=as_json)
        except (IncompatibleCompatibilityError, InvalidCompatibilityError) as exc:
            _fail(f"Incompatible update: {exc}", code=EXIT_ERROR, as_json=as_json)
        except (AtomicUpdateError, PluginUpdateError, CatalogError) as exc:
            _fail(str(exc), code=EXIT_ERROR, as_json=as_json)
        if as_json:
            _write_json({"id": plugin.id, "version": plugin.version})
        else:
            click.echo(f"Updated plugin: {plugin.id} v{plugin.version}")
        return EXIT_OK

    @plugins_group.command("rollback")
    @click.argument("plugin_id")
    @click.option("--workspace-id", "workspace_id", type=int, default=None)
    @click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
    def rollback(plugin_id: str, workspace_id: int | None, as_json: bool) -> int:
        """Roll a plugin back to its retained previous version."""
        from app.services.plugin_catalog import CatalogEntryNotFoundError, CatalogError
        from app.services.plugin_ops import (
            AtomicUpdateError,
            PluginNotFoundError,
            PluginUpdateError,
            rollback_plugin,
        )

        try:
            plugin, entry = rollback_plugin(plugin_id, workspace_id=workspace_id)
        except PluginNotFoundError as exc:
            _fail(str(exc), code=EXIT_NOT_FOUND, as_json=as_json)
        except CatalogEntryNotFoundError as exc:
            _fail(str(exc), code=EXIT_NOT_FOUND, as_json=as_json)
        except (AtomicUpdateError, PluginUpdateError) as exc:
            _fail(str(exc), code=EXIT_ERROR, as_json=as_json)
        except CatalogError as exc:
            _fail(str(exc), code=EXIT_ERROR, as_json=as_json)
        if as_json:
            payload = {"id": plugin.id, "version": plugin.version}
            if entry is not None:
                payload["source"] = entry.source
                payload["integrity"] = entry.integrity
            _write_json(payload)
        else:
            click.echo(f"Rolled back plugin: {plugin.id} v{plugin.version}")
        return EXIT_OK

    @plugins_group.command("scaffold")
    @click.argument("plugin_id")
    @click.option(
        "--output-dir",
        "output_dir",
        default=None,
        help="Directory to write scaffold (default: cwd/<plugin-id>).",
    )
    @click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
    def scaffold_plugin(plugin_id: str, output_dir: str | None, as_json: bool) -> int:
        """Generate a valid plugin scaffold from a template.

        Creates a plugin directory with manifest.json, entry point (plugin.py),
        and a minimal test file. Validates the generated manifest before writing.
        """
        import re
        from pathlib import Path

        # Validate plugin_id format (kebab-case or snake_case)
        if not re.match(r"^[a-z][a-z0-9_-]*$", plugin_id):
            _fail(
                "Plugin ID must be lowercase alphanumeric with hyphens "
                "or underscores, starting with a letter.",
                code=EXIT_ERROR,
                as_json=as_json,
            )

        # Determine output directory
        target_dir = Path.cwd() / plugin_id if output_dir is None else Path(output_dir) / plugin_id

        if target_dir.exists():
            _fail(
                f"Directory already exists: {target_dir}",
                code=EXIT_ERROR,
                as_json=as_json,
            )

        # Create scaffold files
        manifest = _generate_manifest(plugin_id)
        plugin_py = _generate_plugin_py(plugin_id)
        test_py = _generate_test_py(plugin_id)

        try:
            target_dir.mkdir(parents=True, exist_ok=False)
            (target_dir / "manifest.json").write_text(manifest, encoding="utf-8")
            (target_dir / "plugin.py").write_text(plugin_py, encoding="utf-8")
            (target_dir / "test_plugin.py").write_text(test_py, encoding="utf-8")
        except OSError as exc:
            _fail(f"Failed to write scaffold: {exc}", code=EXIT_ERROR, as_json=as_json)

        # Validate the generated manifest
        from app.services.plugins import ManifestValidationError, PluginError, PluginManifest

        try:
            PluginManifest.from_file(target_dir / "manifest.json")
        except (ManifestValidationError, PluginError) as exc:
            # Clean up on validation failure
            import shutil

            shutil.rmtree(target_dir, ignore_errors=True)
            _fail(f"Generated manifest validation failed: {exc}", code=EXIT_ERROR, as_json=as_json)

        if as_json:
            _write_json(
                {
                    "plugin_id": plugin_id,
                    "directory": str(target_dir),
                    "files": ["manifest.json", "plugin.py", "test_plugin.py"],
                }
            )
        else:
            click.echo(f"Scaffold created at: {target_dir}")
            click.echo("Files: manifest.json, plugin.py, test_plugin.py")
        return EXIT_OK

    @plugins_group.command("publishers")
    @click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
    def list_publishers(as_json: bool) -> int:
        """List configured trusted publisher ids (never key material)."""
        from app.services.plugin_signatures import VERIFY_IF_PRESENT
        from app.services.plugin_trust import trusted_publisher_ids

        publishers = trusted_publisher_ids(current_app.config.get("PLUGIN_TRUSTED_KEYS") or {})
        policy = current_app.config.get("PLUGIN_TRUST_POLICY", VERIFY_IF_PRESENT)
        if as_json:
            _write_json(
                {
                    "publishers": publishers,
                    "count": len(publishers),
                    "policy": policy,
                }
            )
            return EXIT_OK
        if not publishers:
            click.echo("No trusted publishers configured.")
            return EXIT_OK
        for publisher in publishers:
            click.echo(publisher)
        click.echo(f"{len(publishers)} trusted publisher(s) configured (policy: {policy}).")
        return EXIT_OK

    @plugins_group.command("errors")
    @click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
    def plugin_errors(as_json: bool) -> int:
        """List the most recent structured plugin error reports (operator scope)."""
        from app.services.plugin_errors import list_operator_error_reports

        reports = list_operator_error_reports()
        if as_json:
            _write_json({"count": len(reports), "errors": reports})
            return EXIT_OK
        if not reports:
            click.echo("No plugin error reports recorded.")
            return EXIT_OK
        for report in reports:
            details = report.get("message") or report.get("exception_type") or ""
            click.echo(
                f"[{report['created_at']}] {report['plugin_id']} "
                f"{report['operation']} {report['exception_type'] or ''} {details}".rstrip()
            )
        click.echo(f"{len(reports)} plugin error report(s).")
        return EXIT_OK

    @plugins_group.command("lockfile")
    @click.argument("workspace_id", type=int)
    @click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
    def lockfile(workspace_id: int, as_json: bool) -> int:
        """Print the resolved plugin lockfile for a workspace.

        Reuses the same service as the API to avoid drift.
        """
        from app.models import PluginLockEntry
        from app.services.plugin_lockfile import build_lockfile

        lock_entries = PluginLockEntry.query.filter_by(workspace_id=workspace_id).all()
        if not lock_entries:
            if as_json:
                _write_json({"lockfile_version": 1, "plugins": []})
            else:
                click.echo("No lockfile entries found for this workspace.")
            return EXIT_OK

        lockfile = build_lockfile(lock_entries)
        if as_json:
            _write_json(lockfile)
        else:
            for plugin in lockfile["plugins"]:
                line = (
                    f"{plugin['plugin_id']} {plugin['version']} "
                    f"{plugin['source']} {plugin['integrity']}"
                )
                click.echo(line)
        return EXIT_OK

    @plugins_group.command("lock")
    @click.option(
        "--workspace-id",
        "workspace_id",
        type=int,
        default=None,
        help="Restrict the lockfile to one workspace (operator default: all).",
    )
    @click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
    def lock(workspace_id: int | None, as_json: bool) -> int:
        """Print the resolved plugin lockfile, optionally scoped to a workspace."""
        from app.models import PluginLockEntry
        from app.services.plugin_lockfile import build_lockfile

        query = PluginLockEntry.query
        if workspace_id is not None:
            query = query.filter_by(workspace_id=workspace_id)
        rows = query.order_by(PluginLockEntry.resolved_at.desc()).all()

        # The operator view spans workspaces, where the same plugin can appear
        # once per workspace. Keep the most recent resolution per plugin id so
        # the canonical lockfile stays unambiguous.
        latest: dict[str, object] = {}
        for row in rows:
            latest.setdefault(row.plugin_id, row)
        lockfile = build_lockfile(latest.values())
        if as_json:
            _write_json(lockfile)
        else:
            if not lockfile["plugins"]:
                click.echo("No lockfile entries found.")
            for plugin in lockfile["plugins"]:
                click.echo(
                    f"{plugin['plugin_id']} {plugin['version']} "
                    f"{plugin['source']} {plugin['integrity']}"
                )
        return EXIT_OK


def _generate_manifest(plugin_id: str) -> str:
    """Generate a valid manifest.json for the plugin scaffold."""
    import json

    module_name = plugin_id.replace("-", "_")
    class_name = module_name.replace("_", " ").title().replace(" ", "") + "Plugin"

    manifest = {
        "id": plugin_id,
        "name": plugin_id.replace("-", " ").replace("_", " ").title(),
        "version": "0.1.0",
        "description": f"Example plugin generated from scaffold: {plugin_id}",
        "author": "AI Code Assistant contributors",
        "entry_point": f"{module_name}.plugin:{class_name}",
        "capabilities": ["PROJECT_READ"],
        "permissions": ["read:project:files"],
        "configuration": {"example_setting": "default_value"},
    }
    return json.dumps(manifest, indent=2, sort_keys=True)


def _generate_plugin_py(plugin_id: str) -> str:
    """Generate the plugin.py entry point for the scaffold."""
    module_name = plugin_id.replace("-", "_")
    class_name = module_name.replace("_", " ").title().replace(" ", "") + "Plugin"

    return f'''"""Example plugin generated from scaffold: {plugin_id}.

See the plugin developer guide for the full walkthrough. Importing this
module has no side effects: nothing is subscribed until ``register()``
(called from ``on_enable``) runs.
"""

from __future__ import annotations

from app.services.events import Event, get_dispatcher

PLUGIN_ID = "{plugin_id}"
EVENT_TYPE = "project.created"


class {class_name}:
    """Record ``project.created`` events and expose the latest one."""

    def __init__(self, app=None, manifest=None):
        self.app = app
        self.manifest = manifest
        self.seen_project_ids: list[int] = []

    def register(self) -> None:
        """Subscribe to ``project.created`` (idempotent)."""
        dispatcher = get_dispatcher()
        dispatcher.unsubscribe(EVENT_TYPE, self.on_event)
        dispatcher.subscribe(EVENT_TYPE, self.on_event, plugin_id=PLUGIN_ID)

    def on_event(self, event):
        """Handle an event. Handlers must not raise; return safe data instead."""
        project_id = (event.data or {{}}).get("project_id")
        self.seen_project_ids.append(project_id)
        return {{"plugin": PLUGIN_ID, "event": event.event_type, "project_id": project_id}}

    def on_enable(self) -> None:
        """Lifecycle hook: subscribe once the plugin is enabled."""
        self.register()

    def on_disable(self) -> None:
        """Lifecycle hook: stop receiving events while disabled."""
        get_dispatcher().unsubscribe(EVENT_TYPE, self.on_event)


def main() -> None:
    """Load the example from its manifest and exercise the registry flow."""
    from app.services.plugins import Plugin, PluginManifest, PluginRegistry

    manifest = PluginManifest.from_file(Path(__file__).with_name("manifest.json"))
    registry = PluginRegistry()
    plugin = Plugin(manifest)
    plugin.load()
    registry.register(plugin)

    instance: {class_name} = plugin.get_instance()
    instance.register()
    try:
        # A host application would instead dispatch real ``project.created``
        # events; here we dispatch one directly so the example is self-contained.
        event = Event(
            event_type="project.created",
            data={{"project_id": 42}},
            workspace_id=1,
            user_id=1,
        )
        instance.on_event(event)
    finally:
        get_dispatcher().unsubscribe(EVENT_TYPE, instance.on_event)

    print(f"{{manifest.id}} registered; recorded {{len(instance.seen_project_ids)}} event(s).")


if __name__ == "__main__":
    main()
'''


def _generate_test_py(plugin_id: str) -> str:
    """Generate a minimal test file for the plugin scaffold."""
    class_name = plugin_id.replace("-", "_").replace("_", " ").title().replace(" ", "") + "Plugin"
    module_name = plugin_id.replace("-", "_")

    return f'''"""Tests for the {plugin_id} plugin scaffold."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app.services.events import Event, get_dispatcher
from app.services.plugins import Plugin, PluginManifest, PluginRegistry

# Make the scaffold directory importable
SCAFFOLD_DIR = Path(__file__).parent
sys.path.insert(0, str(SCAFFOLD_DIR))

try:
    from {module_name}.plugin import {class_name}, main
finally:
    sys.path.remove(str(SCAFFOLD_DIR))


def test_manifest_validates_against_schema():
    """The generated manifest must satisfy the published JSON schema."""
    manifest = PluginManifest.from_file(SCAFFOLD_DIR / "manifest.json")

    assert manifest.id == "{plugin_id}"
    assert manifest.entry_point == "{module_name}.plugin:{class_name}"
    assert manifest.capabilities == ["PROJECT_READ"]


def test_plugin_loads_and_registers():
    """The plugin must load and register through the real plugin services."""
    manifest = PluginManifest.from_file(SCAFFOLD_DIR / "manifest.json")
    registry = PluginRegistry()
    plugin = Plugin(manifest)

    plugin.load()
    registry.register(plugin)

    assert registry.get("{plugin_id}") is plugin
    assert plugin.module is not None
    assert getattr(plugin.module, "{class_name}", None) is not None


def test_plugin_subscribes_and_records_events():
    """The plugin must subscribe to events and record them."""
    manifest = PluginManifest.from_file(SCAFFOLD_DIR / "manifest.json")
    plugin = Plugin(manifest)
    plugin.load()
    instance = plugin.get_instance()

    instance.register()
    try:
        subscribers = get_dispatcher().list_subscribers("project.created")
        assert "{plugin_id}" in subscribers["project.created"]

        event = Event(event_type="project.created", data={{"project_id": 42}})
        instance.on_event(event)
    finally:
        get_dispatcher().unsubscribe("project.created", instance.on_event)

    assert [e.data["project_id"] for e in instance.seen_project_ids] == [42]


def test_main_runs_end_to_end(capsys):
    """The plugin's main function exercises the registry flow."""
    main()

    output = capsys.readouterr().out
    assert "{plugin_id} registered; recorded 1 event(s)." in output
'''
