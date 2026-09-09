"""Adversarial SSRF/redirect/host-validation tests for the Stellar service.

These tests assert fail-closed behavior for the guards implemented in
``app/services/stellar.py`` (and reused by the Soroban RPC client): endpoint
validation, redirect refusal, private-IP rejection, host resolution, size
caps, and timeouts. No real network access is used (timeout tests use a local
loopback server that never answers).

DNS bypass limitation: only the guard logic is testable offline. A public
hostname that an attacker controls to resolve to a private address can only be
caught at request time by the injected ``host_resolver``; we cannot prove real
DNS behavior without a network, so those tests inject a resolver and document
that the request-time DNS check is the boundary (see
``TestRequestTimeHostResolution`` and ``docs/security.md``).
"""

import contextlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.services.soroban_rpc import SorobanRpcClient, SorobanRpcUnavailableError
from app.services.stellar import (
    AccountError,
    NetworkError,
    StellarError,
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


class TestExoticHostForms:
    """Mixed-case, trailing-dot, and encoded host forms (documented behavior).

    Behavior: hosts are normalized by ``_parse_host`` — scheme/host lowercased,
    one trailing dot stripped — before every safety check. Percent-encoded and
    otherwise unsafe hosts are treated as malformed (rejected), so an encoded
    private IP can never bypass the literal checks.
    """

    def test_mixed_case_host_allowed_and_normalized(self):
        assert validate_endpoint_url("https://HORIZON.STELLAR.ORG", is_public=True) is True
        assert validate_endpoint_url("HTTPS://horizon.stellar.org", is_public=True) is True

    def test_trailing_dot_public_host_allowed(self):
        assert validate_endpoint_url("https://horizon.stellar.org./", is_public=True) is True

    def test_trailing_dot_private_literals_rejected(self):
        assert validate_endpoint_url("https://127.0.0.1./", is_public=True) is False
        assert validate_endpoint_url("https://169.254.169.254./", is_public=True) is False
        assert validate_endpoint_url("https://10.0.0.1./", is_public=True) is False

    def test_trailing_dot_loopback_allowed_for_custom(self):
        # Normalization collapses localhost. -> localhost (a loopback host).
        assert validate_endpoint_url("http://localhost.:8000", is_public=False) is True
        assert validate_endpoint_url("http://127.0.0.1.:8000", is_public=False) is True

    def test_percent_encoded_private_host_rejected(self):
        # %31%32%37.0.0.1 decodes to 127.0.0.1; encoded hosts are rejected.
        assert validate_endpoint_url("https://%31%32%37.0.0.1/", is_public=True) is False
        assert (
            validate_endpoint_url("http://%31%32%37%2e%30%2e%30%2e%31:8000/", is_public=False)
            is False
        )

    def test_userinfo_cannot_hide_host(self):
        assert validate_endpoint_url("https://attacker@127.0.0.1/", is_public=True) is False

    def test_parse_host_normalizes(self):
        from app.services.stellar import _parse_host

        assert _parse_host("HTTPS://HORIZON.STELLAR.ORG./x") == (
            "https",
            "horizon.stellar.org",
            "",
        )
        assert _parse_host("https://127.0.0.1./") == ("https", "127.0.0.1", "")
        assert _parse_host("https://%31%32%37.0.0.1/") is None
        assert _parse_host("https://exa mple.org/") is None


class TestRequestTimeDNSNormalization:
    def test_resolver_receives_normalized_host(self, app):
        """The request-time DNS check sees the canonical (dotted-root-stripped) host."""
        seen = []

        def resolver(host):
            seen.append(host)
            return [(2, 1, 6, "", ("93.184.216.34", 0))]

        session = _FakeSession(_FakeResponse(200, {"status": "ok"}))
        with app.app_context():
            service = StellarService(
                session=session,
                host_resolver=resolver,
                horizon_url="https://horizon.stellar.org./",
            )
            service._get_json(service.config.horizon_url + "/accounts/" + VALID_ADDRESS)
        assert seen == ["horizon.stellar.org"]

    def test_encoded_private_host_rejected_at_config_time(self, app):
        with app.app_context(), pytest.raises(StellarError):
            StellarService(horizon_url="https://%31%32%37.0.0.1/")

    def test_trailing_dot_private_host_rejected_at_config_time(self, app):
        with app.app_context(), pytest.raises(StellarError):
            StellarService(horizon_url="https://127.0.0.1./")


class _NeverRespondsHandler(BaseHTTPRequestHandler):
    """Accepts a request and never sends a response (used for timeout tests)."""

    protocol_version = "HTTP/1.0"

    def _stall(self):
        with contextlib.suppress(Exception):
            time.sleep(30)

    def do_GET(self):
        self._stall()

    def do_POST(self):
        self._stall()

    def log_message(self, *args):  # pragma: no cover - quiet
        return

    def handle_error(self, request, client_address):  # pragma: no cover - quiet
        return


class _SlowServer:
    """A loopback HTTP server whose handlers never respond."""

    def __init__(self):
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _NeverRespondsHandler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        host, port = self._httpd.server_address[:2]
        self.base_url = f"http://{host}:{port}"

    def close(self):
        self._httpd.shutdown()
        self._httpd.server_close()


class TestTimeoutAbort:
    """A never-finishing response aborts within the configured timeout."""

    def test_horizon_times_out(self, app):
        server = _SlowServer()
        try:
            with app.app_context():
                service = StellarService(
                    network="custom",
                    horizon_url=server.base_url,
                    timeout=0.3,
                    max_response_bytes=1024,
                )
                started = time.monotonic()
                with pytest.raises(NetworkError):
                    service.get_account(VALID_ADDRESS)
                assert time.monotonic() - started < 5  # aborted, did not hang
        finally:
            server.close()

    def test_rpc_times_out(self, app):
        server = _SlowServer()
        try:
            with app.app_context():
                client = SorobanRpcClient(
                    network="custom",
                    rpc_url=server.base_url + "/rpc",
                    timeout=0.3,
                    max_response_bytes=1024,
                )
                started = time.monotonic()
                with pytest.raises(SorobanRpcUnavailableError):
                    client.get_health()
                assert time.monotonic() - started < 5  # aborted, did not hang
        finally:
            server.close()
