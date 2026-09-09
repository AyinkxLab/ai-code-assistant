"""Tests for the extended Stellar/Soroban detection signals."""

from types import SimpleNamespace

import pytest

from app.services.stellar_detection import (
    detect_stellar_network,
    detect_stellar_project,
    project_stellar_metadata,
)


def _file(path, content=None):
    return SimpleNamespace(path=path, content=content)


class TestNewSignals:
    def test_plain_rust_still_not_stellar(self):
        files = [
            _file("Cargo.toml", "[dependencies]\nserde = '1.0'\n"),
            _file("src/main.rs", "fn main() {}"),
        ]
        signals = detect_stellar_project(files)
        assert signals.is_stellar is False
        assert signals.confidence == "none"

    def test_cli_tooling_in_makefile_possible(self):
        files = [_file("Makefile", "build:\n\tsoroban contract build --package counter\n")]
        signals = detect_stellar_project(files)
        assert signals.confidence == "possible"
        assert signals.stellar_cli_tooling is True

    def test_ci_workflow_soroban_possible(self):
        files = [_file(".github/workflows/build.yml", "run: stellar contract build\n")]
        signals = detect_stellar_project(files)
        assert signals.confidence == "possible"
        assert signals.stellar_cli_tooling is True

    def test_mention_of_stellar_in_workflow_is_not_enough(self):
        files = [_file(".github/workflows/build.yml", "name: stellar stuff\n")]
        signals = detect_stellar_project(files)
        assert signals.stellar_cli_tooling is False

    def test_soroban_crate_still_likely(self):
        files = [_file("Cargo.toml", "[dependencies]\nsoroban-sdk = '21.0.0'\n")]
        signals = detect_stellar_project(files)
        assert signals.confidence == "likely"
        assert signals.is_soroban is True

    def test_relevant_files_collected(self):
        files = [
            _file("Cargo.toml", "[dependencies]\nsoroban-sdk = '21.0.0'\n"),
            _file(
                "stellar.toml",
                '[NETWORK_TESTNET]\npassphrase = "Test SDF Network ; September 2015"\n',
            ),
            _file("src/main.rs", "fn main() {}"),
        ]
        signals = detect_stellar_project(files)
        assert "Cargo.toml" in signals.relevant_files
        assert "stellar.toml" in signals.relevant_files
        assert "src/main.rs" not in signals.relevant_files


class TestExtendedCliToolingDetection:
    @pytest.mark.parametrize(
        ("path", "content"),
        [
            ("scripts/build.sh", "soroban contract build --package counter\n"),
            ("scripts/deploy.sh", "stellar contract deploy --wasm foo.wasm\n"),
            (".gitlab-ci.yml", "- soroban contract build\n"),
            (".circleci/config.yml", "run: stellar xdr type XDR_DECODE\n"),
            (".circleci/config.yaml", "run: soroban invoke --id C...\n"),
            (".travis.yml", "script: stellar keys generate\n"),
        ],
    )
    def test_cli_commands_in_new_surfaces_detected(self, path, content):
        signals = detect_stellar_project([_file(path, content)])
        assert signals.confidence == "possible"
        assert signals.stellar_cli_tooling is True
        assert path in signals.relevant_files
        assert signals.evidence

    @pytest.mark.parametrize(
        ("path", "content"),
        [
            ("scripts/build.sh", "echo 'stellar things'\n"),
            ("scripts/run.sh", "# soroban is mentioned in a comment\n"),
            (".gitlab-ci.yml", "description: stellar project\n"),
            (".circleci/config.yml", "name: soroban pipeline\n"),
            (".travis.yml", "notifications: stellar\n"),
        ],
    )
    def test_keyword_mentions_do_not_trigger(self, path, content):
        signals = detect_stellar_project([_file(path, content)])
        assert signals.stellar_cli_tooling is False
        assert signals.is_stellar is False
        assert signals.confidence == "none"

    def test_plain_rust_with_stellar_readme_is_none(self):
        files = [
            _file("Cargo.toml", "[dependencies]\nserde = '1.0'\n"),
            _file("src/main.rs", "fn main() {}\n"),
            _file("README.md", "# Counter\nBuilt for the Stellar network.\n"),
        ]
        signals = detect_stellar_project(files)
        assert signals.is_stellar is False
        assert signals.confidence == "none"

    def test_soroban_readme_still_none_without_tooling(self):
        # A README mention of Soroban alone never makes a plain crate "stellar".
        files = [
            _file("src/lib.rs", "pub fn f() {}\n"),
            _file("README.md", "Uses Soroban smart contracts.\n"),
        ]
        signals = detect_stellar_project(files)
        assert signals.is_stellar is False
        assert signals.confidence == "none"


class TestNetworkHints:
    def test_testnet_passphrase(self):
        files = [
            _file(
                "stellar.toml",
                '[NETWORK_TESTNET]\nNETWORK_PASSPHRASE = "Test SDF Network ; September 2015"\n',
            )
        ]
        result = detect_stellar_network(files)
        assert result["network"] == "testnet"

    def test_mainnet_passphrase(self):
        files = [
            _file(
                "stellar.toml",
                'passphrase = "Public Global Stellar Network ; September 2015"\n',
            )
        ]
        result = detect_stellar_network(files)
        assert result["network"] == "mainnet"

    def test_filename_hint(self):
        files = [_file("soroban-testnet.json", '{"network": "testnet"}')]
        result = detect_stellar_network(files)
        assert result["network"] == "testnet"

    def test_conflicting_hints_ambiguous(self):
        files = [
            _file("stellar.toml", 'passphrase = "Test SDF Network ; September 2015"\n'),
            _file("stellar-mainnet.toml", "x = 1\n"),
        ]
        result = detect_stellar_network(files)
        assert result["network"] is None

    def test_no_hints(self):
        result = detect_stellar_network([_file("README.md", "# hi")])
        assert result["network"] is None


class TestMetadata:
    def test_metadata_includes_new_fields(self):
        files = [_file("Cargo.toml", "[dependencies]\nsoroban-sdk = '21.0.0'\n")]
        meta = detect_stellar_project(files).to_dict()
        assert meta["network_hints"] == []
        assert "relevant_files" in meta
        assert meta["is_stellar"] is True

    def test_project_metadata_stellar(self):
        class FakeProject:
            def __init__(self):
                self.files = [
                    SimpleNamespace(
                        path="Cargo.toml", content="[dependencies]\nsoroban-sdk='21.0.0'\n"
                    ),
                    SimpleNamespace(path="stellar.toml", content="[NETWORK_TESTNET]"),
                ]

        meta = project_stellar_metadata(FakeProject())
        assert meta["is_stellar"] is True
        assert "stellar.toml" in meta["network_files"]
        assert meta["confidence"] == "likely"
