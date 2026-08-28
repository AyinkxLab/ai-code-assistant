"""Tests for the read-only Soroban/Stellar RPC client."""

import json

import pytest

from app.services.soroban_rpc import (
    SorobanRpcClient,
    SorobanRpcError,
    SorobanRpcInvalidParamsError,
    SorobanRpcResponseError,
    SorobanRpcUnavailableError,
    get_soroban_rpc_client,
)
from app.services.stellar import NetworkError
from app.services.stellar_xdr import ledger_key_for_contract

HASH64 = "a" * 64
DOCS_CONTRACT = "CCPYZFKEAXHHS5VVW5J45TOU7S2EODJ7TZNJIA5LKDVL3PESCES6FNCI"
#: A realistic base64 LedgerKey generated from a known-good contract id.
SAMPLE_LEDGER_KEY = ledger_key_for_contract(DOCS_CONTRACT)


@pytest.fixture(autouse=True)
def _reset_rpc_id(monkeypatch):
    """Reset the module-level JSON-RPC id counter so responses can echo id 1."""
    import app.services.soroban_rpc as mod

    monkeypatch.setattr(mod, "_request_id", 0)
    yield


class _FakeResponse:
    def __init__(self, status_code, payload=None, raw=b""):
        self.status_code = status_code
        self._raw = raw or json.dumps(payload or {}).encode()
        self.requested_url = None

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
        self.requested_body = None
        self.kwargs = {}

    def post(self, url, data=None, timeout=None, stream=None, headers=None, allow_redirects=False):
        self.requested_url = url
        self.requested_body = data
        self.kwargs["allow_redirects"] = allow_redirects
        self.response.requested_url = url
        return self.response


def _rpc_result(payload):
    return {"jsonrpc": "2.0", "id": 1, "result": payload}


def _client(app, response, **kwargs):
    return SorobanRpcClient(session=_FakeSession(response), **kwargs)


class TestHealth:
    def test_get_health_success(self, app):
        payload = {
            "status": "healthy",
            "latestLedger": 3730763,
            "latestLedgerCloseTime": "1784671485",
            "oldestLedger": 3609804,
            "oldestLedgerCloseTime": "1784065656",
            "ledgerRetentionWindow": 120960,
        }
        session = _FakeSession(_FakeResponse(200, _rpc_result(payload)))
        with app.app_context():
            client = SorobanRpcClient(session=session)
            health = client.get_health()
        assert health["status"] == "healthy"
        assert health["latestLedger"] == 3730763
        assert health["ledgerRetentionWindow"] == 120960
        assert session.requested_url == "https://soroban-testnet.stellar.org"
        assert session.kwargs["allow_redirects"] is False
        assert json.loads(session.requested_body)["method"] == "getHealth"


class TestLatestLedger:
    def test_get_latest_ledger_success(self, app):
        payload = {
            "id": HASH64,
            "protocolVersion": 22,
            "sequence": 490314,
            "closeTime": "1752722132",
            "headerXdr": "AAAA",
            "metadataXdr": "BBBB",
        }
        with app.app_context():
            client = _client(app, _FakeResponse(200, _rpc_result(payload)))
            ledger = client.get_latest_ledger()
        assert ledger["sequence"] == 490314
        assert ledger["protocolVersion"] == 22
        assert ledger["id"] == HASH64


class TestNetwork:
    def test_get_network(self, app):
        payload = {"passphrase": "Test SDF Network ; September 2015", "protocolVersion": 20}
        with app.app_context():
            client = _client(app, _FakeResponse(200, _rpc_result(payload)))
            network = client.get_network()
        assert network["passphrase"] == "Test SDF Network ; September 2015"


class TestLedgerEntries:
    def test_get_ledger_entries_success(self, app):
        payload = {
            "entries": [
                {
                    "key": "AAAABgAA",
                    "xdr": "AAAA",
                    "lastModifiedLedgerSeq": 2552504,
                    "liveUntilLedgerSeq": 0,
                }
            ],
            "latestLedger": 2552990,
        }
        session = _FakeSession(_FakeResponse(200, _rpc_result(payload)))
        with app.app_context():
            client = SorobanRpcClient(session=session)
            result = client.get_ledger_entries([SAMPLE_LEDGER_KEY])
        assert result["latestLedger"] == 2552990
        assert result["entries"][0]["lastModifiedLedgerSeq"] == 2552504
        request = json.loads(session.requested_body)
        assert request["method"] == "getLedgerEntries"
        assert request["params"]["keys"] == [SAMPLE_LEDGER_KEY]

    def test_empty_keys_rejected(self, app):
        with app.app_context():
            client = _client(app, _FakeResponse(200, _rpc_result({})))
            with pytest.raises(SorobanRpcInvalidParamsError):
                client.get_ledger_entries([])

    def test_too_many_keys_rejected(self, app):
        with app.app_context():
            client = SorobanRpcClient(max_ledger_keys=2)
            with pytest.raises(SorobanRpcInvalidParamsError):
                client.get_ledger_entries([SAMPLE_LEDGER_KEY, SAMPLE_LEDGER_KEY, SAMPLE_LEDGER_KEY])

    def test_invalid_key_rejected(self, app):
        with app.app_context():
            client = _client(app, _FakeResponse(200, _rpc_result({})))
            with pytest.raises(SorobanRpcInvalidParamsError):
                client.get_ledger_entries(["not base64!!"])

    def test_json_format_passed_through(self, app):
        session = _FakeSession(_FakeResponse(200, _rpc_result({"entries": [], "latestLedger": 1})))
        with app.app_context():
            client = SorobanRpcClient(session=session)
            client.get_ledger_entries([SAMPLE_LEDGER_KEY], xdr_format="json")
        params = json.loads(session.requested_body)["params"]
        assert params["xdrFormat"] == "json"

    def test_bad_format_rejected(self, app):
        with app.app_context():
            client = _client(app, _FakeResponse(200, _rpc_result({})))
            with pytest.raises(SorobanRpcInvalidParamsError):
                client.get_ledger_entries([SAMPLE_LEDGER_KEY], xdr_format="xml")


class TestTransaction:
    def test_get_transaction_success(self, app):
        payload = {
            "status": "SUCCESS",
            "txHash": HASH64,
            "latestLedger": 490314,
            "ledger": 490252,
            "createdAt": "1752721821",
            "applicationOrder": 3,
            "feeBump": False,
            "envelopeXdr": "AAAA",
        }
        session = _FakeSession(_FakeResponse(200, _rpc_result(payload)))
        with app.app_context():
            client = SorobanRpcClient(session=session)
            result = client.get_transaction(HASH64)
        assert result["status"] == "SUCCESS"
        assert result["ledger"] == 490252
        request = json.loads(session.requested_body)
        assert request["params"]["hash"] == HASH64

    def test_not_found_status_not_an_error(self, app):
        payload = {
            "status": "NOT_FOUND",
            "txHash": HASH64,
            "latestLedger": 1,
            "latestLedgerCloseTime": "1",
            "oldestLedger": 1,
            "oldestLedgerCloseTime": "1",
        }
        with app.app_context():
            client = _client(app, _FakeResponse(200, _rpc_result(payload)))
            result = client.get_transaction(HASH64)
        assert result["status"] == "NOT_FOUND"

    def test_invalid_hash_rejected(self, app):
        with app.app_context():
            client = _client(app, _FakeResponse(200, _rpc_result({})))
            with pytest.raises(SorobanRpcInvalidParamsError):
                client.get_transaction("zzzz")


class TestEvents:
    def test_get_events_success(self, app):
        payload = {
            "events": [
                {
                    "type": "contract",
                    "ledger": 3727845,
                    "ledgerClosedAt": "2026-07-21T18:01:10Z",
                    "contractId": "CDLZFC3SYJYDZT7K67VZ75HPJVIEUVNIXF47ZG2FB2RMQQVU2HHGCYSC",
                    "id": "0016010972359577600-0000000001",
                    "txHash": HASH64,
                    "topic": ["AAAADwAAAAh0cmFuc2Zlcg==", "*"],
                    "value": "AAAACgA=",
                }
            ],
            "latestLedger": 3730843,
            "oldestLedger": 3609884,
            "cursor": "0016010972359577600-0000000008",
        }
        session = _FakeSession(_FakeResponse(200, _rpc_result(payload)))
        with app.app_context():
            client = SorobanRpcClient(session=session)
            result = client.get_events(
                start_ledger=199616,
                contract_ids=[DOCS_CONTRACT],
                limit=2,
            )
        assert result["events"][0]["contractId"].startswith("CDL")
        assert result["cursor"]
        request = json.loads(session.requested_body)
        assert request["params"]["startLedger"] == 199616
        assert request["params"]["pagination"]["limit"] == 2

    def test_invalid_contract_filter_rejected(self, app):
        with app.app_context():
            client = _client(app, _FakeResponse(200, _rpc_result({})))
            with pytest.raises(SorobanRpcInvalidParamsError):
                client.get_events(contract_ids=["not-a-contract"])

    def test_too_many_contract_filters_rejected(self, app):
        with app.app_context():
            client = _client(app, _FakeResponse(200, _rpc_result({})))
            with pytest.raises(SorobanRpcInvalidParamsError):
                client.get_events(contract_ids=[DOCS_CONTRACT] * 6)


class TestErrors:
    def test_http_404(self, app):
        with app.app_context():
            client = _client(app, _FakeResponse(404, {}))
            with pytest.raises(SorobanRpcResponseError):
                client.get_health()

    def test_http_500(self, app):
        with app.app_context():
            client = _client(app, _FakeResponse(500, {}))
            with pytest.raises(SorobanRpcError):
                client.get_health()

    def test_rpc_error_payload(self, app):
        payload = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "boom"}}
        with app.app_context():
            client = _client(app, _FakeResponse(200, payload))
            with pytest.raises(SorobanRpcResponseError) as exc:
                client.get_health()
            assert exc.value.code == -32000
            assert "boom" in str(exc.value)

    def test_malformed_json(self, app):
        with app.app_context():
            client = _client(app, _FakeResponse(200, raw=b"<html>not json</html>"))
            with pytest.raises(SorobanRpcUnavailableError):
                client.get_health()

    def test_non_jsonrpc_payload(self, app):
        with app.app_context():
            client = _client(app, _FakeResponse(200, raw=b'{"foo": 1}'))
            with pytest.raises(SorobanRpcUnavailableError):
                client.get_health()

    def test_id_mismatch(self, app):
        payload = {"jsonrpc": "2.0", "id": 999, "result": {}}
        with app.app_context():
            client = _client(app, _FakeResponse(200, payload))
            with pytest.raises(SorobanRpcUnavailableError):
                client.get_health()

    def test_result_not_object(self, app):
        with app.app_context():
            client = _client(app, _FakeResponse(200, _rpc_result([1, 2])))
            with pytest.raises(SorobanRpcUnavailableError):
                client.get_health()

    def test_redirect_refused(self, app):
        with app.app_context():
            client = _client(app, _FakeResponse(301, raw=b""))
            with pytest.raises(SorobanRpcUnavailableError):
                client.get_health()


class TestBoundsAndSSRF:
    def test_response_too_large(self, app):
        big = _FakeResponse(200, raw=b"x" * 100)
        with app.app_context():
            client = _client(app, big, max_response_bytes=50)
            with pytest.raises(SorobanRpcUnavailableError):
                client.get_health()

    def test_out_of_base_url_rejected(self, app):
        with app.app_context():
            client = SorobanRpcClient()
            with pytest.raises(NetworkError):
                client._check_url("https://evil.example.com/")

    def test_unsafe_endpoint_rejected(self, app):
        with app.app_context():
            client = SorobanRpcClient()
            # HTTP is not allowed for public networks.
            with pytest.raises(NetworkError):
                client._check_url("http://soroban-testnet.stellar.org/")

    def test_no_rpc_configured_for_custom(self, app):
        with app.app_context():
            client = SorobanRpcClient(network="custom", rpc_url="http://127.0.0.1:8000/rpc")
            client._config.rpc_url = None
            with pytest.raises(SorobanRpcUnavailableError):
                client.get_health()

    def test_private_host_resolution_rejected(self, app):
        def resolver(host):
            return [(2, 1, 6, "", ("10.0.0.5", 0))]

        session = _FakeSession(_FakeResponse(200, _rpc_result({"status": "healthy"})))
        with app.app_context():
            client = SorobanRpcClient(session=session, host_resolver=resolver)
            with pytest.raises(NetworkError):
                client.get_health()

    def test_public_host_resolution_allowed(self, app):
        def resolver(host):
            return [(2, 1, 6, "", ("93.184.216.34", 0))]

        session = _FakeSession(_FakeResponse(200, _rpc_result({"status": "healthy"})))
        with app.app_context():
            client = SorobanRpcClient(session=session, host_resolver=resolver)
            health = client.get_health()
        assert health["status"] == "healthy"

    def test_resolution_failure_tolerated(self, app):
        def resolver(host):
            raise OSError("dns down")

        session = _FakeSession(_FakeResponse(200, _rpc_result({"status": "healthy"})))
        with app.app_context():
            client = SorobanRpcClient(session=session, host_resolver=resolver)
            health = client.get_health()
        assert health["status"] == "healthy"


class TestFactory:
    def test_get_soroban_rpc_client(self, app):
        with app.app_context():
            client = get_soroban_rpc_client()
            assert isinstance(client, SorobanRpcClient)
