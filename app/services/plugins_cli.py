"""Operator-facing plugin management Flask CLI commands (#171).

Usage (inside the Flask app context):

    flask plugins list [--json]
    flask plugins inspect <plugin_id> [--json]
    flask plugins enable <plugin_id>
    flask plugins disable <plugin_id>
    flask plugins install <local-path> [--json]

The CLI acts as the **operator**, not as an end user: it reads/writes the
persisted ``Plugin`` rows and never performs per-user workspace authorization.
Installation is **local-only** — a filesystem path with a validated
``manifest.json``; URLs are refused. Nothing here grants capabilities or loads
plugin code.

Exit codes: ``0`` success, ``1`` unknown plugin / missing argument, ``2``
manifest validation or other service error. ``--json`` emits stable,
parseable JSON on stdout.
"""

from __future__ import annotations

import json

import click
from flask import Flask

#: Exit codes (success / not found / validation-or-other error).
EXIT_OK = 0
EXIT_NOT_FOUND = 1
EXIT_ERROR = 2


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
        from app.services.plugin_ops import PluginNotFoundError, set_plugin_enabled

        try:
            plugin = set_plugin_enabled(plugin_id, True)
        except PluginNotFoundError as exc:
            _fail(str(exc), code=EXIT_NOT_FOUND, as_json=as_json)
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
    @click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
    def install_plugin(path: str, as_json: bool) -> int:
        """Register a plugin from a local manifest directory (URLs refused)."""
        from app.services.plugin_ops import install_from_local_dir
        from app.services.plugins import ManifestValidationError, PluginError

        try:
            plugin = install_from_local_dir(path)
        except (ManifestValidationError, PluginError) as exc:
            _fail(f"Invalid plugin manifest: {exc}", code=EXIT_ERROR, as_json=as_json)
        if as_json:
            _write_json(plugin.to_dict())
        else:
            click.echo(f"Installed plugin: {plugin.id} v{plugin.version}")
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
