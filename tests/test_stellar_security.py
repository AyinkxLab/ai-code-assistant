"""Adversarial SSRF/redirect/host-validation tests for the Stellar service.

These tests assert fail-closed behavior for the guards implemented in
``app/services/stellar.py`` (and reused by the Soroban RPC client): endpoint
validation, redirect refusal, private-IP rejection, host resolution, size
caps, and timeouts. No real network access is used.
"""

import json

import pytest

from app.services.soroban_rpc import SorobanRpcClient, SorobanRpcUnavailableError
from app.services.stellar import (
    AccountError,
    NetworkError,
    StellarService,
    validate_endpoint_url,
)

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
        self.allow_redirects = None

    def get(self, url, timeout=None, stream=None, headers=None, allow_redirects=False):
        self.allow_redirects = allow_redirects
        return self.response


class TestPublicEndpointAdversarial:
    def test_private_ipv4_literals_rejected(self):
        for host in ("10.0.0.1", "172.16.0.1", "172.31.255.1", "192.168.1.1"):
            assert validate_endpoint_url(f"https://{host}/", is_public=True) is False

    def test_loopback_ipv4_rejected(self):
        assert validate_endpoint_url("https://127.0.0.1/", is_public=True) is False
        assert validate_endpoint_url("https://127.0.0.5/", is_public=True) is False

    def test_link_local_rejected(self):
        assert validate_endpoint_url("https://169.254.169.254/", is_public=True) is False

    def test_reserved_and_multicast_rejected(self):
        assert validate_endpoint_url("https://224.0.0.1/", is_public=True) is False
        assert validate_endpoint_url("https://240.0.0.1/", is_public=True) is False
        assert validate_endpoint_url("https://198.51.100.7/", is_public=True) is False

    def test_unspecified_rejected(self):
        assert validate_endpoint_url("https://0.0.0.0/", is_public=True) is False

    def test_ipv6_private_rejected(self):
        assert validate_endpoint_url("https://[::1]/", is_public=True) is False
        assert validate_endpoint_url("https://[fe80::1]/", is_public=True) is False
        assert validate_endpoint_url("https://[fc00::1]/", is_public=True) is False

    def test_obvious_private_hostnames_rejected(self):
        for host in ("localhost", "myhost.local", "intranet.internal", "x.lan"):
            assert validate_endpoint_url(f"https://{host}/", is_public=True) is False

    def test_public_hostname_allowed(self):
        assert validate_endpoint_url("https://horizon.stellar.org", is_public=True) is True
        assert validate_endpoint_url("https://soroban-testnet.stellar.org", is_public=True) is True

    def test_custom_loopback_allowed(self):
        assert validate_endpoint_url("http://127.0.0.1:8000", is_public=False) is True
        assert validate_endpoint_url("http://[::1]:8000", is_public=False) is True

    def test_invalid_schemes_rejected(self):
        assert validate_endpoint_url("ftp://horizon.stellar.org", is_public=True) is False
        assert validate_endpoint_url("file:///etc/passwd", is_public=True) is False


class TestRedirectRefusal:
    def test_horizon_redirect_refused(self, app):
        session = _FakeSession(_FakeResponse(302))
        with app.app_context():
            service = StellarService(session=session)
            with pytest.raises(NetworkError):
                service._get_json(service.config.horizon_url + "/accounts/" + VALID_ADDRESS)
            assert session.allow_redirects is False

    def test_rpc_redirect_refused(self, app):
        class _PostSession(_FakeSession):
            def post(
                self, url, data=None, timeout=None, stream=None, headers=None, allow_redirects=False
            ):
                self.allow_redirects = allow_redirects
                return self.response

        session = _PostSession(_FakeResponse(301, raw=b""))
        with app.app_context():
            client = SorobanRpcClient(session=session)
            with pytest.raises(SorobanRpcUnavailableError):
                client.get_health()


class TestRequestTimeHostResolution:
    def test_private_resolution_rejected(self, app):
        def resolver(host):
            return [(2, 1, 6, "", ("10.0.0.5", 0))]

        session = _FakeSession(_FakeResponse(200, {"status": "ok"}))
        with app.app_context():
            service = StellarService(session=session, host_resolver=resolver)
            with pytest.raises(NetworkError):
                service._get_json(service.config.horizon_url + "/accounts/" + VALID_ADDRESS)

    def test_public_resolution_allowed(self, app):
        def resolver(host):
            return [(2, 1, 6, "", ("93.184.216.34", 0))]

        session = _FakeSession(_FakeResponse(200, {"account_id": VALID_ADDRESS}))
        with app.app_context():
            service = StellarService(session=session, host_resolver=resolver)
            account = service.get_account(VALID_ADDRESS)
        assert account["account_id"] == VALID_ADDRESS

    def test_resolution_failure_tolerated(self, app):
        def resolver(host):
            raise OSError("dns failure")

        session = _FakeSession(_FakeResponse(200, {"account_id": VALID_ADDRESS}))
        with app.app_context():
            service = StellarService(session=session, host_resolver=resolver)
            account = service.get_account(VALID_ADDRESS)
        assert account["account_id"] == VALID_ADDRESS

    def test_strict_validation_disabled_skips_resolution(self, app):
        def resolver(host):
            raise AssertionError("resolver should not run")

        session = _FakeSession(_FakeResponse(200, {"account_id": VALID_ADDRESS}))
        with app.app_context():
            app.config["STELLAR_STRICT_HOST_VALIDATION"] = False
            service = StellarService(session=session, host_resolver=resolver)
            account = service.get_account(VALID_ADDRESS)
        assert account["account_id"] == VALID_ADDRESS


class TestTransportBounds:
    def test_404_maps_to_account_error(self, app):
        session = _FakeSession(_FakeResponse(404))
        with app.app_context():
            service = StellarService(session=session)
            with pytest.raises(AccountError):
                service.get_account(VALID_ADDRESS)

    def test_oversized_response_rejected(self, app):
        big = _FakeResponse(200, raw=b"x" * 100)
        with app.app_context():
            service = StellarService(session=_FakeSession(big), max_response_bytes=50)
            with pytest.raises(NetworkError):
                service._get_json(service.config.horizon_url + "/accounts/" + VALID_ADDRESS)

    def test_malformed_json_rejected(self, app):
        bad = _FakeResponse(200, raw=b"<not json>")
        with app.app_context():
            service = StellarService(session=_FakeSession(bad))
            with pytest.raises(NetworkError):
                service._get_json(service.config.horizon_url + "/accounts/" + VALID_ADDRESS)
