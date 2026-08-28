"""Tests for the minimal Stellar strkey and LedgerKey XDR encoders.

All encoders are verified against authoritative fixture values from the Stellar
RPC documentation and the official ``stellar-xdr`` definitions.
"""

import base64
import struct

import pytest

from app.services.stellar_xdr import (
    StrkeyError,
    account_address_to_bytes,
    contract_address_to_bytes,
    ledger_key_account,
    ledger_key_contract_code,
    ledger_key_contract_data_instance,
    ledger_key_for_account,
    ledger_key_for_contract,
    strkey_decode,
    strkey_encode,
    validate_ledger_key_base64,
    validate_stellar_strkey,
    wasm_hash_from_hex,
)

# Real addresses taken from the official Stellar RPC documentation.
GALAXY_ADDRESS = "GALAXYVOIDAOPZTDLHILAJQKCVVFMD4IKLXLSZV5YHO7VY74IWZILUTO"
DOCS_ACCOUNT = "GAF67IMC4Y3SAEDHZMN57J5G22A7M34R4OFDA6PDO4I4VINS25ZYLBZZ"
DOCS_CONTRACT = "CCPYZFKEAXHHS5VVW5J45TOU7S2EODJ7TZNJIA5LKDVL3PESCES6FNCI"

#: Official ``stellar xdr decode --type LedgerKey`` fixture for DOCS_ACCOUNT.
DOCS_ACCOUNT_LEDGER_KEY = "AAAAAAAAAAAL76GC5jcgEGfLG9+nptaB9m+R44oweeN3EcqhstdzhQ=="


def _u32(raw, offset):
    return struct.unpack(">I", raw[offset : offset + 4])[0]


class TestStrkey:
    def test_account_round_trip(self):
        payload = account_address_to_bytes(GALAXY_ADDRESS)
        assert strkey_encode(0x30, payload) == GALAXY_ADDRESS

    def test_contract_round_trip(self):
        payload = contract_address_to_bytes(DOCS_CONTRACT)
        assert strkey_encode(0x10, payload) == DOCS_CONTRACT

    def test_decode_returns_version_and_payload(self):
        version, payload = strkey_decode(GALAXY_ADDRESS)
        assert version == 0x30
        assert len(payload) == 32

    def test_valid_strkey_checksum(self):
        ok, reason = validate_stellar_strkey(GALAXY_ADDRESS, prefix="G")
        assert ok, reason
        ok, reason = validate_stellar_strkey(DOCS_CONTRACT, prefix="C")
        assert ok, reason

    def test_wrong_prefix_rejected(self):
        ok, _ = validate_stellar_strkey(GALAXY_ADDRESS, prefix="C")
        assert ok is False

    def test_structural_only_address_rejected(self):
        # A structurally valid (56 chars, G prefix) address with a bad checksum
        # must fail full checksum validation.
        ok, _ = validate_stellar_strkey("G" + "A" * 55, prefix="G")
        assert ok is False

    def test_corrupted_checksum_rejected(self):
        replacement = "Q" if GALAXY_ADDRESS[3] != "Q" else "R"
        corrupted = "G" + GALAXY_ADDRESS[1:3] + replacement + GALAXY_ADDRESS[4:]
        ok, _ = validate_stellar_strkey(corrupted, prefix="G")
        assert ok is False

    def test_invalid_alphabet_rejected(self):
        ok, reason = validate_stellar_strkey("G" + "0" * 55, prefix="G")
        assert ok is False
        assert "base32" in reason

    def test_not_a_string(self):
        ok, _ = validate_stellar_strkey(12345, prefix="G")
        assert ok is False

    def test_account_address_to_bytes_length(self):
        assert len(account_address_to_bytes(GALAXY_ADDRESS)) == 32

    def test_contract_address_to_bytes_length(self):
        assert len(contract_address_to_bytes(DOCS_CONTRACT)) == 32

    def test_wrong_version_payload_rejected(self):
        with pytest.raises(StrkeyError):
            account_address_to_bytes(DOCS_CONTRACT)


class TestLedgerKeyEncoding:
    def test_account_key_matches_official_fixture(self):
        # Exact match against the LedgerKey base64 shown in the official docs
        # ("stellar xdr decode --type LedgerKey" example).
        assert ledger_key_for_account(DOCS_ACCOUNT) == DOCS_ACCOUNT_LEDGER_KEY

    def test_account_key_layout(self):
        raw = base64.b64decode(ledger_key_account(b"\x01" * 32))
        assert _u32(raw, 0) == 0  # LedgerEntryType.ACCOUNT
        assert _u32(raw, 4) == 0  # PublicKeyType.ED25519
        assert raw[8:] == b"\x01" * 32

    def test_contract_instance_key_layout(self):
        raw = base64.b64decode(ledger_key_contract_data_instance(b"\x02" * 32))
        assert len(raw) == 48
        assert _u32(raw, 0) == 6  # LedgerEntryType.CONTRACT_DATA
        assert _u32(raw, 4) == 1  # SCAddressType.CONTRACT
        assert raw[8:40] == b"\x02" * 32
        assert _u32(raw, 40) == 20  # SCV_LEDGER_KEY_CONTRACT_INSTANCE
        assert _u32(raw, 44) == 1  # ContractDataDurability.PERSISTENT

    def test_contract_code_key_layout(self):
        raw = base64.b64decode(ledger_key_contract_code(b"\x03" * 32))
        assert len(raw) == 36
        assert _u32(raw, 0) == 7  # LedgerEntryType.CONTRACT_CODE
        assert raw[4:] == b"\x03" * 32

    def test_contract_key_for_real_contract(self):
        key = ledger_key_for_contract(DOCS_CONTRACT)
        raw = base64.b64decode(key)
        assert _u32(raw, 0) == 6
        assert raw[8:40] == contract_address_to_bytes(DOCS_CONTRACT)

    def test_short_payload_rejected(self):
        with pytest.raises(StrkeyError):
            ledger_key_account(b"\x00" * 31)
        with pytest.raises(StrkeyError):
            ledger_key_contract_data_instance(b"\x00" * 31)


class TestWasmHash:
    def test_valid_hash(self):
        raw = wasm_hash_from_hex("a" * 64)
        assert len(raw) == 32
        assert raw == bytes.fromhex("a" * 64)

    def test_short_hash_rejected(self):
        with pytest.raises(StrkeyError):
            wasm_hash_from_hex("a" * 63)

    def test_non_hex_rejected(self):
        with pytest.raises(StrkeyError):
            wasm_hash_from_hex("z" * 64)


class TestLedgerKeyValidation:
    def test_valid_base64_key(self):
        ok, _ = validate_ledger_key_base64(DOCS_ACCOUNT_LEDGER_KEY)
        assert ok

    def test_empty_rejected(self):
        ok, _ = validate_ledger_key_base64("")
        assert ok is False

    def test_not_base64_rejected(self):
        ok, reason = validate_ledger_key_base64("not base64!!")
        assert ok is False
        assert "base64" in reason

    def test_too_short_rejected(self):
        tiny = base64.b64encode(b"\x00" * 8).decode("ascii")
        ok, _ = validate_ledger_key_base64(tiny)
        assert ok is False

    def test_too_long_rejected(self):
        big = base64.b64encode(b"\x00" * 4096).decode("ascii")
        ok, _ = validate_ledger_key_base64(big)
        assert ok is False
