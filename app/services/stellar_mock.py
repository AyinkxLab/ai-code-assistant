"""Deterministic, offline mock Stellar network for tests and local development.

A small in-process HTTP server (stdlib ``http.server`` only) that speaks the two
APIs the application reads from:

* **Horizon-style JSON** (``GET /accounts/<id>``, ``/transactions/<hash>``,
  ``/ledgers/<seq>``, ``/accounts/<id>/transactions``, ``/assets``).
* **Stellar RPC (JSON-RPC 2.0)** on ``POST /rpc`` with the read-only method set
  the app implements (``getHealth``, ``getVersionInfo``, ``getLatestLedger``,
  ``getNetwork``, ``getLedgerEntries``, ``getLedgers``, ``getTransactions``,
  ``getEvents``, ``getFeeStats``).

Every response is deterministic and bound to the same loopback-only fixtures
(no external network, no signing, no submission, no simulation). It is meant to
exercise the *real* service/RPC code paths end to end during tests and to give
local development an offline node.

Usage in tests::

    from app.services.stellar_mock import MockStellarServer
    server = MockStellarServer()
    server.start()
    try:
        ... point STELLAR_HORIZON_URL / STELLAR_RPC_URL at
            server.horizon_url / server.rpc_url ...
    finally:
        server.stop()

See ``tests/conftest.py`` (``mock_stellar`` fixture) and ``docs/stellar.md``.
"""

from __future__ import annotations

import base64
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from app.services.stellar_xdr import (
    contract_address_to_bytes,
    ledger_key_contract_code,
    ledger_key_for_contract,
)

# ---------------------------------------------------------------------------
# Deterministic fixtures (all public/read-only values used across tests).
# ---------------------------------------------------------------------------

MOCK_ACCOUNT = "GALAXYVOIDAOPZTDLHILAJQKCVVFMD4IKLXLSZV5YHO7VY74IWZILUTO"
MOCK_CONTRACT = "CCPYZFKEAXHHS5VVW5J45TOU7S2EODJ7TZNJIA5LKDVL3PESCES6FNCI"
MOCK_WASM_HASH = bytes(range(32))
MOCK_WASM_HASH_HEX = MOCK_WASM_HASH.hex()
MOCK_CODE = bytes(range(64)) * 4  # 256 bytes of "wasm"
MOCK_LATEST_LEDGER = 490314
MOCK_PROTOCOL = 22
MOCK_NETWORK_PASSPHRASE = "Standalone Network ; February 2021"
MOCK_TX_HASH = "ab" * 32

# ---------------------------------------------------------------------------
# Ledger-entry XDR builders (wire layout verified by the decode fixtures).
# ---------------------------------------------------------------------------


def _b64(blob: bytes) -> str:
    return base64.b64encode(blob).decode("ascii")


def _p32(value: int) -> bytes:
    return (value & 0xFFFFFFFF).to_bytes(4, "big")


def _opaque(body: bytes) -> bytes:
    pad = (4 - (len(body) % 4)) % 4
    return _p32(len(body)) + body + b"\x00" * pad


def _instance_key() -> str:
    return ledger_key_for_contract(MOCK_CONTRACT)


def _code_key() -> str:
    return ledger_key_contract_code(MOCK_WASM_HASH)


def _instance_entry_xdr() -> str:
    """A CONTRACT_DATA instance entry whose executable wasm is ``MOCK_WASM_HASH``."""
    contract = contract_address_to_bytes(MOCK_CONTRACT)
    # LedgerEntryData: type(6), ExtensionPoint(0), SCAddress(contract),
    # SCVal key (LEDGER_KEY_CONTRACT_INSTANCE=20), durability PERSISTENT(1),
    # SCVal value (CONTRACT_INSTANCE=19: wasm executable + nil storage map).
    body = (
        _p32(6)
        + _p32(0)
        + _p32(1)
        + contract
        + _p32(20)
        + _p32(1)
        + _p32(19)
        + _p32(0)
        + MOCK_WASM_HASH
        + _p32(0)
    )
    return _b64(body)


def _code_entry_xdr() -> str:
    """A CONTRACT_CODE entry for ``MOCK_WASM_HASH`` with ``MOCK_CODE`` bytes."""
    body = _p32(7) + _p32(0) + MOCK_WASM_HASH + _opaque(MOCK_CODE)
    return _b64(body)


# ---------------------------------------------------------------------------
# Horizon fixture records
# ---------------------------------------------------------------------------

_HORIZON_ACCOUNT = {
    "account_id": MOCK_ACCOUNT,
    "sequence": "1234567890",
    "subentry_count": 1,
    "balances": [
        {"asset_type": "native", "asset_code": None, "asset_issuer": None, "balance": "50.0000000"}
    ],
}

_HORIZON_TRANSACTION = {
    "hash": MOCK_TX_HASH,
    "ledger": MOCK_LATEST_LEDGER,
    "created_at": "2026-08-01T12:00:00Z",
    "successful": True,
    "source_account": MOCK_ACCOUNT,
    "memo": None,
}

_HORIZON_LEDGER = {
    "sequence": MOCK_LATEST_LEDGER,
    "hash": "c0ffee",
    "ledger_hash": "c0ffee",
    "prev_hash": "b0ba",
    "closed_at": "2026-08-01T12:00:00Z",
    "protocol_version": MOCK_PROTOCOL,
    "base_fee_in_stroops": 100,
    "base_reserve_in_stroops": 500_000_000,
    "max_tx_set_size": 1000,
    "successful_transaction_count": 3,
    "failed_transaction_count": 0,
    "operation_count": 4,
    "total_coins": "1000000000.0000000",
    "fee_pool": "100.0000000",
}

_HORIZON_TX_RECORD = {
    "hash": MOCK_TX_HASH,
    "ledger": MOCK_LATEST_LEDGER,
    "created_at": "2026-08-01T12:00:00Z",
    "successful": True,
    "source_account": MOCK_ACCOUNT,
    "memo": None,
    "fee_charged": "100",
    "max_fee": "200",
    "operation_count": 1,
}

_HORIZON_ASSET_RECORD = {
    "asset_type": "credit_alphanum4",
    "asset_code": "USDC",
    "asset_issuer": MOCK_ACCOUNT,
    "amount": "1000.0000000",
    "num_accounts": 5,
    "flags": {"auth_required": False, "auth_revocable": False, "auth_immutable": False},
}

# ---------------------------------------------------------------------------
# RPC fixtures
# ---------------------------------------------------------------------------

MOCK_INSTANCE_ENTRY = {
    "key": _instance_key(),
    "xdr": _instance_entry_xdr(),
    "lastModifiedLedgerSeq": 490300,
    "liveUntilLedgerSeq": 0,
}
MOCK_CODE_ENTRY = {
    "key": _code_key(),
    "xdr": _code_entry_xdr(),
    "lastModifiedLedgerSeq": 490301,
    "liveUntilLedgerSeq": 0,
}


def _rpc_health() -> dict:
    return {
        "status": "healthy",
        "latestLedger": MOCK_LATEST_LEDGER,
        "latestLedgerCloseTime": "1753000000",
        "oldestLedger": MOCK_LATEST_LEDGER - 1000,
        "oldestLedgerCloseTime": "1752990000",
        "ledgerRetentionWindow": 120960,
    }


def _rpc_latest_ledger() -> dict:
    return {
        "id": MOCK_TX_HASH,
        "protocolVersion": MOCK_PROTOCOL,
        "sequence": MOCK_LATEST_LEDGER,
        "closeTime": "1753000000",
        "headerXdr": "AAAA",
        "metadataXdr": "AAAA",
    }


def _rpc_network() -> dict:
    return {
        "passphrase": MOCK_NETWORK_PASSPHRASE,
        "protocolVersion": MOCK_PROTOCOL,
        "friendbotUrl": "https://friendbot.example.invalid",
    }


def _rpc_version() -> dict:
    return {
        "version": "mock",
        "commitHash": "deadbeef",
        "buildTimestamp": "2026-08-01T00:00:00Z",
        "protocolVersion": MOCK_PROTOCOL,
    }


def _rpc_method_dispatch(method: str, params: dict[str, Any]) -> tuple[int, dict]:
    """Return ``(jsonrpc_error_code|None, result)`` for a read-only RPC method."""
    if method == "getHealth":
        return None, _rpc_health()
    if method == "getLatestLedger":
        return None, _rpc_latest_ledger()
    if method == "getNetwork":
        return None, _rpc_network()
    if method == "getVersionInfo":
        return None, _rpc_version()
    if method == "getLedgerEntries":
        known = {
            MOCK_INSTANCE_ENTRY["key"]: MOCK_INSTANCE_ENTRY,
            MOCK_CODE_ENTRY["key"]: MOCK_CODE_ENTRY,
        }
        entries = []
        for key in (params or {}).get("keys") or []:
            if key in known:
                entries.append(known[key])
        return None, {"entries": entries, "latestLedger": MOCK_LATEST_LEDGER}
    if method == "getLedgers":
        return None, {"ledgers": [], "latestLedger": MOCK_LATEST_LEDGER, "cursor": None}
    if method == "getTransactions":
        return None, {"transactions": [], "latestLedger": MOCK_LATEST_LEDGER, "cursor": None}
    if method == "getEvents":
        return None, {
            "events": [
                {
                    "type": "contract",
                    "ledger": MOCK_LATEST_LEDGER - 1,
                    "ledgerClosedAt": "2026-08-01T12:00:00Z",
                    "contractId": MOCK_CONTRACT,
                    "id": "001-0000000001",
                    "txHash": MOCK_TX_HASH,
                    "topic": [],
                    "value": "AAAADwAAAAh0cmFuc2Zlcg==",
                }
            ],
            "latestLedger": MOCK_LATEST_LEDGER,
            "oldestLedger": MOCK_LATEST_LEDGER - 1000,
            "cursor": "001-0000000001",
        }
    if method == "getFeeStats":
        return None, {
            "sorobanInclusionFee": {
                "max": "1000",
                "min": "100",
                "mode": "250",
                "p10": "100",
                "p50": "250",
                "p90": "800",
                "p99": "950",
                "p999": "1000",
            },
            "inclusionFee": {
                "max": "1000",
                "min": "100",
                "mode": "250",
                "p10": "100",
                "p50": "250",
                "p90": "800",
                "p99": "950",
                "p999": "1000",
            },
            "latestLedger": MOCK_LATEST_LEDGER,
        }
    return -32601, {"message": f"Mock method not implemented: {method}"}


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------


class _RequestHandler(BaseHTTPRequestHandler):
    """Serves deterministic Horizon + JSON-RPC fixtures."""

    protocol_version = "HTTP/1.0"
    server_version = "MockStellar/1.0"

    def log_message(self, format, *args):  # pragma: no cover - quiet by design
        return

    def handle_error(self, request, client_address):  # pragma: no cover - quiet
        # Suppress broken-pipe noise when a client disconnects during shutdown.
        return

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 2 * 1024 * 1024:
            return b""
        return self.rfile.read(length)

    # -- Horizon-style reads --------------------------------------------

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/":
            return self._send_json(200, {"_links": {}})
        if path.startswith("/accounts/"):
            rest = path[len("/accounts/") :]
            if "/" in rest:
                address, sub = rest.split("/", 1)
                if sub == "transactions":
                    if address != MOCK_ACCOUNT:
                        return self._send_json(404, {"title": "Resource Missing"})
                    return self._send_json(
                        200,
                        {
                            "_embedded": {"records": [_HORIZON_TX_RECORD]},
                            "_links": {"next": {"href": None}},
                        },
                    )
            if rest == MOCK_ACCOUNT:
                return self._send_json(200, _HORIZON_ACCOUNT)
            return self._send_json(404, {"title": "Resource Missing"})
        if path.startswith("/transactions/"):
            tx_hash = path[len("/transactions/") :].lower()
            if tx_hash == MOCK_TX_HASH:
                return self._send_json(200, _HORIZON_TRANSACTION)
            return self._send_json(404, {"title": "Resource Missing"})
        if path.startswith("/ledgers/"):
            sequence = path[len("/ledgers/") :]
            if sequence == str(MOCK_LATEST_LEDGER):
                return self._send_json(200, _HORIZON_LEDGER)
            return self._send_json(404, {"title": "Resource Missing"})
        if path == "/assets":
            return self._send_json(
                200,
                {
                    "_embedded": {"records": [_HORIZON_ASSET_RECORD]},
                    "_links": {"next": {"href": None}},
                },
            )
        return self._send_json(404, {"title": "Not Found"})

    # -- Stellar RPC (JSON-RPC 2.0) --------------------------------------

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/rpc":
            return self._send_json(404, {"error": "Not found"})
        try:
            request = json.loads(self._read_body().decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return self._send_json(
                200,
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
            )
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
            return self._send_json(
                200,
                {
                    "jsonrpc": "2.0",
                    "id": request.get("id") if isinstance(request, dict) else None,
                    "error": {"code": -32600, "message": "Invalid Request"},
                },
            )
        request_id = request.get("id")
        code, result = _rpc_method_dispatch(str(request.get("method")), request.get("params") or {})
        if code is not None:
            return self._send_json(
                200,
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": code, "message": result.get("message")},
                },
            )
        return self._send_json(200, {"jsonrpc": "2.0", "id": request_id, "result": result})


class MockStellarServer:
    """An in-process deterministic Horizon + Stellar RPC server.

    Binds to an ephemeral loopback port. Use :attr:`horizon_url` /
    :attr:`rpc_url` as ``STELLAR_HORIZON_URL`` / ``STELLAR_RPC_URL`` for a
    ``custom``/local network configuration.
    """

    def __init__(self) -> None:
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _RequestHandler)
        self._thread: threading.Thread | None = None
        host, port = self._httpd.server_address[:2]
        self._base_url = f"http://{host}:{port}"

    @property
    def horizon_url(self) -> str:
        """Base URL for Horizon-style JSON endpoints."""
        return self._base_url

    @property
    def rpc_url(self) -> str:
        """Base URL for the JSON-RPC Stellar RPC endpoint."""
        return f"{self._base_url}/rpc"

    @property
    def port(self) -> int:
        return int(self._httpd.server_address[1])

    def start(self) -> MockStellarServer:
        """Start serving in a background thread (idempotent)."""
        if self._thread and self._thread.is_alive():
            return self
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="mock-stellar", daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        """Shut the server down and release the port."""
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None


def find_free_port() -> int:
    """Return a currently-free TCP port on the loopback interface."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def run_mock_server() -> None:  # pragma: no cover - manual local-dev helper
    """Start the mock server in the foreground for local development."""
    server = MockStellarServer().start()
    print("Mock Stellar network listening:")
    print("  Horizon:", server.horizon_url)
    print("  RPC:    ", server.rpc_url)
    print("Press Ctrl+C to stop.")
    try:
        while True:
            threading.Event().wait(3600)
    except KeyboardInterrupt:
        server.stop()


if __name__ == "__main__":  # pragma: no cover
    run_mock_server()
