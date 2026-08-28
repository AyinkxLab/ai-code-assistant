"""Tests for the Stellar network service: configuration, endpoints, and client."""

import json

import pytest

from app.services.stellar import (
    AccountError,
    NetworkConfig,
    NetworkError,
    StellarError,
    StellarNetwork,
    StellarService,
    get_stellar_service,
    resolve_network_config,
    validate_endpoint_url,
    validate_stellar_address,
)

VALID_ADDRESS = "G" + "A" * 55
VALID_ADDRESS_2 = "G" + "B" * 55


class _FakeResponse:
    def __init__(self, status_code, payload=None, raw=b"", headers=None):
        self.status_code = status_code
        self.headers = headers or {"Content-Type": "application/json"}
        self._raw = raw or (json.dumps(payload or {}).encode())
        self._bytes_read = False

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


class TestAddressValidation:
    def test_valid_structure(self):
        assert validate_stellar_address(VALID_ADDRESS) is True

    def test_too_short(self):
        assert validate_stellar_address("G" + "A" * 54) is False

    def test_too_long(self):
        assert validate_stellar_address("G" + "A" * 56) is False

    def test_wrong_prefix(self):
        assert validate_stellar_address("M" + "A" * 55) is False

    def test_invalid_alphabet(self):
        # '0', '1', '8', '9' are excluded from the strkey alphabet. (I and O
        # ARE valid: real addresses such as GALAXYVOIDAOPZTDLHILAJQKCVVFMD4IKL
        # XLSZV5YHO7VY74IWZILUTO contain them.)
        for bad in "0189":
            assert validate_stellar_address("G" + bad + "A" * 54) is False

    def test_valid_address_with_io(self):
        # A real Stellar address that contains 'I' and 'O'.
        assert (
            validate_stellar_address("GALAXYVOIDAOPZTDLHILAJQKCVVFMD4IKLXLSZV5YHO7VY74IWZILUTO")
            is True
        )

    def test_not_a_string(self):
        assert validate_stellar_address(12345) is False


class TestNetworkConfigResolution:
    def test_testnet_default(self, app):
        with app.app_context():
            config = resolve_network_config()
            assert config.network == StellarNetwork.TESTNET
            assert "testnet" in config.horizon_url

    def test_explicit_network(self, app):
        with app.app_context():
            config = resolve_network_config(network="mainnet")
            assert config.network == StellarNetwork.MAINNET
            assert config.mode.value == "production"

    def test_unknown_network_rejected(self):
        with pytest.raises(StellarError):
            resolve_network_config(network="not-a-network")

    def test_explicit_horizon_override(self, app):
        with app.app_context():
            config = resolve_network_config(
                network="testnet", horizon_url="https://horizon.example"
            )
            assert config.horizon_url == "https://horizon.example"

    def test_unsafe_public_horizon_rejected(self, app):
        # HTTP is never allowed for public networks.
        with app.app_context(), pytest.raises(StellarError):
            resolve_network_config(network="mainnet", horizon_url="http://horizon.stellar.org")

    def test_custom_network_allows_loopback(self):
        config = resolve_network_config(network="custom", horizon_url="http://127.0.0.1:8000")
        assert config.horizon_url == "http://127.0.0.1:8000"

    def test_custom_network_rejects_public_host(self):
        with pytest.raises(StellarError):
            resolve_network_config(network="custom", horizon_url="http://evil.example.com")

    def test_network_info(self, app):
        with app.app_context():
            service = StellarService()
            info = service.get_network_info()
            assert info["network"] == "testnet"
            assert "horizon_url" in info
            assert "rpc_url" in info
            assert info["timeout_seconds"] > 0


class TestEndpointValidation:
    def test_https_public(self):
        assert validate_endpoint_url("https://horizon.stellar.org", is_public=True) is True

    def test_http_public_rejected(self):
        assert validate_endpoint_url("http://horizon.stellar.org", is_public=True) is False

    def test_ftp_rejected(self):
        assert validate_endpoint_url("ftp://horizon.stellar.org", is_public=True) is False

    def test_loopback_custom(self):
        assert validate_endpoint_url("http://localhost:8000", is_public=False) is True
        assert validate_endpoint_url("http://127.0.0.1:8000", is_public=False) is True

    def test_public_host_custom_rejected(self):
        assert validate_endpoint_url("http://horizon.stellar.org", is_public=False) is False


class TestStellarServiceClient:
    def test_get_account_success(self, app):
        payload = {
            "account_id": VALID_ADDRESS,
            "sequence": "12345",
            "subentry_count": 1,
            "balances": [{"asset_type": "native", "balance": "100.0000000"}],
        }
        session = _FakeSession(_FakeResponse(200, payload))
        with app.app_context():
            service = StellarService(session=session)
            account = service.get_account(VALID_ADDRESS)
            assert account["account_id"] == VALID_ADDRESS
            assert account["sequence"] == "12345"
            assert account["balances"][0]["balance"] == "100.0000000"
            assert service.config.horizon_url in session.requested_url

    def test_get_account_invalid_address(self, app):
        with app.app_context():
            service = StellarService()
            with pytest.raises(AccountError):
                service.get_account("invalid")

    def test_get_account_not_found(self, app):
        session = _FakeSession(_FakeResponse(404))
        with app.app_context():
            service = StellarService(session=session)
            with pytest.raises(AccountError):
                service.get_account(VALID_ADDRESS)

    def test_get_account_network_error(self, app):
        session = _FakeSession(_FakeResponse(502))
        with app.app_context():
            service = StellarService(session=session)
            with pytest.raises(NetworkError):
                service.get_account(VALID_ADDRESS)

    def test_get_transaction_success(self, app):
        payload = {
            "hash": "a" * 64,
            "ledger": 100,
            "created_at": "2024-01-01T00:00:00Z",
            "successful": True,
            "source_account": VALID_ADDRESS,
            "memo": "",
        }
        session = _FakeSession(_FakeResponse(200, payload))
        with app.app_context():
            service = StellarService(session=session)
            tx = service.get_transaction("A" * 64)
            assert tx["hash"] == "a" * 64
            assert tx["successful"] is True

    def test_get_transaction_malformed_hash(self, app):
        with app.app_context():
            service = StellarService()
            with pytest.raises(StellarError):
                service.get_transaction("zzzz")

    def test_ssrf_out_of_base_rejected(self, app):
        session = _FakeSession(_FakeResponse(200, {}))
        with app.app_context():
            service = StellarService(session=session)
            with pytest.raises(NetworkError):
                service._get_json("https://evil.example.com/accounts/x")

    def test_response_too_large_rejected(self, app):
        big = _FakeResponse(200, raw=b"x" * 100)
        with app.app_context():
            service = StellarService(session=session_factory(big), max_response_bytes=50)
            with pytest.raises(NetworkError):
                service._get_json(service.config.horizon_url + "/accounts/" + VALID_ADDRESS)


def session_factory(response):
    return _FakeSession(response)


class TestGetStellarService:
    def test_returns_service(self, app):
        with app.app_context():
            service = get_stellar_service()
            assert isinstance(service, StellarService)


class TestPresetConfigs:
    def test_all_presets_valid(self):
        for preset in (NetworkConfig.mainnet, NetworkConfig.testnet, NetworkConfig.futurenet):
            config = preset()
            assert config.horizon_url.startswith("https")
            assert config.rpc_url is None or config.rpc_url.startswith("https")
