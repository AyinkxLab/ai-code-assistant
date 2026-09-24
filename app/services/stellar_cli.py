"""Read-only Stellar/Soroban Flask CLI commands.

Usage (inside an app context provided by Flask):

    flask stellar network [--json]
    flask stellar config [--network <net>] [--path <path>] [--overwrite] [--json]
    flask stellar validate <address> [--json]
    flask stellar account <address> [--json]
    flask stellar health [--json]
    flask stellar contract <contract_id> [--wasm-hash <hex>] [--json]
    flask stellar ledger-entry <base64-ledger-key> [--json]
    flask stellar ledger <sequence> [--json]
    flask stellar assets [--cursor <cursor>] [--limit <limit>] [--json]
    flask stellar operation <operation-id> [--json]

Everything is read-only and bounded by the same service/RPC client used by the
web application — commands never sign, simulate, or submit transactions.

``--json`` prints a stable, parseable JSON document. Exit codes:
``0`` success, ``2`` service error / not found / unreachable, ``3`` invalid
input. Without ``--json`` the human-readable output is unchanged and errors go
to stderr.
"""

from __future__ import annotations

import json

import click
from flask import Flask

#: Exit codes (success / service-or-not-found / validation failure).
EXIT_OK = 0
EXIT_SERVICE = 2
EXIT_VALIDATION = 3


def _emit_json(data) -> None:
    click.echo(json.dumps(data, indent=2, sort_keys=True))


def _fail(message: str, code: int, as_json: bool = False) -> None:
    """Print an error and exit (never returns)."""
    if as_json:
        _emit_json({"error": message, "exit_code": code})
    else:
        click.echo(message, err=True)
    raise click.exceptions.Exit(code)


def _exit_code_for(exc, as_json: bool) -> None:
    """Exit with the documented code for a service-layer exception."""
    from app.services.stellar import AccountError

    if isinstance(exc, AccountError):
        _fail(str(exc), EXIT_VALIDATION, as_json=as_json)
    _fail(str(exc), EXIT_SERVICE, as_json=as_json)


def _configure_client(ctx, param, value) -> str | None:
    """Allow the ``--network`` option to override the configured network.

    Only built-in networks (testnet/mainnet/futurenet) and ``custom`` are
    accepted; raw URLs are rejected here just as they are in configuration.
    """
    return value


def register_stellar_cli(app: Flask) -> None:
    """Register the ``stellar`` CLI command group on ``app``."""

    @app.cli.group("stellar")
    def stellar_group():
        """Read-only Stellar/Soroban developer commands.

        Exit codes: 0 success; 2 service error / not found / unreachable;
        3 invalid input. Add ``--json`` to any command for JSON output.
        """

    @stellar_group.command("network")
    @click.option("--network", default=None, callback=_configure_client)
    @click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
    def stellar_network(network: str | None, as_json: bool) -> None:
        """Print the resolved Stellar network configuration."""
        from app.services.stellar import StellarError, StellarService

        try:
            service = StellarService(network=network)
        except StellarError as exc:
            _fail(str(exc), EXIT_SERVICE, as_json=as_json)
        info = service.get_network_info()
        if as_json:
            _emit_json(info)
            return
        click.echo(f"network: {info['network']}")
        click.echo(f"passphrase: {info['network_passphrase']}")
        click.echo(f"mode: {info['mode']}")
        click.echo(f"public: {info['is_public']}")
        click.echo(f"horizon: {info['horizon_url']}")
        click.echo(f"rpc: {info['rpc_url'] or '(none)'}")
        click.echo(f"timeout_seconds: {info['timeout_seconds']}")

    @stellar_group.command("config")
    @click.option(
        "--network",
        default=None,
        callback=_configure_client,
        help="Network to generate config for (testnet/mainnet/futurenet/custom).",
    )
    @click.option(
        "--path",
        "target_path",
        default=".soroban/config.toml",
        show_default=True,
        help="Target path recorded in the generated row.",
    )
    @click.option(
        "--overwrite",
        is_flag=True,
        default=False,
        help="Allow replacing --path when it is listed as existing.",
    )
    @click.option(
        "--existing",
        "existing",
        multiple=True,
        help="Existing project path (repeatable) that must not be overwritten.",
    )
    @click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
    def stellar_config(
        network: str | None,
        target_path: str,
        overwrite: bool,
        existing: tuple[str, ...],
        as_json: bool,
    ) -> None:
        """Generate a read-only Soroban network config (no files are written)."""
        from app.services.stellar import StellarError
        from app.services.stellar_config import StellarConfigError, generate_config_file

        try:
            row = generate_config_file(
                network,
                existing_paths=list(existing),
                target_path=target_path,
                overwrite=overwrite,
            )
        except (StellarConfigError, StellarError) as exc:
            _fail(str(exc), EXIT_VALIDATION, as_json=as_json)
        if as_json:
            _emit_json({"path": row["path"], "content": row["content"]})
            return
        click.echo(f"# {row['path']}")
        click.echo(row["content"], nl=False)

    @stellar_group.command("validate")
    @click.argument("address")
    @click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
    def stellar_validate(address: str, as_json: bool) -> None:
        """Validate a Stellar address (G...)."""
        from app.services.stellar import StellarService

        service = StellarService()
        if service.validate_address(address):
            if as_json:
                _emit_json({"address": address, "valid": True, "reason": "ok"})
            else:
                click.echo(f"{address} is a structurally valid Stellar address.")
            return
        if as_json:
            _emit_json(
                {
                    "address": address,
                    "valid": False,
                    "reason": "Not a valid Stellar address.",
                    "exit_code": EXIT_VALIDATION,
                }
            )
            raise click.exceptions.Exit(EXIT_VALIDATION)
        _fail(f"{address} is not a valid Stellar address.", EXIT_VALIDATION)

    @stellar_group.command("account")
    @click.argument("address")
    @click.option("--network", default=None, callback=_configure_client)
    @click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
    def stellar_account(address: str, network: str | None, as_json: bool) -> None:
        """Fetch a bounded, read-only view of a Stellar account."""
        from app.services.stellar import AccountError, StellarError, StellarService

        try:
            service = StellarService(network=network)
            account = service.get_account(address)
        except AccountError as exc:
            _fail(str(exc), EXIT_VALIDATION, as_json=as_json)
        except StellarError as exc:
            _fail(str(exc), EXIT_SERVICE, as_json=as_json)
        if as_json:
            _emit_json(account)
            return
        click.echo(f"account_id: {account['account_id']}")
        click.echo(f"sequence: {account['sequence']}")
        click.echo(f"subentry_count: {account['subentry_count']}")
        for balance in account["balances"][:20]:
            click.echo(
                f"balance: {balance.get('balance')} "
                f"{balance.get('asset_code') or balance.get('asset_type')}"
                f"{':' + balance['asset_issuer'] if balance.get('asset_issuer') else ''}"
            )

    @stellar_group.command("health")
    @click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
    def stellar_health(as_json: bool) -> None:
        """Print live Stellar RPC health and the latest ledger (read-only)."""
        from app.services.soroban_rpc import SorobanRpcClient, SorobanRpcError

        try:
            client = SorobanRpcClient()
            health = client.get_health()
            latest = client.get_latest_ledger()
        except SorobanRpcError as exc:
            _fail(str(exc), EXIT_SERVICE, as_json=as_json)
        except Exception as exc:
            _fail(f"Health check failed: {exc}", EXIT_SERVICE, as_json=as_json)
        if as_json:
            _emit_json(
                {
                    "status": health.get("status"),
                    "latest_ledger": health.get("latestLedger"),
                    "oldest_ledger": health.get("oldestLedger"),
                    "retention_window": health.get("ledgerRetentionWindow"),
                    "protocol_version": latest.get("protocolVersion"),
                }
            )
            return
        click.echo(f"status: {health.get('status')}")
        click.echo(f"latest_ledger: {health.get('latestLedger')}")
        click.echo(
            f"retention_window: {health.get('ledgerRetentionWindow')} ledgers "
            f"({health.get('oldestLedger')} - {health.get('latestLedger')})"
        )
        click.echo(f"protocol_version: {latest.get('protocolVersion')}")

    @stellar_group.command("contract")
    @click.argument("contract_id")
    @click.option("--wasm-hash", default=None, help="Optional wasm code hash (hex).")
    @click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
    def stellar_contract(contract_id: str, wasm_hash: str | None, as_json: bool) -> None:
        """Inspect a Soroban contract (read-only, via Stellar RPC)."""
        from app.services.stellar_inspection import inspect_contract

        try:
            result = inspect_contract(contract_id, wasm_hash=wasm_hash)
        except Exception as exc:
            _exit_code_for(exc, as_json)
        if as_json:
            _emit_json(result)
            return
        click.echo(f"contract_id: {result['contract_id']}")
        click.echo(f"network: {result['network']['network']}")
        click.echo(f"found: {result.get('found')}")
        click.echo(f"latest_ledger: {result.get('latest_ledger')}")
        if result.get("instance_entry"):
            entry = result["instance_entry"]
            click.echo(f"instance_last_modified_ledger: {entry.get('lastModifiedLedgerSeq')}")
            click.echo("instance_xdr: (retrieved, not decoded)")
        if "wasm_hash" in result:
            click.echo(f"wasm_hash: {result['wasm_hash']}")
            click.echo(f"code_found: {result.get('code_found')}")

    @stellar_group.command("ledger-entry")
    @click.argument("ledger_key")
    @click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
    def stellar_ledger_entry(ledger_key: str, as_json: bool) -> None:
        """Look up a live ledger entry by base64 LedgerKey (read-only)."""
        from app.services.stellar_inspection import inspect_ledger_entry

        try:
            result = inspect_ledger_entry(ledger_key)
        except Exception as exc:
            _exit_code_for(exc, as_json)
        if as_json:
            _emit_json(result)
            return
        click.echo(f"network: {result['network']['network']}")
        click.echo(f"found: {result.get('found')}")
        click.echo(f"latest_ledger: {result.get('latest_ledger')}")
        if result.get("entry"):
            click.echo(f"last_modified_ledger: {result['entry'].get('lastModifiedLedgerSeq')}")
            click.echo("xdr: (retrieved, not decoded)")

    @stellar_group.command("ledger")
    @click.argument("sequence", type=int)
    @click.option("--network", default=None, callback=_configure_client)
    @click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
    def stellar_ledger(sequence: int, network: str | None, as_json: bool) -> None:
        """Fetch a bounded ledger by sequence number (read-only)."""
        from app.services.stellar import StellarError, StellarService

        try:
            result = StellarService(network=network).get_ledger(sequence)
        except StellarError as exc:
            _fail(str(exc), EXIT_SERVICE, as_json=as_json)
        if as_json:
            _emit_json(result)
            return
        click.echo(f"sequence: {result['sequence']}")
        click.echo(f"hash: {result['hash']}")
        click.echo(f"closed_at: {result['closed_at']}")
        click.echo(f"operation_count: {result['operation_count']}")

    @stellar_group.command("assets")
    @click.option("--cursor", default=None)
    @click.option("--limit", default=None, type=int)
    @click.option("--network", default=None, callback=_configure_client)
    @click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
    def stellar_assets(
        cursor: str | None, limit: int | None, network: str | None, as_json: bool
    ) -> None:
        """Fetch a bounded page of issued assets (read-only)."""
        from app.services.stellar import StellarError, StellarService

        try:
            result = StellarService(network=network).get_assets(cursor=cursor, limit=limit)
        except StellarError as exc:
            _fail(str(exc), EXIT_SERVICE, as_json=as_json)
        if as_json:
            _emit_json(result)
            return
        for asset in result["records"]:
            code = asset.get("asset_code") or asset.get("asset_type")
            click.echo(
                f"asset: {code} issuer={asset.get('asset_issuer')} amount={asset.get('amount')}"
            )
        if result.get("next"):
            click.echo(f"next: {result['next']}")

    @stellar_group.command("operation")
    @click.argument("operation_id")
    @click.option("--network", default=None, callback=_configure_client)
    @click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
    def stellar_operation(operation_id: str, network: str | None, as_json: bool) -> None:
        """Fetch bounded operation metadata (read-only)."""
        from app.services.stellar import StellarError, StellarService

        try:
            result = StellarService(network=network).get_operation(operation_id)
        except StellarError as exc:
            _fail(str(exc), EXIT_SERVICE, as_json=as_json)
        if as_json:
            _emit_json(result)
            return
        click.echo(f"id: {result['id']}")
        click.echo(f"type: {result['type']}")
        click.echo(f"source_account: {result['source_account']}")
        click.echo(f"transaction_hash: {result['transaction_hash']}")
        click.echo(f"ledger: {result['ledger']}")
