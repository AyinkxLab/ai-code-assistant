"""Tests for the read-only Stellar network switcher (#176).

Covers API selection persistence + validation, service/RPC routing of the
selected network, and the guarantee that mainnet is never an implicit default
(it requires an explicit stored selection).
"""

import contextlib

import pytest
from flask_login import login_user

from app.services import ratelimit
from app.services.soroban_rpc import SorobanRpcClient
from app.services.stellar import StellarService

MAINNET_RPC = "https://soroban-mainnet.stellar.org"
TESTNET_RPC = "https://soroban-testnet.stellar.org"


@pytest.fixture(autouse=True)
def _reset_ratelimit():
    ratelimit.reset()
    yield
    ratelimit.reset()


def _network_payload(network="testnet"):
    return {"network": {"network": network}, "rpc_available": False}


@contextlib.contextmanager
def _authorized(app, user):
    """Run inside an authenticated request context for ``user``."""
    with app.test_request_context("/"):
        login_user(user)
        yield


def _store_network(db, user, network):
    user.stellar_network = network
    db.session.commit()
    return user


class TestNetworkSelectionApi:
    def test_requires_login(self, client):
        assert client.put("/stellar/api/network", json={"network": "testnet"}).status_code == 302
        assert client.get("/stellar/api/network").status_code == 302

    def test_default_selection_is_testnet(self, client, make_user, login, monkeypatch):
        make_user()
        login()
        monkeypatch.setattr("app.stellar.routes.network_status", lambda: _network_payload())
        response = client.get("/stellar/api/network")
        assert response.status_code == 200
        selection = response.get_json()["selection"]
        assert selection["default_network"] == "testnet"
        assert selection["stored_network"] is None
        assert selection["effective_network"] == "testnet"
        values = {item["value"] for item in selection["selectable"]}
        assert values == {"testnet", "mainnet", "futurenet", "custom"}
        # mainnet is offered but must never be the effective default here.
        assert selection["effective_network"] != "mainnet"

    def test_select_mainnet_persists(self, app, db, client, make_user, login, monkeypatch):
        user = make_user()
        login()
        response = client.put("/stellar/api/network", json={"network": "mainnet"})
        assert response.status_code == 200
        assert response.get_json()["stored_network"] == "mainnet"
        assert response.get_json()["effective_network"] == "mainnet"
        from app.models import User

        assert db.session.get(User, user.id).stellar_network == "mainnet"
        # Subsequent reads report the stored mainnet selection.
        monkeypatch.setattr(
            "app.stellar.routes.network_status", lambda: _network_payload("mainnet")
        )
        selection = client.get("/stellar/api/network").get_json()["selection"]
        assert selection["stored_network"] == "mainnet"
        assert selection["effective_network"] == "mainnet"

    def test_select_local_development(self, client, make_user, login):
        make_user()
        login()
        response = client.put("/stellar/api/network", json={"network": "custom"})
        assert response.status_code == 200
        assert response.get_json()["stored_network"] == "custom"
        assert response.get_json()["effective_network"] == "custom"

    def test_normalizes_case(self, client, make_user, login):
        make_user()
        login()
        response = client.put("/stellar/api/network", json={"network": "MAINNET"})
        assert response.status_code == 200
        assert response.get_json()["stored_network"] == "mainnet"

    def test_unknown_network_rejected(self, client, make_user, login):
        make_user()
        login()
        response = client.put("/stellar/api/network", json={"network": "somewhere"})
        assert response.status_code == 400
        assert "Unsupported Stellar network" in response.get_json()["error"]

    def test_raw_url_rejected(self, client, make_user, login):
        make_user()
        login()
        for url in ("https://evil.example.com", "http://127.0.0.1:8000", "mainnet.stellar.org"):
            response = client.put("/stellar/api/network", json={"network": url})
            assert response.status_code == 400, url

    def test_missing_network_rejected(self, client, make_user, login):
        make_user()
        login()
        assert client.put("/stellar/api/network", json={}).status_code == 400

    def test_selection_is_per_user(self, client, make_user, login, monkeypatch):
        make_user(username="alice", email="alice@example.com")
        login(email="alice@example.com")
        assert client.put("/stellar/api/network", json={"network": "mainnet"}).status_code == 200
        # A second user is unaffected.
        make_user(username="bob", email="bob@example.com")
        login(email="bob@example.com")
        monkeypatch.setattr("app.stellar.routes.network_status", lambda: _network_payload())
        selection = client.get("/stellar/api/network").get_json()["selection"]
        assert selection["stored_network"] is None
        assert selection["effective_network"] == "testnet"


class TestNetworkSelectionRouting:
    """The stored selection routes through services and RPC clients."""

    def test_service_uses_stored_mainnet(self, app, make_user, db):
        user = _store_network(
            db, make_user(username="router", email="router@example.com"), "mainnet"
        )
        with _authorized(app, user):
            service = StellarService()
        assert service.config.network.value == "mainnet"
        assert service.config.horizon_url == "https://horizon.stellar.org"
        assert service.config.rpc_url == MAINNET_RPC

    def test_rpc_client_uses_stored_mainnet(self, app, make_user, db):
        user = _store_network(
            db, make_user(username="router2", email="router2@example.com"), "mainnet"
        )
        with _authorized(app, user):
            client = SorobanRpcClient()
        assert client.config.network.value == "mainnet"
        assert client.config.rpc_url == MAINNET_RPC

    def test_service_uses_stored_futurenet(self, app, make_user, db):
        user = _store_network(
            db, make_user(username="router3", email="router3@example.com"), "futurenet"
        )
        with _authorized(app, user):
            service = StellarService()
        assert service.config.network.value == "futurenet"
        assert service.config.rpc_url == "https://soroban-futurenet.stellar.org"

    def test_defaults_to_testnet_without_selection(self, app, make_user):
        # mainnet must never be the implicit default for an authenticated user
        # with no explicit selection.
        user = make_user(username="defaultuser", email="default@example.com")
        with _authorized(app, user):
            service = StellarService()
            client = SorobanRpcClient()
        assert service.config.network.value == "testnet"
        assert client.config.rpc_url == TESTNET_RPC

    def test_anonymous_request_defaults_to_config(self, app):
        with app.test_request_context("/"):
            service = StellarService()
        assert service.config.network.value == "testnet"

    def test_explicit_network_overrides_stored(self, app, make_user, db):
        user = _store_network(db, make_user(username="x", email="x@example.com"), "mainnet")
        with _authorized(app, user):
            service = StellarService(network="futurenet")
        assert service.config.network.value == "futurenet"

    def test_invalid_stored_value_is_ignored(self, app, make_user, db):
        user = make_user(username="odd", email="odd@example.com")
        user.stellar_network = "https://evil.example.com"
        db.session.commit()
        with _authorized(app, user):
            service = StellarService()
        assert service.config.network.value == "testnet"

    def test_custom_selection_maps_to_loopback_local(self, app, make_user, db):
        user = _store_network(db, make_user(username="local", email="local@example.com"), "custom")
        with _authorized(app, user):
            service = StellarService()
        assert service.config.network.value == "custom"
        assert service.config.is_public is False
        assert service.config.rpc_url == "http://localhost:8000/soroban/rpc"
