"""Tests for read-only Stellar/Soroban config generation (#183).

Covers per-network generation, structure validation, the no-secret guarantee,
the no-overwrite behavior, and the detection round trip.
"""

import tomllib
from types import SimpleNamespace

import pytest

from app.services.stellar_config import (
    CONFIG_PATH,
    StellarConfigError,
    generate_config_file,
    generate_soroban_config,
    supported_config_networks,
    validate_generated_config,
)
from app.services.stellar_detection import detect_stellar_project


class TestGeneration:
    @pytest.mark.parametrize(
        "network,section,passphrase",
        [
            ("testnet", "testnet", "Test SDF Network ; September 2015"),
            ("mainnet", "mainnet", "Public Global Stellar Network ; September 2015"),
            ("futurenet", "futurenet", "Test SDF Future Network ; October 2022"),
            ("custom", "local", "Standalone Network ; February 2021"),
        ],
    )
    def test_generates_per_network(self, network, section, passphrase):
        data = tomllib.loads(generate_soroban_config(network))
        assert data["network"][section]["network_passphrase"] == passphrase

    def test_testnet_includes_validated_rpc(self):
        table = validate_generated_config(generate_soroban_config("testnet"), "testnet")
        assert table["rpc_url"] == "https://soroban-testnet.stellar.org"

    def test_unknown_network_rejected(self):
        with pytest.raises(StellarConfigError):
            generate_soroban_config("bogus")

    def test_supported_networks(self):
        assert set(supported_config_networks()) == {"testnet", "mainnet", "futurenet", "custom"}


class TestValidation:
    def test_round_trips_through_detection(self):
        text = generate_soroban_config("testnet")
        signals = detect_stellar_project([SimpleNamespace(path=CONFIG_PATH, content=text)])
        assert signals.soroban_config_dir is True
        assert "testnet" in signals.network_hints

    def test_rejects_non_toml(self):
        with pytest.raises(StellarConfigError):
            validate_generated_config("this is = = not toml", "testnet")

    def test_rejects_wrong_passphrase(self):
        text = '[network.testnet]\nnetwork_passphrase = "nope"\n'
        with pytest.raises(StellarConfigError):
            validate_generated_config(text, "testnet")

    def test_rejects_secret_key(self):
        text = (
            "[network.testnet]\n"
            'network_passphrase = "Test SDF Network ; September 2015"\n'
            'secret_key = "S' + "A" * 55 + '"\n'
        )
        with pytest.raises(StellarConfigError):
            validate_generated_config(text, "testnet")

    def test_rejects_seed_value_without_secret_key_name(self):
        text = (
            "[network.testnet]\n"
            'network_passphrase = "Test SDF Network ; September 2015"\n'
            'note = "S' + "A" * 55 + '"\n'
        )
        with pytest.raises(StellarConfigError):
            validate_generated_config(text, "testnet")


class TestNoSecrets:
    @pytest.mark.parametrize("network", ["testnet", "mainnet", "futurenet", "custom"])
    def test_generated_output_has_no_secret_assignments(self, network):
        text = generate_soroban_config(network)
        lowered = text.lower()
        for bad in ("private_key", "secret_key", "seed =", "mnemonic", "password =", "token ="):
            assert bad not in lowered
        assert "S" + "A" * 55 not in text
        # The document is internally validated for secret-looking keys/values.
        validate_generated_config(text, network)


class TestNoOverwrite:
    def test_refuses_existing_target(self):
        with pytest.raises(StellarConfigError):
            generate_config_file("testnet", existing_paths=[CONFIG_PATH])

    def test_allows_existing_target_with_overwrite(self):
        row = generate_config_file("testnet", existing_paths=[CONFIG_PATH], overwrite=True)
        assert row["path"] == CONFIG_PATH

    def test_ignores_unrelated_existing_paths(self):
        row = generate_config_file("testnet", existing_paths=["README.md", "src/lib.rs"])
        assert row["path"] == CONFIG_PATH

    def test_accepts_existing_path_objects(self):
        files = [SimpleNamespace(path=CONFIG_PATH)]
        with pytest.raises(StellarConfigError):
            generate_config_file("testnet", existing_paths=files)

    def test_row_shape(self):
        row = generate_config_file("testnet")
        assert row["is_binary"] is False
        assert row["language"] == "toml"
        assert row["size"] == len(row["content"].encode("utf-8"))
        validate_generated_config(row["content"], "testnet")
