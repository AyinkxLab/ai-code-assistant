"""Stellar blockchain network integration and configuration.

Supports Stellar public networks (mainnet, testnet, futurenet) and custom
networks. Provides network detection, configuration, and metadata.

The :class:`StellarService` is the read-only integration layer used by the
application. All endpoints come from configuration (never from user or project
input) and outbound requests are SSRF-bounded: only http(s) is allowed, custom
networks may only target loopback hosts, and every request enforces a timeout
and a response-body cap.
"""

import ipaddress
import json
import socket
from dataclasses import dataclass
from enum import StrEnum

import requests
from flask import current_app, has_app_context

DEFAULT_TIMEOUT = 15
MAX_RESPONSE_BYTES = 2 * 1024 * 1024

# strkey base-32 alphabet used by Stellar addresses (RFC 4648 base32, which
# excludes 0, 1, 8, 9 but includes I and O — real addresses contain them).
_STRKEY_ALPHABET = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567")
# Loopback hosts allowed for custom/development networks.
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
_LOOPBACK_HOSTS |= {f"127.0.0.{i}" for i in range(1, 256)}

# Hostname suffixes that are unambiguous evidence of a private/intranet host
# (rejected for public networks without needing a DNS lookup).
_OBVIOUS_PRIVATE_HOST_SUFFIXES = (
    ".local",
    ".internal",
    ".lan",
    ".home",
    ".intranet",
    ".corp",
    ".localhost",
)
_OBVIOUS_PRIVATE_HOSTS = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}


def _host_is_private_literal(host: str) -> bool:
    """Return ``True`` when ``host`` is an IP literal that is not globally routable."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )


def _hostname_is_obviously_private(host: str) -> bool:
    """Return ``True`` when ``host`` is obviously a private/intranet hostname."""
    host = host.strip().lower()
    if host in _OBVIOUS_PRIVATE_HOSTS:
        return True
    return any(host.endswith(suffix) for suffix in _OBVIOUS_PRIVATE_HOST_SUFFIXES)


class StellarNetwork(StrEnum):
    """Supported Stellar networks."""

    MAINNET = "mainnet"
    TESTNET = "testnet"
    FUTURENET = "futurenet"
    CUSTOM = "custom"


class StellarNetworkMode(StrEnum):
    """Stellar development mode."""

    DEVELOPMENT = "development"  # Local/private development
    TESTING = "testing"  # Testnet
    PRODUCTION = "production"  # Mainnet


@dataclass
class NetworkConfig:
    """Stellar network configuration and connection details."""

    network: StellarNetwork
    network_passphrase: str
    horizon_url: str
    rpc_url: str | None = None  # For Soroban smart contracts
    mode: StellarNetworkMode = StellarNetworkMode.DEVELOPMENT
    is_public: bool = False
    chain_id: str | None = None

    @staticmethod
    def mainnet() -> "NetworkConfig":
        """Get Stellar mainnet configuration."""
        return NetworkConfig(
            network=StellarNetwork.MAINNET,
            network_passphrase="Public Global Stellar Network ; September 2015",
            horizon_url="https://horizon.stellar.org",
            rpc_url="https://soroban-mainnet.stellar.org",
            mode=StellarNetworkMode.PRODUCTION,
            is_public=True,
        )

    @staticmethod
    def testnet() -> "NetworkConfig":
        """Get Stellar testnet configuration."""
        return NetworkConfig(
            network=StellarNetwork.TESTNET,
            network_passphrase="Test SDF Network ; September 2015",
            horizon_url="https://horizon-testnet.stellar.org",
            rpc_url="https://soroban-testnet.stellar.org",
            mode=StellarNetworkMode.TESTING,
            is_public=True,
        )

    @staticmethod
    def futurenet() -> "NetworkConfig":
        """Get Stellar futurenet configuration."""
        return NetworkConfig(
            network=StellarNetwork.FUTURENET,
            network_passphrase="Test SDF Future Network ; October 2022",
            horizon_url="https://horizon-futurenet.stellar.org",
            rpc_url="https://soroban-futurenet.stellar.org",
            mode=StellarNetworkMode.TESTING,
            is_public=True,
        )

    @staticmethod
    def local() -> "NetworkConfig":
        """Get local development network configuration."""
        return NetworkConfig(
            network=StellarNetwork.CUSTOM,
            network_passphrase="Standalone Network ; February 2021",
            horizon_url="http://localhost:8000",
            rpc_url="http://localhost:8000/soroban/rpc",
            mode=StellarNetworkMode.DEVELOPMENT,
            is_public=False,
        )

    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "network": self.network.value,
            "network_passphrase": self.network_passphrase,
            "horizon_url": self.horizon_url,
            "rpc_url": self.rpc_url,
            "mode": self.mode.value,
            "is_public": self.is_public,
            "chain_id": self.chain_id,
        }


class StellarAssetType(StrEnum):
    """Stellar asset types."""

    NATIVE = "native"  # XLM (Lumens)
    STANDARD = "standard"  # Custom issued asset
    LIQUIDITY_POOL_SHARE = "liquidity_pool_share"


@dataclass
class StellarAsset:
    """Represents a Stellar asset on-chain."""

    code: str
    issuer: str | None = None  # None for native XLM
    type: StellarAssetType = StellarAssetType.STANDARD
    balance: str | None = None
    is_authorized: bool | None = None

    def is_native(self) -> bool:
        """Check if asset is native XLM."""
        return self.type == StellarAssetType.NATIVE or self.code == "XLM"

    def __repr__(self) -> str:
        """String representation."""
        if self.is_native():
            return "XLM"
        return f"{self.code}:{self.issuer}" if self.issuer else self.code


@dataclass
class StellarAccount:
    """Represents a Stellar account on-chain."""

    public_key: str
    sequence: int
    balances: list[StellarAsset]
    flags: dict = None
    signers: list[dict] = None
    data_entries: dict = None

    def __post_init__(self):
        """Normalize defaults."""
        if self.flags is None:
            self.flags = {}
        if self.signers is None:
            self.signers = []
        if self.data_entries is None:
            self.data_entries = {}

    def has_trustline(self, asset: StellarAsset) -> bool:
        """Check if account has trustline for asset."""
        for balance in self.balances:
            if balance.code == asset.code and balance.issuer == asset.issuer:
                return True
        return False


class StellarError(Exception):
    """Base exception for Stellar integration."""

    pass


class NetworkError(StellarError):
    """Network connection error."""

    pass


class AccountError(StellarError):
    """Account-related error."""

    pass


class AssetError(StellarError):
    """Asset-related error."""

    pass


class ContractError(StellarError):
    """Smart contract error."""

    pass


# Stellar blockchain properties
STELLAR_PROPERTIES = {
    "base_fee_stroops": 100,  # 0.00001 XLM
    "base_reserve_stroops": 500_000_000,  # 50 XLM per account
    "transaction_timeout_seconds": 3600,
    "max_tx_size_bytes": 1024 * 100,  # 100KB
}

# Common Soroban contract types
SOROBAN_CONTRACT_TYPES = {
    "payment": "Payment and transfer contracts",
    "token": "Stellar CAP46-6 Token contract",
    "nft": "NFT/DeFi contract",
    "defi": "DeFi protocol contract",
    "oracles": "Price oracle contract",
    "governance": "DAO/governance contract",
}

# Stellar development tools and SDKs
STELLAR_SDKS = {
    "py-stellar-base": "Python SDK",
    "py-soroban-env": "Soroban Python SDK",
    "stellar-sdk": "JavaScript SDK",
    "js-stellar-sdk": "JavaScript SDK (full name)",
    "go-stellar-base": "Go SDK",
    "stellar-go": "Go SDK (alternate)",
    "stellar-rs": "Rust SDK",
    "stellar-java-sdk": "Java SDK",
}

# Stellar frameworks and tools
STELLAR_TOOLS = {
    "stellar-cli": "Stellar CLI for development",
    "soroban": "Soroban smart contracts CLI",
    "horizon": "Stellar API server",
    "stellar-laboratory": "Web-based Stellar IDE",
    "friendbot": "Test network faucet API",
}


# ---------------------------------------------------------------------------
# Address validation
# ---------------------------------------------------------------------------


def validate_stellar_address(address: str) -> bool:
    """Return ``True`` if ``address`` looks like a Stellar account id (G...).

    Performs a strict structural check (exactly 56 chars, 'G' prefix, valid
    strkey alphabet). It is intentionally *structural*: full CRC/checksum
    validation requires the ``stellar-sdk`` package which is not a dependency
    today, so callers must not treat a ``True`` as proof the account exists.
    """
    if not isinstance(address, str) or len(address) != 56:
        return False
    if not address.startswith("G"):
        return False
    return all(char in _STRKEY_ALPHABET for char in address[1:])


# ---------------------------------------------------------------------------
# Endpoint safety
# ---------------------------------------------------------------------------


def _parse_host(url: str) -> tuple[str, str, str] | None:
    """Return ``(scheme, host, port)`` for ``url`` or ``None`` if malformed."""
    try:
        from urllib.parse import urlparse

        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    return parsed.scheme, (parsed.hostname or "").lower(), str(parsed.port or "")


def validate_endpoint_url(url: str, *, is_public: bool) -> bool:
    """Return ``True`` if ``url`` is safe to call for the given network type.

    Fail closed: http(s) only; public networks require https and must not point
    at a private/link-local/loopback IP literal or an obviously-private
    hostname; custom networks may only target loopback hosts (local
    stellar-core / soroban-rpc). DNS-based verification of public hostnames
    happens at request time (see ``StellarService``).
    """
    parsed = _parse_host(url)
    if parsed is None:
        return False
    scheme, host, _port = parsed
    if is_public:
        if scheme != "https":
            return False
        return not (_host_is_private_literal(host) or _hostname_is_obviously_private(host))
    return host in _LOOPBACK_HOSTS


# ---------------------------------------------------------------------------
# Configuration resolution
# ---------------------------------------------------------------------------

#: Preset configurations keyed by network name (used when no explicit URL set).
_NETWORK_PRESETS = {
    StellarNetwork.MAINNET.value: NetworkConfig.mainnet,
    StellarNetwork.TESTNET.value: NetworkConfig.testnet,
    StellarNetwork.FUTURENET.value: NetworkConfig.futurenet,
}


def resolve_network_config(
    network: str | None = None,
    horizon_url: str | None = None,
    rpc_url: str | None = None,
) -> NetworkConfig:
    """Resolve a :class:`NetworkConfig` from explicit values or presets.

    ``None`` values fall back to ``current_app.config`` and then to the
    built-in presets. Explicit URLs override the preset for the chosen network
    so operators can point at a local node without changing the network id.

    Raises:
        StellarError: If the network name is unknown or an explicit endpoint
            fails endpoint validation.
    """
    cfg = current_app.config if has_app_context() else {}

    network = network or cfg.get("STELLAR_NETWORK", StellarNetwork.TESTNET.value)
    network = network.lower().strip()

    preset = _NETWORK_PRESETS.get(network)
    if preset is None and network != StellarNetwork.CUSTOM.value:
        raise StellarError(f"Unknown Stellar network: {network}")

    config = preset() if preset is not None else NetworkConfig.local()

    explicit_horizon = horizon_url or (cfg.get("STELLAR_HORIZON_URL") or "").strip()
    explicit_rpc = rpc_url or (cfg.get("STELLAR_RPC_URL") or "").strip()

    if explicit_horizon:
        if not validate_endpoint_url(explicit_horizon, is_public=config.is_public):
            raise StellarError(f"Unsafe Horizon endpoint for network {network}: {explicit_horizon}")
        config.horizon_url = explicit_horizon
    if explicit_rpc:
        if not validate_endpoint_url(explicit_rpc, is_public=config.is_public):
            raise StellarError(f"Unsafe Soroban RPC endpoint for network {network}: {explicit_rpc}")
        config.rpc_url = explicit_rpc

    return config


# ---------------------------------------------------------------------------
# Read-only Stellar service
# ---------------------------------------------------------------------------


class StellarService:
    """Read-only integration with Stellar Horizon / Soroban RPC.

    The service never signs, sends, or funds transactions; it only reads
    public network data for accounts, transactions, ledgers, assets, and
    network info. All endpoint URLs come from configuration and are validated
    before any request is made (fail closed, SSRF-bounded): https-only for
    public networks, loopback-only for custom networks, requests restricted to
    the configured base URL, redirects refused, response bodies size-capped,
    and public hosts verified to resolve to globally routable addresses.
    """

    def __init__(
        self,
        *,
        network: str | None = None,
        horizon_url: str | None = None,
        rpc_url: str | None = None,
        timeout: int | None = None,
        max_response_bytes: int | None = None,
        session: requests.Session | None = None,
        host_resolver=None,
    ) -> None:
        """Initialize the service with optional explicit endpoint overrides.

        ``host_resolver`` is an injectable ``getaddrinfo``-compatible callable
        (``host -> list[(family, type, proto, canonname, sockaddr)]``) used to
        verify that a public-network host resolves to a globally routable
        address before a request is made. It defaults to ``socket.getaddrinfo``
        and is only exercised for public networks; tests may inject a fake.
        """
        if has_app_context():
            cfg = current_app.config
            network = network or cfg.get("STELLAR_NETWORK")
            horizon_url = horizon_url or cfg.get("STELLAR_HORIZON_URL")
            rpc_url = rpc_url or cfg.get("STELLAR_RPC_URL")
            timeout = timeout or cfg.get("STELLAR_REQUEST_TIMEOUT")
            max_response_bytes = max_response_bytes or cfg.get("STELLAR_MAX_RESPONSE_BYTES")
        self._config = resolve_network_config(network, horizon_url, rpc_url)
        self._timeout = timeout or DEFAULT_TIMEOUT
        self._max_bytes = max_response_bytes or MAX_RESPONSE_BYTES
        self._session = session or requests.Session()
        self._strict_host_validation = True
        if has_app_context():
            self._strict_host_validation = (
                current_app.config.get("STELLAR_STRICT_HOST_VALIDATION", True) is not False
            )
        self._resolver = host_resolver or (lambda host: socket.getaddrinfo(host, None))

    @property
    def config(self) -> NetworkConfig:
        """Resolved network configuration."""
        return self._config

    def get_network_info(self) -> dict:
        """Return metadata about the configured network (never contacts the node)."""
        return {
            **self._config.to_dict(),
            "timeout_seconds": self._timeout,
            "max_response_bytes": self._max_bytes,
        }

    def validate_address(self, address: str) -> bool:
        """Return ``True`` if ``address`` is structurally a Stellar account id."""
        return validate_stellar_address(address)

    def get_account(self, address: str) -> dict:
        """Fetch account details from Horizon.

        Args:
            address: Stellar account id (G...)

        Returns:
            Bounded account data (id, sequence, balances, subentry count).

        Raises:
            AccountError: If the address is invalid or the account does not exist.
            NetworkError: If the network call fails.
        """
        if not validate_stellar_address(address):
            raise AccountError(f"Invalid Stellar address: {address}")

        path = f"{self._config.horizon_url}/accounts/{address}"
        data = self._get_json(path)
        return {
            "account_id": data.get("account_id", address),
            "sequence": data.get("sequence"),
            "subentry_count": data.get("subentry_count"),
            "balances": [
                {
                    "asset_type": b.get("asset_type"),
                    "asset_code": b.get("asset_code"),
                    "asset_issuer": b.get("asset_issuer"),
                    "balance": b.get("balance"),
                }
                for b in (data.get("balances") or [])
            ][:50],
        }

    def get_transaction(self, transaction_hash: str) -> dict:
        """Fetch basic transaction details from Horizon.

        Args:
            transaction_hash: Stellar transaction hash (hex).

        Returns:
            Bounded transaction data (hash, ledger, account, created_at, memo).

        Raises:
            StellarError: If the hash is malformed.
            NetworkError: If the network call fails.
        """
        if not isinstance(transaction_hash, str) or not transaction_hash.strip():
            raise StellarError("A transaction hash is required.")
        transaction_hash = transaction_hash.strip().lower()
        valid_chars = all(c in "0123456789abcdef" for c in transaction_hash)
        if not valid_chars or not (32 <= len(transaction_hash) <= 64):
            raise StellarError(f"Malformed transaction hash: {transaction_hash}")

        path = f"{self._config.horizon_url}/transactions/{transaction_hash}"
        data = self._get_json(path)
        return {
            "hash": data.get("hash"),
            "ledger": data.get("ledger"),
            "created_at": data.get("created_at"),
            "successful": data.get("successful"),
            "source_account": data.get("source_account"),
            "memo": data.get("memo"),
        }

    def get_ledger(self, sequence: int) -> dict:
        """Fetch a single ledger by sequence number from Horizon (read-only).

        Args:
            sequence: Ledger sequence number (>= 1).

        Returns:
            Bounded ledger metadata (sequence, hash, header, timestamps, tx/op
            counts). The raw ``header`` field is omitted to keep responses
            small.

        Raises:
            StellarError: If ``sequence`` is not a positive integer.
            NetworkError: If the network call fails or the ledger is missing.
        """
        try:
            sequence = int(sequence)
        except (TypeError, ValueError) as exc:
            raise StellarError(f"Invalid ledger sequence: {sequence}") from exc
        if sequence <= 0:
            raise StellarError(f"Invalid ledger sequence: {sequence}")

        data = self._get_json(f"{self._config.horizon_url}/ledgers/{sequence}")
        return {
            "sequence": data.get("sequence"),
            "hash": data.get("hash"),
            "ledger_hash": data.get("ledger_hash"),
            "prev_hash": data.get("prev_hash"),
            "closed_at": data.get("closed_at"),
            "protocol_version": data.get("protocol_version"),
            "base_fee_in_stroops": data.get("base_fee_in_stroops"),
            "base_reserve_in_stroops": data.get("base_reserve_in_stroops"),
            "max_tx_set_size": data.get("max_tx_set_size"),
            "successful_transaction_count": data.get("successful_transaction_count"),
            "failed_transaction_count": data.get("failed_transaction_count"),
            "operation_count": data.get("operation_count"),
            "total_coins": data.get("total_coins"),
            "fee_pool": data.get("fee_pool"),
        }

    def get_assets(self, *, cursor: str | None = None, limit: int | None = None) -> dict:
        """Return a bounded list of issued assets from Horizon (read-only).

        Args:
            cursor: Horizon pagination cursor (opaque string).
            limit: Maximum records to return (capped at 100).

        Returns:
            ``{"records": [...], "next": cursor|None, "prev": cursor|None}``.
        """
        if limit is not None:
            try:
                limit = max(1, min(int(limit), 100))
            except (TypeError, ValueError) as exc:
                raise StellarError(f"Invalid asset limit: {limit}") from exc

        params = {}
        if cursor:
            params["cursor"] = str(cursor)
        if limit:
            params["limit"] = limit
        query = "?" + "&".join(f"{k}={v}" for k, v in params.items()) if params else ""
        data = self._get_json(f"{self._config.horizon_url}/assets{query}")
        records = (data.get("_embedded") or {}).get("records") or []
        bounded = [
            {
                "asset_type": record.get("asset_type"),
                "asset_code": record.get("asset_code"),
                "asset_issuer": record.get("asset_issuer"),
                "amount": record.get("amount"),
                "num_accounts": record.get("num_accounts"),
                "flags": record.get("flags"),
            }
            for record in records[:100]
        ]
        return {
            "records": bounded,
            "next": (data.get("_links") or {}).get("next", {}).get("href"),
            "prev": (data.get("_links") or {}).get("prev", {}).get("href"),
        }

    def get_account_transactions(self, address: str, limit: int = 20) -> dict:
        """Return a bounded list of an account's recent transactions from Horizon.

        Args:
            address: Stellar account id (G...).
            limit: Maximum transactions to return (capped at 100).

        Returns:
            ``{"records": [...], "next": cursor|None}`` where each record is a
            bounded transaction summary.

        Raises:
            AccountError: If the address is invalid or the account does not exist.
            NetworkError: If the network call fails.
        """
        if not validate_stellar_address(address):
            raise AccountError(f"Invalid Stellar address: {address}")
        try:
            limit = max(1, min(int(limit), 100))
        except (TypeError, ValueError) as exc:
            raise StellarError(f"Invalid transaction limit: {limit}") from exc

        data = self._get_json(
            f"{self._config.horizon_url}/accounts/{address}/transactions?limit={limit}"
        )
        records = (data.get("_embedded") or {}).get("records") or []
        bounded = [
            {
                "hash": record.get("hash"),
                "ledger": record.get("ledger"),
                "created_at": record.get("created_at"),
                "successful": record.get("successful"),
                "source_account": record.get("source_account"),
                "memo": record.get("memo"),
                "fee_charged": record.get("fee_charged"),
                "max_fee": record.get("max_fee"),
                "operation_count": record.get("operation_count"),
            }
            for record in records[:100]
        ]
        return {
            "records": bounded,
            "next": (data.get("_links") or {}).get("next", {}).get("href"),
        }

    # -- internal helpers -------------------------------------------------

    def _get_json(self, url: str) -> dict:
        """GET ``url`` and return the JSON body, bounded and SSRF-checked.

        Redirects are never followed (fail closed): a 3xx response is treated
        as an error so the service can never be redirected to a host that was
        not validated.
        """
        self._check_url(url)
        try:
            response = self._session.get(
                url,
                timeout=self._timeout,
                stream=True,
                allow_redirects=False,
                headers={"Accept": "application/json", "User-Agent": "ai-code-assistant"},
            )
        except requests.RequestException as exc:
            raise NetworkError(f"Stellar network request failed: {exc}") from exc

        with response:
            if response.status_code in (300, 301, 302, 303, 307, 308):
                raise NetworkError("Stellar network refused a redirect response (fail closed)")
            if response.status_code == 404:
                raise AccountError(f"Stellar resource not found: {url}")
            if response.status_code >= 400:
                raise NetworkError(f"Stellar network returned {response.status_code}")

            chunks = []
            size = 0
            for chunk in response.iter_content(4096):
                size += len(chunk)
                if size > self._max_bytes:
                    raise NetworkError("Stellar response exceeded size limit")
                chunks.append(chunk)
                if size >= self._max_bytes:
                    break
            body = b"".join(chunks)

        try:
            return json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise NetworkError("Stellar network returned invalid JSON") from exc

    def _check_url(self, url: str) -> None:
        """Fail closed unless ``url`` is allowed for the configured network."""
        allowed_base = self._config.horizon_url.rstrip("/")
        if not url.startswith(allowed_base + "/"):
            raise NetworkError(f"Refusing out-of-base Stellar request: {url}")
        if not validate_endpoint_url(url, is_public=self._config.is_public):
            raise NetworkError(f"Refusing unsafe Stellar endpoint: {url}")
        self._assert_public_host_resolution(url)

    def _assert_public_host_resolution(self, url: str) -> None:
        """Reject a public-network host that resolves to a private address.

        Best-effort DNS-level defense-in-depth against config pointing at a
        hostname that resolves inside the deployment (SSRF). Only runs for
        public networks; DNS failures are tolerated (the scheme, literal-IP and
        base-URL guards still apply) because operator-supplied public endpoints
        are already restricted.
        """
        if not self._config.is_public or not self._strict_host_validation:
            return
        host = _parse_host(url)[1] if _parse_host(url) else ""
        if not host or _host_is_private_literal(host) or _hostname_is_obviously_private(host):
            return
        try:
            resolved = self._resolver(host)
        except Exception:  # DNS unavailable — the structural guards still apply
            return
        for info in resolved:
            sockaddr = info[4] if isinstance(info, tuple) and len(info) > 4 else info
            ip = sockaddr[0] if isinstance(sockaddr, (tuple, list)) else None
            if ip and _host_is_private_literal(ip):
                raise NetworkError(f"Refusing private address for public network: {ip}")


def get_stellar_service() -> StellarService:
    """Return a :class:`StellarService` bound to the current app configuration."""
    return StellarService()
