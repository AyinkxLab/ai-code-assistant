"""Tests for Stellar blockchain integration."""

import pytest

from app.services.stellar import (
    SOROBAN_CONTRACT_TYPES,
    STELLAR_PROPERTIES,
    STELLAR_SDKS,
    STELLAR_TOOLS,
    AccountError,
    AssetError,
    ContractError,
    NetworkConfig,
    NetworkError,
    StellarAccount,
    StellarAsset,
    StellarAssetType,
    StellarError,
    StellarNetwork,
    StellarNetworkMode,
)


class TestStellarNetwork:
    """Test StellarNetwork enum."""

    def test_network_values(self):
        """Test that all networks have correct values."""
        assert StellarNetwork.MAINNET.value == "mainnet"
        assert StellarNetwork.TESTNET.value == "testnet"
        assert StellarNetwork.FUTURENET.value == "futurenet"
        assert StellarNetwork.CUSTOM.value == "custom"

    def test_network_count(self):
        """Test that we have expected number of networks."""
        networks = list(StellarNetwork)
        assert len(networks) == 4


class TestStellarNetworkMode:
    """Test StellarNetworkMode enum."""

    def test_mode_values(self):
        """Test mode values."""
        assert StellarNetworkMode.DEVELOPMENT.value == "development"
        assert StellarNetworkMode.TESTING.value == "testing"
        assert StellarNetworkMode.PRODUCTION.value == "production"

    def test_mode_count(self):
        """Test number of modes."""
        modes = list(StellarNetworkMode)
        assert len(modes) == 3


class TestNetworkConfig:
    """Test NetworkConfig dataclass."""

    def test_create_custom_config(self):
        """Test creating custom network config."""
        config = NetworkConfig(
            network=StellarNetwork.CUSTOM,
            network_passphrase="Custom Network",
            horizon_url="http://localhost:8000",
        )
        assert config.network == StellarNetwork.CUSTOM
        assert config.network_passphrase == "Custom Network"
        assert config.horizon_url == "http://localhost:8000"
        assert config.mode == StellarNetworkMode.DEVELOPMENT

    def test_mainnet_preset(self):
        """Test mainnet preset configuration."""
        config = NetworkConfig.mainnet()
        assert config.network == StellarNetwork.MAINNET
        assert config.mode == StellarNetworkMode.PRODUCTION
        assert config.is_public is True
        assert "stellar.org" in config.horizon_url
        assert config.rpc_url is not None

    def test_testnet_preset(self):
        """Test testnet preset configuration."""
        config = NetworkConfig.testnet()
        assert config.network == StellarNetwork.TESTNET
        assert config.mode == StellarNetworkMode.TESTING
        assert config.is_public is True
        assert "testnet" in config.horizon_url
        assert config.rpc_url is not None

    def test_futurenet_preset(self):
        """Test futurenet preset configuration."""
        config = NetworkConfig.futurenet()
        assert config.network == StellarNetwork.FUTURENET
        assert config.mode == StellarNetworkMode.TESTING
        assert config.is_public is True
        assert "futurenet" in config.horizon_url

    def test_local_preset(self):
        """Test local development network."""
        config = NetworkConfig.local()
        assert config.network == StellarNetwork.CUSTOM
        assert config.mode == StellarNetworkMode.DEVELOPMENT
        assert config.is_public is False
        assert "localhost" in config.horizon_url

    def test_config_to_dict(self):
        """Test converting config to dictionary."""
        config = NetworkConfig.mainnet()
        result = config.to_dict()
        assert result["network"] == "mainnet"
        assert result["mode"] == "production"
        assert result["is_public"] is True
        assert "horizon_url" in result
        assert "rpc_url" in result


class TestStellarAssetType:
    """Test StellarAssetType enum."""

    def test_asset_types(self):
        """Test asset type values."""
        assert StellarAssetType.NATIVE.value == "native"
        assert StellarAssetType.STANDARD.value == "standard"
        assert StellarAssetType.LIQUIDITY_POOL_SHARE.value == "liquidity_pool_share"


class TestStellarAsset:
    """Test StellarAsset dataclass."""

    def test_create_native_asset(self):
        """Test creating native XLM asset."""
        asset = StellarAsset(code="XLM", type=StellarAssetType.NATIVE)
        assert asset.is_native() is True
        assert asset.code == "XLM"
        assert asset.issuer is None

    def test_create_standard_asset(self):
        """Test creating standard asset."""
        issuer = "GBUQWP3BOUZX34ULNQG23RQ6F4BWFJXUR3CEEVNQT4TS4VJJBTCYL444"
        asset = StellarAsset(
            code="USDC",
            issuer=issuer,
            type=StellarAssetType.STANDARD,
        )
        assert asset.is_native() is False
        assert asset.code == "USDC"
        assert asset.issuer == issuer

    def test_asset_repr_native(self):
        """Test native asset representation."""
        asset = StellarAsset(code="XLM", type=StellarAssetType.NATIVE)
        assert repr(asset) == "XLM"

    def test_asset_repr_standard(self):
        """Test standard asset representation."""
        issuer = "GBUQWP3BOUZX34ULNQG23RQ6F4BWFJXUR3CEEVNQT4TS4VJJBTCYL444"
        asset = StellarAsset(code="USDC", issuer=issuer)
        assert repr(asset) == f"USDC:{issuer}"

    def test_asset_repr_no_issuer(self):
        """Test asset without issuer."""
        asset = StellarAsset(code="FOO")
        assert repr(asset) == "FOO"


class TestStellarAccount:
    """Test StellarAccount dataclass."""

    def test_create_account(self):
        """Test creating a Stellar account."""
        public_key = "GBUQWP3BOUZX34ULNQG23RQ6F4BWFJXUR3CEEVNQT4TS4VJJBTCYL444"
        account = StellarAccount(
            public_key=public_key,
            sequence=1,
            balances=[StellarAsset(code="XLM", type=StellarAssetType.NATIVE)],
        )
        assert account.public_key == public_key
        assert account.sequence == 1
        assert len(account.balances) == 1
        assert account.flags == {}
        assert account.signers == []

    def test_account_has_trustline(self):
        """Test checking trustline for asset."""
        usdc_asset = StellarAsset(
            code="USDC",
            issuer="GBUQWP3BOUZX34ULNQG23RQ6F4BWFJXUR3CEEVNQT4TS4VJJBTCYL444",
        )
        account = StellarAccount(
            public_key="GBUQWP3BOUZX34ULNQG23RQ6F4BWFJXUR3CEEVNQT4TS4VJJBTCYL444",
            sequence=1,
            balances=[usdc_asset],
        )
        assert account.has_trustline(usdc_asset) is True

    def test_account_no_trustline(self):
        """Test account without trustline."""
        account = StellarAccount(
            public_key="GBUQWP3BOUZX34ULNQG23RQ6F4BWFJXUR3CEEVNQT4TS4VJJBTCYL444",
            sequence=1,
            balances=[],
        )
        unknown_asset = StellarAsset(code="UNKNOWN")
        assert account.has_trustline(unknown_asset) is False


class TestStellarExceptions:
    """Test Stellar exception hierarchy."""

    def test_exception_inheritance(self):
        """Test exception inheritance chain."""
        assert issubclass(NetworkError, StellarError)
        assert issubclass(AccountError, StellarError)
        assert issubclass(AssetError, StellarError)
        assert issubclass(ContractError, StellarError)

    def test_raise_network_error(self):
        """Test raising network error."""
        with pytest.raises(NetworkError):
            raise NetworkError("Connection failed")

    def test_raise_account_error(self):
        """Test raising account error."""
        with pytest.raises(AccountError):
            raise AccountError("Account not found")

    def test_raise_stellar_error(self):
        """Test catching base exception."""
        with pytest.raises(StellarError):
            raise ContractError("Contract error")


class TestStellarProperties:
    """Test Stellar blockchain properties."""

    def test_stellar_properties_exist(self):
        """Test that blockchain properties are defined."""
        assert "base_fee_stroops" in STELLAR_PROPERTIES
        assert "base_reserve_stroops" in STELLAR_PROPERTIES
        assert "transaction_timeout_seconds" in STELLAR_PROPERTIES
        assert "max_tx_size_bytes" in STELLAR_PROPERTIES

    def test_stellar_properties_values(self):
        """Test property values make sense."""
        assert STELLAR_PROPERTIES["base_fee_stroops"] == 100
        assert STELLAR_PROPERTIES["base_reserve_stroops"] == 500_000_000
        assert STELLAR_PROPERTIES["transaction_timeout_seconds"] > 0
        assert STELLAR_PROPERTIES["max_tx_size_bytes"] > 0


class TestSorobanContracts:
    """Test Soroban contract types."""

    def test_soroban_contract_types(self):
        """Test Soroban contract types are defined."""
        assert "payment" in SOROBAN_CONTRACT_TYPES
        assert "token" in SOROBAN_CONTRACT_TYPES
        assert "nft" in SOROBAN_CONTRACT_TYPES
        assert "defi" in SOROBAN_CONTRACT_TYPES
        assert "oracles" in SOROBAN_CONTRACT_TYPES
        assert "governance" in SOROBAN_CONTRACT_TYPES

    def test_contract_type_descriptions(self):
        """Test contract descriptions are non-empty."""
        for contract_type, description in SOROBAN_CONTRACT_TYPES.items():
            assert len(description) > 0
            assert isinstance(contract_type, str)
            assert isinstance(description, str)


class TestStellarSDKs:
    """Test Stellar SDK definitions."""

    def test_stellar_sdks_exist(self):
        """Test that SDK definitions exist."""
        assert len(STELLAR_SDKS) > 0
        assert "py-stellar-base" in STELLAR_SDKS
        assert "stellar-sdk" in STELLAR_SDKS

    def test_sdk_descriptions(self):
        """Test SDK descriptions."""
        for _sdk, description in STELLAR_SDKS.items():
            assert len(description) > 0
            assert "SDK" in description or "SDK" in description.lower()


class TestStellarTools:
    """Test Stellar tools definitions."""

    def test_stellar_tools_exist(self):
        """Test that tool definitions exist."""
        assert len(STELLAR_TOOLS) > 0
        assert "stellar-cli" in STELLAR_TOOLS
        assert "soroban" in STELLAR_TOOLS

    def test_tool_descriptions(self):
        """Test tool descriptions."""
        for _tool, description in STELLAR_TOOLS.items():
            assert len(description) > 0


class TestNetworkConfigIntegration:
    """Integration tests for network configurations."""

    def test_all_public_networks_have_urls(self):
        """Test that public networks have valid URLs."""
        for network_config in [
            NetworkConfig.mainnet(),
            NetworkConfig.testnet(),
            NetworkConfig.futurenet(),
        ]:
            assert network_config.is_public is True
            assert network_config.horizon_url.startswith("http")
            if network_config.rpc_url:
                assert network_config.rpc_url.startswith("http")

    def test_local_network_development_mode(self):
        """Test local network uses development mode."""
        config = NetworkConfig.local()
        assert config.mode == StellarNetworkMode.DEVELOPMENT
        assert config.is_public is False
