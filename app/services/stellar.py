"""Stellar blockchain network integration and configuration.

Supports Stellar public networks (mainnet, testnet, futurenet) and custom
networks. Provides network detection, configuration, and metadata.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class StellarNetwork(str, Enum):
    """Supported Stellar networks."""

    MAINNET = "mainnet"
    TESTNET = "testnet"
    FUTURENET = "futurenet"
    CUSTOM = "custom"


class StellarNetworkMode(str, Enum):
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
    rpc_url: Optional[str] = None  # For Soroban smart contracts
    mode: StellarNetworkMode = StellarNetworkMode.DEVELOPMENT
    is_public: bool = False
    chain_id: Optional[str] = None

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


class StellarAssetType(str, Enum):
    """Stellar asset types."""

    NATIVE = "native"  # XLM (Lumens)
    STANDARD = "standard"  # Custom issued asset
    LIQUIDITY_POOL_SHARE = "liquidity_pool_share"


@dataclass
class StellarAsset:
    """Represents a Stellar asset on-chain."""

    code: str
    issuer: Optional[str] = None  # None for native XLM
    type: StellarAssetType = StellarAssetType.STANDARD
    balance: Optional[str] = None
    is_authorized: Optional[bool] = None

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
            if (
                balance.code == asset.code
                and balance.issuer == asset.issuer
            ):
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
