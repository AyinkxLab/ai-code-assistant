"""Tests for the read-only Stellar/Soroban inspection service."""

import json

import pytest

from app.services.stellar import AccountError, StellarService
from app.services.stellar_inspection import (
    inspect_account,
    inspect_contract,
    inspect_ledger_entry,
    network_status,
)
from app.services.stellar_xdr import ledger_key_for_contract

VALID_ADDRESS = "GALAXYVOIDAOPZTDLHILAJQKCVVFMD4IKLXLSZV5YHO7VY74IWZILUTO"
DOCS_CONTRACT = "CCPYZFKEAXHHS5VVW5J45TOU7S2EODJ7TZNJIA5LKDVL3PESCES6FNCI"
HASH64 = "a" * 64
INVALID_STRUCTURAL_ADDRESS = "G" + "A" * 55


class _FakeResponse:
    def __init__(self, status_code, payload=None, raw=b""):
        self.status_code = status_code
        self._raw = raw or json.dumps(payload or {}).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def iter_content(self, chunk_size):
        yield self._raw


class _FakeSession:
    def __init__(self, response):
        self.response = response
        self.requested_url = None

    def get(self, url, timeout=None, stream=None, headers=None, allow_redirects=False):
        self.requested_url = url
        return self.response

    def post(self, url, data=None, timeout=None, stream=None, headers=None, allow_redirects=False):
        self.requested_url = url
        return self.response


def _rpc_result(payload):
    return {"jsonrpc": "2.0", "id": 1, "result": payload}


class _StubRpc:
    """A minimal RPC client double used where the full client is overkill."""

    def __init__(self, *, health=None, latest=None, network=None, entries=None):
        from types import SimpleNamespace

        self._health = health or {"status": "healthy", "latestLedger": 10}
        self._latest = latest or {"sequence": 10, "protocolVersion": 22}
        self._network = network or {"passphrase": "Test SDF Network ; September 2015"}
        self._entries = entries or {"entries": [], "latestLedger": 10}
        self.config = SimpleNamespace(
            rpc_url="https://soroban-testnet.stellar.org",
            to_dict=lambda: {"network": "testnet"},
        )

    def get_health(self):
        return self._health

    def get_latest_ledger(self):
        return self._latest

    def get_network(self):
        return self._network

    def get_ledger_entries(self, keys, xdr_format="base64"):
        return self._entries


class TestNetworkStatus:
    def test_available(self, app):
        stub = _StubRpc()
        with app.app_context():
            result = network_status(rpc=stub)
        assert result["rpc_available"] is True
        assert result["health"]["status"] == "healthy"
        assert result["latest_ledger"]["sequence"] == 10
        assert result["network"]["network"] == "testnet"

    def test_unavailable(self, app):
        class _Broken(_StubRpc):
            def get_health(self):
                from app.services.soroban_rpc import SorobanRpcUnavailableError

                raise SorobanRpcUnavailableError("unreachable")

        with app.app_context():
            result = network_status(rpc=_Broken())
        assert result["rpc_available"] is False
        assert "unreachable" in result["rpc_error"]


class TestInspectAccount:
    def test_success_with_ledger_freshness(self, app):
        session = _FakeSession(
            _FakeResponse(
                200,
                {
                    "account_id": VALID_ADDRESS,
                    "sequence": "123",
                    "subentry_count": 1,
                    "balances": [{"asset_type": "native", "balance": "50.0000000"}],
                },
            )
        )
        stub = _StubRpc()
        with app.app_context():
            result = inspect_account(
                VALID_ADDRESS, service=StellarService(session=session), rpc=stub
            )
        assert result["account"]["sequence"] == "123"
        assert result["ledger_freshness"]["available"] is True
        assert result["ledger_freshness"]["sequence"] == 10
        assert result["network"]["network"] == "testnet"

    def test_invalid_address_rejected(self, app):
        with app.app_context(), pytest.raises(AccountError):
            inspect_account("not-an-address")

    def test_bad_checksum_rejected(self, app):
        with app.app_context(), pytest.raises(AccountError):
            inspect_account("G" + "A" * 55)

    def test_ledger_freshness_unavailable(self, app):
        class _Broken(_StubRpc):
            def get_latest_ledger(self):
                from app.services.soroban_rpc import SorobanRpcUnavailableError

                raise SorobanRpcUnavailableError("down")

        session = _FakeSession(_FakeResponse(200, {"account_id": VALID_ADDRESS, "sequence": "1"}))
        with app.app_context():
            result = inspect_account(
                VALID_ADDRESS, service=StellarService(session=session), rpc=_Broken()
            )
        assert result["ledger_freshness"]["available"] is False


class TestInspectContract:
    def test_found(self, app):
        entry = {
            "key": ledger_key_for_contract(DOCS_CONTRACT),
            "xdr": "AAAA",
            "lastModifiedLedgerSeq": 2552504,
            "liveUntilLedgerSeq": 0,
        }
        stub = _StubRpc(entries={"entries": [entry], "latestLedger": 2552990})
        with app.app_context():
            result = inspect_contract(DOCS_CONTRACT, rpc=stub)
        assert result["found"] is True
        assert result["instance_entry"]["lastModifiedLedgerSeq"] == 2552504
        assert result["decoded"] is False
        assert result["latest_ledger"] == 2552990

    def test_not_found(self, app):
        stub = _StubRpc(entries={"entries": [], "latestLedger": 5})
        with app.app_context():
            result = inspect_contract(DOCS_CONTRACT, rpc=stub)
        assert result["found"] is False
        assert result["instance_entry"] is None

    def test_with_wasm_hash(self, app):
        entry = {
            "key": ledger_key_for_contract(DOCS_CONTRACT),
            "xdr": "AAAA",
            "lastModifiedLedgerSeq": 1,
            "liveUntilLedgerSeq": 0,
        }
        code_entry = {
            "key": "AAAA",
            "xdr": "wasm" * 50,
            "lastModifiedLedgerSeq": 2,
            "liveUntilLedgerSeq": 0,
        }
        stub = _StubRpc(entries={"entries": [entry], "latestLedger": 10})
        with app.app_context():
            stub.get_ledger_entries = lambda keys, xdr_format="base64": {
                "entries": [entry if "xdr" not in keys[0] else code_entry],
                "latestLedger": 10,
            }
            result = inspect_contract(DOCS_CONTRACT, rpc=stub, wasm_hash=HASH64)
        assert result["wasm_hash"] == HASH64
        assert result["code_found"] is True

    def test_invalid_contract_rejected(self, app):
        with app.app_context(), pytest.raises(AccountError):
            inspect_contract(VALID_ADDRESS)  # G-address is not a contract id

    def test_invalid_wasm_hash_rejected(self, app):
        stub = _StubRpc(entries={"entries": [], "latestLedger": 1})
        with app.app_context(), pytest.raises(AccountError):
            inspect_contract(DOCS_CONTRACT, rpc=stub, wasm_hash="zz")


class TestInspectLedgerEntry:
    def test_found(self, app):
        key = ledger_key_for_contract(DOCS_CONTRACT)
        entry = {"key": key, "xdr": "AAAA", "lastModifiedLedgerSeq": 7, "liveUntilLedgerSeq": 0}
        stub = _StubRpc(entries={"entries": [entry], "latestLedger": 10})
        with app.app_context():
            result = inspect_ledger_entry(key, rpc=stub)
        assert result["found"] is True
        assert result["entry"]["lastModifiedLedgerSeq"] == 7

    def test_invalid_key_rejected(self, app):
        with app.app_context(), pytest.raises(AccountError):
            inspect_ledger_entry("not base64")
