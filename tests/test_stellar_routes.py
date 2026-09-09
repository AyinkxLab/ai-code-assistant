"""Tests for the read-only /stellar API endpoints."""

import pytest

from app.services import ratelimit

VALID_ADDRESS = "GALAXYVOIDAOPZTDLHILAJQKCVVFMD4IKLXLSZV5YHO7VY74IWZILUTO"
DOCS_CONTRACT = "CCPYZFKEAXHHS5VVW5J45TOU7S2EODJ7TZNJIA5LKDVL3PESCES6FNCI"


@pytest.fixture(autouse=True)
def _reset_ratelimit():
    ratelimit.reset()
    yield
    ratelimit.reset()


class TestAuth:
    def test_requires_login(self, client):
        assert client.get("/stellar/").status_code == 302
        assert client.get("/stellar/api/network").status_code == 302


class TestNetworkEndpoint:
    def test_network(self, client, make_user, login, monkeypatch):
        make_user()
        login()
        monkeypatch.setattr(
            "app.stellar.routes.network_status",
            lambda: {"network": {"network": "testnet"}, "rpc_available": False},
        )
        response = client.get("/stellar/api/network")
        assert response.status_code == 200
        assert response.get_json()["network"]["network"] == "testnet"

    def test_network_rich_rpc_status_passthrough(self, client, make_user, login, monkeypatch):
        # The panel renderer reads these exact fields for its live status card.
        make_user()
        login()
        monkeypatch.setattr(
            "app.stellar.routes.network_status",
            lambda: {
                "network": {
                    "network": "testnet",
                    "network_passphrase": "Test SDF Network ; September 2015",
                    "horizon_url": "https://horizon-testnet.stellar.org",
                    "rpc_url": "https://soroban-testnet.stellar.org",
                    "is_public": True,
                },
                "rpc_available": True,
                "rpc_endpoint": "https://soroban-testnet.stellar.org",
                "health": {"status": "healthy", "ledgerRetentionWindow": 120960},
                "latest_ledger": {"sequence": 490314},
                "rpc_network": {"passphrase": "Test SDF Network ; September 2015"},
            },
        )
        response = client.get("/stellar/api/network")
        assert response.status_code == 200
        data = response.get_json()
        assert data["rpc_available"] is True
        assert data["health"]["status"] == "healthy"
        assert data["latest_ledger"]["sequence"] == 490314
        assert data["network"]["horizon_url"].startswith("https://")
        assert data["network"]["rpc_url"].startswith("https://")

    def test_network_rate_limited(self, client, make_user, login, monkeypatch):
        make_user()
        login()
        monkeypatch.setattr("app.services.ratelimit.hit", lambda *a, **k: False)
        response = client.get("/stellar/api/network")
        assert response.status_code == 429


class TestAccountEndpoint:
    def test_missing_address(self, client, make_user, login):
        make_user()
        login()
        response = client.get("/stellar/api/account")
        assert response.status_code == 400

    def test_invalid_address(self, client, make_user, login):
        make_user()
        login()
        response = client.get("/stellar/api/account?address=nope")
        assert response.status_code == 400

    def test_success(self, client, make_user, login, monkeypatch):
        make_user()
        login()
        monkeypatch.setattr(
            "app.stellar.routes.inspect_account",
            lambda address: {
                "address": address,
                "network": {"network": "testnet"},
                "account": {"sequence": "1"},
                "ledger_freshness": {"available": False},
            },
        )
        response = client.get(f"/stellar/api/account?address={VALID_ADDRESS}")
        assert response.status_code == 200
        assert response.get_json()["account"]["sequence"] == "1"


class TestContractEndpoint:
    def test_missing_contract(self, client, make_user, login):
        make_user()
        login()
        response = client.get("/stellar/api/contract")
        assert response.status_code == 400

    def test_success(self, client, make_user, login, monkeypatch):
        make_user()
        login()
        monkeypatch.setattr(
            "app.stellar.routes.inspect_contract",
            lambda cid, wasm_hash=None: {
                "contract_id": cid,
                "network": {"network": "testnet"},
                "found": True,
                "latest_ledger": 10,
                "instance_entry": {"lastModifiedLedgerSeq": 5},
                "decoded": False,
            },
        )
        response = client.get(f"/stellar/api/contract?address={DOCS_CONTRACT}")
        assert response.status_code == 200
        assert response.get_json()["found"] is True

    def test_success_rich_decoded_contract_payload(self, client, make_user, login, monkeypatch):
        # The panel renders the decoded structured view and the bounded raw XDR
        # from exactly these fields.
        make_user()
        login()
        monkeypatch.setattr(
            "app.stellar.routes.inspect_contract",
            lambda cid, wasm_hash=None: {
                "contract_id": cid,
                "network": {"network": "testnet"},
                "found": True,
                "latest_ledger": 10,
                "decoded": True,
                "instance_entry": {
                    "lastModifiedLedgerSeq": 5,
                    "liveUntilLedgerSeq": 0,
                    "xdr": "AAAAAA==",
                    "decoded": {
                        "decoded": True,
                        "type": "CONTRACT_DATA",
                        "detail": {
                            "contract_id": cid,
                            "durability": "persistent",
                            "value": {"type": "instance", "executable": {"type": "wasm"}},
                        },
                    },
                },
            },
        )
        response = client.get(f"/stellar/api/contract?address={DOCS_CONTRACT}")
        assert response.status_code == 200
        payload = response.get_json()
        entry = payload["instance_entry"]
        assert entry["decoded"]["decoded"] is True
        assert entry["decoded"]["detail"]["contract_id"] == DOCS_CONTRACT
        assert entry["xdr"] == "AAAAAA=="
        assert payload["decoded"] is True


class TestLedgerEntryEndpoint:
    def test_missing_key(self, client, make_user, login):
        make_user()
        login()
        response = client.get("/stellar/api/ledger-entry")
        assert response.status_code == 400

    def test_invalid_key(self, client, make_user, login):
        make_user()
        login()
        response = client.get("/stellar/api/ledger-entry?key=nonsense")
        assert response.status_code == 400

    def test_success_rich_ledger_payload(self, client, make_user, login, monkeypatch):
        make_user()
        login()
        monkeypatch.setattr(
            "app.stellar.routes.inspect_ledger_entry",
            lambda key: {
                "network": {"network": "testnet"},
                "latest_ledger": 10,
                "found": True,
                "decoded": True,
                "entry": {
                    "lastModifiedLedgerSeq": 7,
                    "liveUntilLedgerSeq": 0,
                    "xdr": "AAAAAA==",
                    "decoded": {
                        "decoded": True,
                        "type": "CONTRACT_DATA",
                        "detail": {"contract_id": DOCS_CONTRACT, "durability": "persistent"},
                    },
                },
            },
        )
        response = client.get("/stellar/api/ledger-entry?key=AAAAAA==")
        assert response.status_code == 200
        payload = response.get_json()
        assert payload["found"] is True
        assert payload["entry"]["decoded"]["detail"]["durability"] == "persistent"
        assert payload["entry"]["xdr"] == "AAAAAA=="
