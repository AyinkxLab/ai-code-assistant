"""Read-only Stellar/Soroban developer inspection.

Builds on :class:`app.services.stellar.StellarService` (Horizon) and
:class:`app.services.soroban_rpc.SorobanRpcClient` (Stellar RPC) to give a
developer a structured, safe view of:

- the configured network and its live status (RPC health / latest ledger),
- an account's on-chain state,
- a contract's instance entry and deployed wasm code metadata,
- an arbitrary ledger entry by base64 ``LedgerKey``.

Everything is read-only and bounded. Contract-data and contract-code ledger
entries are decoded into structured, human-readable values by
:mod:`app.services.stellar_xdr_decode`; unsupported or malformed XDR is reported
explicitly and never guessed at. Live data is always labelled with the network
it was read from and an honest availability flag — callers must never treat an
unavailable RPC as authoritative data.
"""

from __future__ import annotations

from typing import Any

from app.services.soroban_rpc import (
    MAX_XDR_CHARS,
    SorobanRpcClient,
    get_soroban_rpc_client,
)
from app.services.stellar import (
    AccountError,
    StellarError,
    StellarService,
    get_stellar_service,
    validate_stellar_address,
)
from app.services.stellar_xdr import (
    StrkeyError,
    account_address_to_bytes,
    contract_address_to_bytes,
    ledger_key_contract_code,
    ledger_key_for_contract,
    validate_contract_address,
    validate_ledger_key_base64,
    wasm_hash_from_hex,
)
from app.services.stellar_xdr_decode import decode_ledger_entry_data


def _clip(value: str | None, limit: int = MAX_XDR_CHARS) -> str | None:
    if not isinstance(value, str) or len(value) <= limit:
        return value
    return value[:limit] + "…[truncated]"


def _decode_entry_xdr(raw_xdr: str | None) -> dict[str, Any]:
    """Decode an RPC entry's ``xdr`` (a ``LedgerEntryData``) without raising."""
    return decode_ledger_entry_data(raw_xdr or "")


# ---------------------------------------------------------------------------
# Network status
# ---------------------------------------------------------------------------


def network_status(rpc: SorobanRpcClient | None = None) -> dict[str, Any]:
    """Return the configured network plus a best-effort live RPC status.

    RPC data is included only when the node answers; otherwise an explicit
    ``rpc_available: false`` with the sanitized reason is returned. Never
    fabricates ledger/health data.
    """
    service = get_stellar_service()
    result: dict[str, Any] = {"network": service.config.to_dict(), "rpc_available": False}
    try:
        rpc = rpc or get_soroban_rpc_client()
        result["rpc_available"] = True
        result["rpc_endpoint"] = rpc.config.rpc_url
        result["health"] = rpc.get_health()
        result["latest_ledger"] = rpc.get_latest_ledger()
        result["rpc_network"] = rpc.get_network()
    except StellarError as exc:
        result["rpc_error"] = str(exc)
        result["rpc_available"] = False
    return result


# ---------------------------------------------------------------------------
# Account inspection
# ---------------------------------------------------------------------------


def inspect_account(
    address: str,
    *,
    service: StellarService | None = None,
    rpc: SorobanRpcClient | None = None,
) -> dict[str, Any]:
    """Inspect a Stellar account (read-only).

    Combines the parsed Horizon account data with RPC ledger freshness and the
    network the data was read from. Raises :class:`AccountError` for invalid
    addresses (structural or checksum) and missing accounts.
    """
    address = (address or "").strip()
    if not validate_stellar_address(address):
        raise AccountError(f"Invalid Stellar address: {address}")
    try:
        account_address_to_bytes(address)
    except StrkeyError as exc:
        raise AccountError(f"Invalid Stellar address checksum: {exc}") from exc

    service = service or get_stellar_service()
    account = service.get_account(address)

    ledger: dict[str, Any] = {}
    ledger_available = False
    if rpc is not None:
        try:
            ledger = rpc.get_latest_ledger()
            ledger_available = True
        except StellarError:
            ledger_available = False

    return {
        "address": address,
        "network": service.config.to_dict(),
        "account": account,
        "ledger_freshness": {
            "available": ledger_available,
            "sequence": ledger.get("sequence") if ledger_available else None,
            "close_time": ledger.get("closeTime") if ledger_available else None,
        },
        "note": (
            "Account state is read-only network data. Soroban contract state "
            "for this account lives under its contracts and is inspectable via "
            "contract inspection."
        ),
    }


# ---------------------------------------------------------------------------
# Contract inspection
# ---------------------------------------------------------------------------


def inspect_contract(
    contract_id: str,
    *,
    rpc: SorobanRpcClient | None = None,
    wasm_hash: str | None = None,
) -> dict[str, Any]:
    """Inspect a Soroban contract (read-only, via Stellar RPC).

    Retrieves the contract's instance ledger entry (its code-hash reference and
    instance storage) and, when a ``wasm_hash`` is supplied, the deployed wasm
    code's ledger entry. Where the RPC returns decodable contract-data/code
    XDR, a structured ``decoded`` view is included alongside the raw (bounded,
    explicitly not re-encoded) XDR; unsupported or malformed XDR is reported
    explicitly and never guessed at.

    Raises:
        AccountError: For an invalid contract id or wasm hash.
        SorobanRpcError: When the RPC is unavailable or rejects the request.
    """
    contract_id = (contract_id or "").strip()
    if not validate_contract_address(contract_id):
        raise AccountError(f"Invalid Soroban contract id: {contract_id}")
    try:
        contract_address_to_bytes(contract_id)
    except StrkeyError as exc:
        raise AccountError(f"Invalid contract id checksum: {exc}") from exc

    rpc = rpc or get_soroban_rpc_client()
    instance_key = ledger_key_for_contract(contract_id)

    result: dict[str, Any] = {
        "contract_id": contract_id,
        "network": rpc.config.to_dict(),
        "ledger_key": instance_key,
        "instance_entry": None,
        "code_entry": None,
        "found": False,
        "decoded": False,
        "note": (
            "Contract inspection is read-only network data. Supported "
            "contract-data/code entries are decoded into a structured view; "
            "unsupported or malformed XDR is reported explicitly and never "
            "guessed at."
        ),
    }

    instance_raw = rpc.get_ledger_entries([instance_key], clip_xdr=False)
    result["latest_ledger"] = instance_raw.get("latestLedger")
    raw_entries = instance_raw.get("entries") or []
    if raw_entries:
        raw_instance = raw_entries[0]
        instance_decoded = _decode_entry_xdr(raw_instance.get("xdr"))
        result["instance_entry"] = {
            "lastModifiedLedgerSeq": raw_instance.get("lastModifiedLedgerSeq"),
            "liveUntilLedgerSeq": raw_instance.get("liveUntilLedgerSeq"),
            "xdr": _clip(raw_instance.get("xdr")),
            "decoded": instance_decoded,
        }
        result["found"] = True
        result["decoded"] = bool(instance_decoded.get("decoded"))
    else:
        result["found"] = False

    if wasm_hash:
        result["wasm_hash"] = wasm_hash.strip().lower()
        try:
            code_bytes = wasm_hash_from_hex(wasm_hash)
        except StrkeyError as exc:
            raise AccountError(f"Invalid wasm hash: {exc}") from exc
        code_key = ledger_key_contract_code(code_bytes)
        code_raw = rpc.get_ledger_entries([code_key], clip_xdr=False)
        code_entries = code_raw.get("entries") or []
        if code_entries:
            raw_code = code_entries[0]
            code_decoded = _decode_entry_xdr(raw_code.get("xdr"))
            result["code_entry"] = {
                "lastModifiedLedgerSeq": raw_code.get("lastModifiedLedgerSeq"),
                "liveUntilLedgerSeq": raw_code.get("liveUntilLedgerSeq"),
                "xdr": _clip(raw_code.get("xdr")),
                "decoded": code_decoded,
            }
            result["code_found"] = True
            if code_decoded.get("decoded"):
                detail = code_decoded.get("detail") or {}
                result["code_byte_size"] = detail.get("code_byte_size")
        else:
            result["code_found"] = False

    return result


def inspect_ledger_entry(
    ledger_key: str,
    *,
    rpc: SorobanRpcClient | None = None,
) -> dict[str, Any]:
    """Look up a live ledger entry by a caller-supplied base64 ``LedgerKey``.

    The key is validated structurally (base64 + sane length) before the request
    and the response is bounded. Supported contract-data/contract-code entries
    are decoded into a structured view; all other entry types and malformed XDR
    are reported explicitly and never guessed at.
    """
    ledger_key = (ledger_key or "").strip()
    ok, reason = validate_ledger_key_base64(ledger_key)
    if not ok:
        raise AccountError(f"Invalid ledger key: {reason}")

    rpc = rpc or get_soroban_rpc_client()
    response = rpc.get_ledger_entries([ledger_key], clip_xdr=False)
    raw = (response.get("entries") or [])[:1]
    entry: dict[str, Any] | None = None
    decoded = False
    if raw:
        raw_entry = raw[0]
        decoded_result = _decode_entry_xdr(raw_entry.get("xdr"))
        decoded = bool(decoded_result.get("decoded"))
        entry = {
            "lastModifiedLedgerSeq": raw_entry.get("lastModifiedLedgerSeq"),
            "liveUntilLedgerSeq": raw_entry.get("liveUntilLedgerSeq"),
            "xdr": _clip(raw_entry.get("xdr")),
            "decoded": decoded_result,
        }
    return {
        "network": rpc.config.to_dict(),
        "latest_ledger": response.get("latestLedger"),
        "entry": entry,
        "found": bool(raw),
        "decoded": decoded,
    }
