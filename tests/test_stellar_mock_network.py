"""End-to-end tests against the deterministic offline mock Stellar network.

These tests boot the in-process mock (see ``tests/conftest.py``) and drive the
*real* ``StellarService`` and ``SorobanRpcClient`` code paths against it over
loopback HTTP — no fake sessions, no external network.
"""

import pytest

from app.services.soroban_rpc import SorobanRpcClient
from app.services.stellar import AccountError, StellarService
from app.services.stellar_inspection import inspect_contract, inspect_ledger_entry
from app.services.stellar_mock import (
    MOCK_ACCOUNT,
    MOCK_CODE,
    MOCK_CODE_ENTRY,
    MOCK_CONTRACT,
    MOCK_LATEST_LEDGER,
    MOCK_NETWORK_PASSPHRASE,
    MOCK_PROTOCOL,
    MOCK_TX_HASH,
    MOCK_WASM_HASH_HEX,
    _instance_key,
)
from app.services.stellar_xdr import ledger_key_contract_code
from app.services.stellar_xdr_decode import decode_ledger_entry_data

MISSING_ACCOUNT = "G" + "A" * 55


def _configure_mock(app, server):
    app.config.update(
        STELLAR_NETWORK="custom",
        STELLAR_HORIZON_URL=server.horizon_url,
        STELLAR_RPC_URL=server.rpc_url,
    )


class TestHorizonEndToEnd:
    def test_get_account(self, app, mock_stellar):
        _configure_mock(app, mock_stellar)
        with app.app_context():
            service = StellarService()
            account = service.get_account(MOCK_ACCOUNT)
        assert account["account_id"] == MOCK_ACCOUNT
        assert account["sequence"] == "1234567890"
        assert account["subentry_count"] == 1
        assert account["balances"][0]["asset_type"] == "native"
        assert account["balances"][0]["balance"] == "50.0000000"

    def test_missing_account_raises(self, app, mock_stellar):
        _configure_mock(app, mock_stellar)
        with app.app_context(), pytest.raises(AccountError):
            StellarService().get_account(MISSING_ACCOUNT)

    def test_get_ledger_and_transaction(self, app, mock_stellar):
        _configure_mock(app, mock_stellar)
        with app.app_context():
            service = StellarService()
            ledger = service.get_ledger(MOCK_LATEST_LEDGER)
            transaction = service.get_transaction(MOCK_TX_HASH)
        assert ledger["sequence"] == MOCK_LATEST_LEDGER
        assert ledger["protocol_version"] == MOCK_PROTOCOL
        assert transaction["hash"] == MOCK_TX_HASH
        assert transaction["successful"] is True

    def test_account_transactions(self, app, mock_stellar):
        _configure_mock(app, mock_stellar)
        with app.app_context():
            result = StellarService().get_account_transactions(MOCK_ACCOUNT)
        assert result["records"][0]["hash"] == MOCK_TX_HASH


class TestRpcEndToEnd:
    def test_health_latest_network(self, app, mock_stellar):
        _configure_mock(app, mock_stellar)
        with app.app_context():
            client = SorobanRpcClient()
            health = client.get_health()
            latest = client.get_latest_ledger()
            network = client.get_network()
        assert health["status"] == "healthy"
        assert health["latestLedger"] == MOCK_LATEST_LEDGER
        assert latest["sequence"] == MOCK_LATEST_LEDGER
        assert latest["protocolVersion"] == MOCK_PROTOCOL
        assert network["passphrase"] == MOCK_NETWORK_PASSPHRASE

    def test_contract_instance_entry_decodes(self, app, mock_stellar):
        _configure_mock(app, mock_stellar)
        with app.app_context():
            client = SorobanRpcClient()
            result = client.get_ledger_entries([_instance_key()], clip_xdr=False)
        assert result["latestLedger"] == MOCK_LATEST_LEDGER
        assert result["entries"]
        decoded = decode_ledger_entry_data(result["entries"][0]["xdr"])
        assert decoded["decoded"] is True
        assert decoded["type"] == "CONTRACT_DATA"
        assert decoded["detail"]["contract_id"] == MOCK_CONTRACT
        assert decoded["detail"]["value"]["executable"]["wasm_hash"] == MOCK_WASM_HASH_HEX

    def test_contract_code_entry_returns_byte_size(self, app, mock_stellar):
        _configure_mock(app, mock_stellar)
        code_key = ledger_key_contract_code(bytes(range(32)))
        with app.app_context():
            client = SorobanRpcClient()
            result = client.get_ledger_entries([code_key], clip_xdr=False)
        assert result["entries"][0]["key"] == MOCK_CODE_ENTRY["key"]
        decoded = decode_ledger_entry_data(result["entries"][0]["xdr"])
        assert decoded["decoded"] is True
        assert decoded["detail"]["wasm_hash"] == MOCK_WASM_HASH_HEX
        assert decoded["detail"]["code_byte_size"] == len(MOCK_CODE)


class TestInspectionEndToEnd:
    def test_inspect_contract_against_mock(self, app, mock_stellar):
        _configure_mock(app, mock_stellar)
        with app.app_context():
            result = inspect_contract(MOCK_CONTRACT, wasm_hash=MOCK_WASM_HASH_HEX)
        assert result["found"] is True
        assert result["decoded"] is True
        assert result["instance_entry"]["decoded"]["detail"]["contract_id"] == MOCK_CONTRACT
        assert result["code_found"] is True
        assert result["code_byte_size"] == len(MOCK_CODE)

    def test_inspect_ledger_entry_against_mock(self, app, mock_stellar):
        _configure_mock(app, mock_stellar)
        with app.app_context():
            result = inspect_ledger_entry(_instance_key())
        assert result["found"] is True
        assert result["decoded"] is True
        assert result["entry"]["decoded"]["detail"]["contract_id"] == MOCK_CONTRACT
