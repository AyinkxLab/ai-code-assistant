"""Tests for the read-only Stellar Flask CLI commands."""

from app.services.stellar import AccountError

VALID_ADDRESS = "GALAXYVOIDAOPZTDLHILAJQKCVVFMD4IKLXLSZV5YHO7VY74IWZILUTO"
DOCS_CONTRACT = "CCPYZFKEAXHHS5VVW5J45TOU7S2EODJ7TZNJIA5LKDVL3PESCES6FNCI"


def _runner(app):
    return app.test_cli_runner()


class TestStellarNetworkCLI:
    def test_network(self, app):
        result = _runner(app).invoke(args=["stellar", "network"])
        assert result.exit_code == 0
        assert "testnet" in result.output
        assert "horizon-testnet.stellar.org" in result.output

    def test_network_explicit_mainnet(self, app):
        result = _runner(app).invoke(args=["stellar", "network", "--network", "mainnet"])
        assert result.exit_code == 0
        assert "mainnet" in result.output

    def test_network_unknown(self, app):
        result = _runner(app).invoke(args=["stellar", "network", "--network", "bogus"])
        assert result.exit_code != 0


class TestStellarValidateCLI:
    def test_valid_address(self, app):
        result = _runner(app).invoke(args=["stellar", "validate", VALID_ADDRESS])
        assert result.exit_code == 0
        assert "valid" in result.output

    def test_invalid_address(self, app):
        result = _runner(app).invoke(args=["stellar", "validate", "nope"])
        assert result.exit_code != 0


class TestStellarAccountCLI:
    def test_account_success(self, app, monkeypatch):
        class _FakeService:
            def get_account(self, address):
                return {
                    "account_id": address,
                    "sequence": "5",
                    "subentry_count": 1,
                    "balances": [{"asset_type": "native", "balance": "10.0000000"}],
                }

        monkeypatch.setattr("app.services.stellar.StellarService", lambda *a, **k: _FakeService())
        result = _runner(app).invoke(args=["stellar", "account", VALID_ADDRESS])
        assert result.exit_code == 0
        assert "sequence: 5" in result.output

    def test_account_error(self, app, monkeypatch):
        class _FakeService:
            def get_account(self, address):
                raise AccountError("not found")

        monkeypatch.setattr("app.services.stellar.StellarService", lambda *a, **k: _FakeService())
        result = _runner(app).invoke(args=["stellar", "account", VALID_ADDRESS])
        assert result.exit_code != 0


class TestStellarHealthCLI:
    def test_health_unavailable(self, app, monkeypatch):
        class _FakeClient:
            def get_health(self):
                from app.services.soroban_rpc import SorobanRpcUnavailableError

                raise SorobanRpcUnavailableError("offline")

            def get_latest_ledger(self):
                raise AssertionError("should not be called")

        monkeypatch.setattr("app.services.soroban_rpc.SorobanRpcClient", lambda: _FakeClient())
        result = _runner(app).invoke(args=["stellar", "health"])
        assert result.exit_code != 0


class TestStellarContractCLI:
    def test_contract(self, app, monkeypatch):
        monkeypatch.setattr(
            "app.services.stellar_inspection.inspect_contract",
            lambda cid, wasm_hash=None: {
                "contract_id": cid,
                "network": {"network": "testnet"},
                "found": True,
                "latest_ledger": 10,
                "instance_entry": {"lastModifiedLedgerSeq": 5},
            },
        )
        result = _runner(app).invoke(args=["stellar", "contract", DOCS_CONTRACT])
        assert result.exit_code == 0
        assert "found: True" in result.output

    def test_contract_invalid(self, app):
        result = _runner(app).invoke(args=["stellar", "contract", "not-a-contract"])
        assert result.exit_code != 0


class TestStellarLedgerEntryCLI:
    def test_ledger_entry(self, app, monkeypatch):
        monkeypatch.setattr(
            "app.services.stellar_inspection.inspect_ledger_entry",
            lambda key: {
                "network": {"network": "testnet"},
                "found": True,
                "latest_ledger": 3,
                "entry": {"lastModifiedLedgerSeq": 1},
            },
        )
        result = _runner(app).invoke(args=["stellar", "ledger-entry", "AAAABgAA"])
        assert result.exit_code == 0
        assert "found: True" in result.output
