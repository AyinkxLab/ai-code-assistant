"""Tests for the Horizon read endpoints added on StellarService."""

import json

import pytest

from app.services.stellar import AccountError, StellarError, StellarService

VALID_ADDRESS = "G" + "A" * 55


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


class TestGetLedger:
    def test_success(self, app):
        payload = {
            "sequence": 100,
            "hash": "abc",
            "closed_at": "2024-01-01T00:00:00Z",
            "protocol_version": 22,
            "base_fee_in_stroops": 100,
            "successful_transaction_count": 3,
            "failed_transaction_count": 0,
            "operation_count": 5,
            "total_coins": "1000000",
        }
        session = _FakeSession(_FakeResponse(200, payload))
        with app.app_context():
            service = StellarService(session=session)
            ledger = service.get_ledger(100)
        assert ledger["sequence"] == 100
        assert ledger["protocol_version"] == 22
        assert service.config.horizon_url + "/ledgers/100" == session.requested_url

    def test_invalid_sequence(self, app):
        with app.app_context():
            service = StellarService()
            with pytest.raises(StellarError):
                service.get_ledger(-1)
            with pytest.raises(StellarError):
                service.get_ledger("abc")


class TestGetAssets:
    def test_success_bounded(self, app):
        records = [
            {
                "asset_type": "credit_alphanum4",
                "asset_code": "USDC",
                "asset_issuer": VALID_ADDRESS,
                "amount": "1000",
                "num_accounts": 5,
            }
            for _ in range(150)
        ]
        payload = {
            "_embedded": {"records": records},
            "_links": {"next": {"href": "https://x?cursor=abc"}, "prev": {"href": None}},
        }
        session = _FakeSession(_FakeResponse(200, payload))
        with app.app_context():
            service = StellarService(session=session)
            result = service.get_assets(cursor="CURSOR", limit=200)
        assert len(result["records"]) == 100
        assert result["records"][0]["asset_code"] == "USDC"
        assert result["next"]

    def test_invalid_limit(self, app):
        with app.app_context():
            service = StellarService()
            with pytest.raises(StellarError):
                service.get_assets(limit="many")


class TestGetAccountTransactions:
    def test_success(self, app):
        payload = {
            "_embedded": {
                "records": [
                    {
                        "hash": "a" * 64,
                        "ledger": 100,
                        "created_at": "2024-01-01T00:00:00Z",
                        "successful": True,
                        "source_account": VALID_ADDRESS,
                        "memo": "",
                    }
                ]
            },
            "_links": {"next": {"href": "https://x?cursor=z"}},
        }
        session = _FakeSession(_FakeResponse(200, payload))
        with app.app_context():
            service = StellarService(session=session)
            result = service.get_account_transactions(VALID_ADDRESS)
        assert len(result["records"]) == 1
        assert result["records"][0]["successful"] is True
        assert "transactions" in session.requested_url

    def test_invalid_address(self, app):
        with app.app_context():
            service = StellarService()
            with pytest.raises(AccountError):
                service.get_account_transactions("nope")

    def test_limit_capped(self, app):
        session = _FakeSession(_FakeResponse(200, {"_embedded": {"records": []}}))
        with app.app_context():
            service = StellarService(session=session)
            service.get_account_transactions(VALID_ADDRESS, limit=9999)
        assert "limit=100" in session.requested_url
