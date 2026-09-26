"""Tests for AI-generated Soroban contract skeletons (#187).

Uses the offline mock provider so the post-processing/fallback path is exercised
deterministically, plus fake providers for the "model returned valid code" and
"model returned junk" paths. Asserts the result always round-trips through
Stellar detection as ``likely`` Soroban and is clearly labelled AI-generated.
"""

import pytest

from app.services.soroban_generation import (
    MAX_DESCRIPTION_CHARS,
    generate_soroban_skeleton,
)

VALID_MODEL_OUTPUT = """Here is a skeleton.

```toml
[package]
name = "token_vault"
version = "0.1.0"
edition = "2021"

[lib]
crate-type = ["cdylib", "rlib"]

[dependencies]
soroban-sdk = "21.0.0"
```

```rust
#![no_std]
use soroban_sdk::{contract, contractimpl, Env};

#[contract]
pub struct TokenVault;

#[contractimpl]
impl TokenVault {
    pub fn deposit(_env: Env) -> u32 {
        0
    }
}
```
"""


class _FakeProvider:
    def __init__(self, text):
        self.text = text
        self.calls = []

    def complete(self, messages, stream=False):
        self.calls.append(messages)
        return self.text


def _register(client, username="tester", email="tester@example.com"):
    client.post(
        "/auth/register",
        data={
            "username": username,
            "email": email,
            "password": "supersecret123",
            "password_confirm": "supersecret123",
        },
    )


class TestGenerateSkeletonService:
    def test_mock_provider_falls_back_to_valid_soroban(self):
        result = generate_soroban_skeleton("a token vault contract")
        assert result["ai_generated"] is True
        assert result["verified"] is False
        assert result["used_fallback"] is True
        assert "AI-generated" in result["label"]
        assert "soroban-sdk" in result["cargo_toml"]
        assert "#[contractimpl]" in result["lib_rs"]
        assert "#[contract]" in result["lib_rs"]

    def test_detection_round_trip_is_likely_soroban(self):
        result = generate_soroban_skeleton("a hello world contract")
        detection = result["detection"]
        assert detection["confidence"] == "likely"
        assert detection["is_soroban"] is True
        assert detection["is_stellar"] is True

    def test_valid_model_output_is_used_verbatim(self):
        provider = _FakeProvider(VALID_MODEL_OUTPUT)
        result = generate_soroban_skeleton("a token vault", provider=provider)
        assert result["used_fallback"] is False
        assert result["name"] == "token_vault"
        assert 'name = "token_vault"' in result["cargo_toml"]
        assert "struct TokenVault" in result["lib_rs"]
        assert result["detection"]["confidence"] == "likely"

    def test_junk_model_output_falls_back(self):
        provider = _FakeProvider("I am not going to write Rust today.")
        result = generate_soroban_skeleton("anything", provider=provider)
        assert result["used_fallback"] is True
        assert result["detection"]["confidence"] == "likely"

    def test_partial_model_output_is_repaired(self):
        # Valid Rust but no Cargo.toml: the manifest is substituted, the code
        # the model produced is kept.
        provider = _FakeProvider(
            "```rust\n#![no_std]\nuse soroban_sdk::{contract, contractimpl, Env};\n"
            "#[contract]\npub struct X;\n#[contractimpl]\nimpl X { pub fn f(_e: Env) {} }\n```"
        )
        result = generate_soroban_skeleton("x", name="my_contract", provider=provider)
        assert result["used_fallback"] is True
        assert "struct X" in result["lib_rs"]
        assert 'name = "my_contract"' in result["cargo_toml"]

    def test_description_is_required(self):
        with pytest.raises(ValueError):
            generate_soroban_skeleton("   ")

    def test_description_is_bounded(self):
        long_description = "x" * (MAX_DESCRIPTION_CHARS + 500)
        provider = _FakeProvider(VALID_MODEL_OUTPUT)
        result = generate_soroban_skeleton(long_description, provider=provider)
        assert len(result["description"]) == MAX_DESCRIPTION_CHARS

    def test_files_are_plain_text_rows(self):
        result = generate_soroban_skeleton("a contract")
        paths = {row["path"] for row in result["files"]}
        assert paths == {"Cargo.toml", "src/lib.rs"}
        for row in result["files"]:
            assert row["is_binary"] is False
            assert row["size"] == len(row["content"].encode("utf-8"))


class TestSkeletonRoute:
    def test_requires_login(self, client):
        response = client.post("/tools/soroban/skeleton", json={"description": "x"})
        assert response.status_code == 302

    def test_requires_description(self, client):
        _register(client)
        response = client.post(
            "/tools/soroban/skeleton", json={}, headers={"X-CSRFToken": "ignored"}
        )
        assert response.status_code == 400

    def test_returns_labeled_skeleton(self, client):
        _register(client)
        response = client.post(
            "/tools/soroban/skeleton",
            json={"description": "a token vault contract", "name": "Token Vault"},
            headers={"X-CSRFToken": "ignored"},
        )
        assert response.status_code == 200
        payload = response.get_json()
        assert payload["name"] == "token_vault"
        assert payload["ai_generated"] is True
        assert payload["verified"] is False
        assert "AI-generated" in payload["label"]
        assert payload["detection"]["confidence"] == "likely"
        paths = {row["path"] for row in payload["files"]}
        assert paths == {"Cargo.toml", "src/lib.rs"}
        assert "soroban-sdk" in payload["cargo_toml"]
        assert "#[contractimpl]" in payload["lib_rs"]
