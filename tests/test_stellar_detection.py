"""Tests for heuristic Stellar/Soroban project detection."""

from types import SimpleNamespace

from app.services.stellar_detection import (
    SOROBAN_CRATES,
    STELLAR_SDK_DEPENDENCIES,
    detect_stellar_project,
    project_stellar_metadata,
)


def _file(path, content=None):
    return SimpleNamespace(path=path, content=content)


class TestDetectionBasics:
    def test_empty_project_not_stellar(self):
        signals = detect_stellar_project([])
        assert signals.is_stellar is False
        assert signals.confidence == "none"

    def test_soroban_cargo_dependency_likely(self):
        files = [
            _file(
                "Cargo.toml",
                "[package]\nname='x'\n[dependencies]\nsoroban-sdk = { version = '21.0.0' }\n",
            )
        ]
        signals = detect_stellar_project(files)
        assert signals.is_stellar is True
        assert signals.confidence == "likely"
        assert signals.soroban_cargo_dependency is True

    def test_contract_attribute_likely(self):
        files = [_file("src/lib.rs", "#![no_std]\n#[contractimpl]\npub struct Contract {}")]
        signals = detect_stellar_project(files)
        assert signals.confidence == "likely"
        assert signals.is_soroban is True

    def test_soroban_import_likely(self):
        files = [_file("src/lib.rs", "use soroban_sdk::contract;\n")]
        signals = detect_stellar_project(files)
        assert signals.confidence == "likely"
        assert signals.soroban_import is True

    def test_plain_rust_project_not_stellar(self):
        files = [
            _file("Cargo.toml", "[dependencies]\nserde = '1.0'\n"),
            _file("src/main.rs", "fn main() {}"),
        ]
        signals = detect_stellar_project(files)
        assert signals.is_stellar is False
        assert signals.confidence == "none"

    def test_stellar_sdk_package_json_possible(self):
        files = [
            _file("package.json", '{"dependencies": {"stellar-sdk": "^11.0.0"}}'),
        ]
        signals = detect_stellar_project(files)
        assert signals.is_stellar is True
        assert signals.confidence == "possible"
        assert signals.stellar_sdk_dependency is True

    def test_stellar_config_file_possible(self):
        files = [
            _file(
                "stellar.toml",
                '[NETWORK_TESTNET]\nHORIZON_URL = "https://horizon-testnet.stellar.org"\n',
            )
        ]
        signals = detect_stellar_project(files)
        assert signals.is_stellar is True
        assert signals.confidence == "possible"
        assert signals.stellar_config_file is True

    def test_contract_directory_only_is_weak(self):
        files = [_file("contracts/hello/Cargo.toml", "[dependencies]\nrand = '0.8'\n")]
        signals = detect_stellar_project(files)
        assert signals.confidence == "possible"
        assert signals.is_soroban is False


class TestManifestParsing:
    def test_workspace_dependencies_detected(self):
        files = [
            _file(
                "Cargo.toml",
                "[workspace.dependencies]\nsoroban-sdk = { workspace = true }\n"
                "soroban-auth = '21.0.0'\n",
            )
        ]
        signals = detect_stellar_project(files)
        assert signals.soroban_cargo_dependency is True
        assert signals.confidence == "likely"

    def test_requirements_stellar_sdk(self):
        files = [_file("requirements.txt", "stellar-sdk>=9.0.0\nrequests==2.31.0\n")]
        signals = detect_stellar_project(files)
        assert signals.stellar_sdk_dependency is True

    def test_go_mod_stellar_base(self):
        go_mod = (
            "module example.com/x\n\ngo 1.21\n\n"
            "require github.com/stellar/go-stellar-base v0.0.1\n"
        )
        files = [_file("go.mod", go_mod)]
        signals = detect_stellar_project(files)
        assert signals.stellar_sdk_dependency is True

    def test_scoped_soroban_crate_membership(self):
        assert "soroban-sdk" in SOROBAN_CRATES
        assert "@stellar/stellar-sdk" in STELLAR_SDK_DEPENDENCIES


class TestMetadata:
    def test_metadata_dict_shape(self):
        files = [_file("Cargo.toml", "[dependencies]\nsoroban-sdk='21.0.0'\n")]
        signals = detect_stellar_project(files)
        meta = signals.to_dict()
        assert meta["is_stellar"] is True
        assert meta["confidence"] == "likely"
        assert "signals" in meta
        assert len(meta["evidence"]) >= 1

    def test_project_stellar_metadata_not_stellar(self):
        class FakeProject:
            def __init__(self):
                self.files = [SimpleNamespace(path="README.md", content="# hi")]

        meta = project_stellar_metadata(FakeProject())
        assert meta["is_stellar"] is False
        assert meta["confidence"] == "none"
        assert "network_files" not in meta

    def test_project_stellar_metadata_stellar(self):
        class FakeProject:
            def __init__(self):
                self.files = [
                    SimpleNamespace(
                        path="Cargo.toml",
                        content="[dependencies]\nsoroban-sdk='21.0.0'\n",
                    ),
                    SimpleNamespace(path="stellar.toml", content="[NETWORK_TESTNET]"),
                ]

        meta = project_stellar_metadata(FakeProject())
        assert meta["is_stellar"] is True
        assert "stellar.toml" in meta["network_files"]
