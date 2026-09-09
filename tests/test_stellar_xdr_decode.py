"""Tests for the bounded SCVal / contract-data XDR decoder.

The fixtures are produced by the independent encoders in
:mod:`stellar_xdr_fixtures` which are written directly against the
authoritative ``stellar/stellar-xdr`` byte layout, so these tests exercise the
real wire format rather than the decoder re-encoding itself.
"""

import base64

from app.services.stellar_xdr import (
    account_address_to_bytes,
    contract_address_to_bytes,
    ledger_key_contract_code,
    ledger_key_for_account,
    ledger_key_for_contract,
    strkey_encode,
)
from app.services.stellar_xdr_decode import (
    decode_ledger_entry_data,
    decode_ledger_key,
    decode_scval_xdr,
)
from tests.stellar_xdr_fixtures import (
    address_scv,
    bytes_scv,
    contract_code_entry,
    contract_data_entry,
    contract_data_instance_entry,
    contract_data_symbol_entry,
    i32_scv,
    i64_scv,
    instance_scv,
    ledger_data_b64,
    map_scv,
    scaddress_account,
    scaddress_contract,
    scv,
    string_scv,
    symbol_scv,
    u32_scv,
    u64_scv,
    vec_scv,
)

DOCS_CONTRACT = "CCPYZFKEAXHHS5VVW5J45TOU7S2EODJ7TZNJIA5LKDVL3PESCES6FNCI"
VALID_ACCOUNT = "GALAXYVOIDAOPZTDLHILAJQKCVVFMD4IKLXLSZV5YHO7VY74IWZILUTO"
WASM_HASH = bytes(range(32))
WASM_HASH_HEX = WASM_HASH.hex()
CODE = bytes(range(64)) * 4  # 256 bytes of "wasm"


def _b64of(blob: bytes) -> str:
    return base64.b64encode(blob).decode("ascii")


class TestDecodeLedgerKey:
    def test_contract_instance_key_round_trip(self):
        key = ledger_key_for_contract(DOCS_CONTRACT)
        result = decode_ledger_key(key)
        assert result["decoded"] is True
        assert result["type"] == "CONTRACT_DATA"
        assert result["detail"]["contract_id"] == DOCS_CONTRACT
        assert result["detail"]["durability"] == "persistent"
        assert result["detail"]["key"]["type"] == "ledger_key_contract_instance"
        assert result["detail"]["contract"]["kind"] == "contract"

    def test_contract_code_key_round_trip(self):
        key = ledger_key_contract_code(WASM_HASH)
        result = decode_ledger_key(key)
        assert result["decoded"] is True
        assert result["type"] == "CONTRACT_CODE"
        assert result["detail"]["wasm_hash"] == WASM_HASH_HEX

    def test_account_key_round_trip(self):
        key = ledger_key_for_account(VALID_ACCOUNT)
        result = decode_ledger_key(key)
        assert result["decoded"] is True
        assert result["type"] == "ACCOUNT"
        assert result["detail"]["account"] == VALID_ACCOUNT

    def test_malformed_key_not_guessed(self):
        result = decode_ledger_key("!!!not-base64!!!")
        assert result["decoded"] is False
        assert result["reason"]

    def test_unsupported_key_type_reported(self):
        # A CONFIG_SETTING (type 8) key payload is intentionally not decoded.
        blob = (8).to_bytes(4, "big") + b"\x00" * 8
        result = decode_ledger_key(_b64of(blob))
        assert result["decoded"] is False
        assert result["type"] == "CONFIG_SETTING"


class TestDecodeContractInstance:
    def test_instance_entry_decodes(self):
        contract = contract_address_to_bytes(DOCS_CONTRACT)
        xdr = ledger_data_b64(contract_data_instance_entry(contract, WASM_HASH))
        result = decode_ledger_entry_data(xdr)
        assert result["decoded"] is True
        assert result["type"] == "CONTRACT_DATA"
        detail = result["detail"]
        assert detail["contract_id"] == DOCS_CONTRACT
        assert detail["contract"]["kind"] == "contract"
        assert detail["durability"] == "persistent"
        assert detail["key"]["type"] == "ledger_key_contract_instance"
        value = detail["value"]
        assert value["type"] == "instance"
        assert value["executable"]["type"] == "wasm"
        assert value["executable"]["wasm_hash"] == WASM_HASH_HEX
        assert value["storage"] is None

    def test_instance_entry_with_storage_decodes(self):
        contract = contract_address_to_bytes(DOCS_CONTRACT)
        owner = bytes(range(32))
        storage = [
            (symbol_scv("owner"), address_scv(owner)),
            (symbol_scv("total"), u64_scv(2**63 + 1)),
        ]
        instance = instance_scv(WASM_HASH, storage=storage)
        blob = contract_data_entry(contract, scv(20), 1, instance)
        result = decode_ledger_entry_data(ledger_data_b64(blob))
        assert result["decoded"] is True
        storage_out = result["detail"]["value"]["storage"]
        assert storage_out is not None
        assert storage_out[0]["key"]["type"] == "symbol"
        assert storage_out[0]["key"]["value"] == "owner"
        assert storage_out[0]["value"]["type"] == "address"
        assert storage_out[0]["value"]["kind"] == "contract"
        assert storage_out[1]["value"]["type"] == "u64"
        assert storage_out[1]["value"]["value"] == 2**63 + 1

    def test_symbol_key_contract_data_decodes(self):
        contract = bytes(range(32))
        xdr = ledger_data_b64(contract_data_symbol_entry(contract, "balance", u64_scv(12345)))
        result = decode_ledger_entry_data(xdr)
        assert result["decoded"] is True
        detail = result["detail"]
        assert detail["key"]["type"] == "symbol"
        assert detail["key"]["value"] == "balance"
        assert detail["value"]["type"] == "u64"
        assert detail["value"]["value"] == 12345


class TestDecodeContractCode:
    def test_code_entry_returns_hash_and_size_only(self):
        xdr = ledger_data_b64(contract_code_entry(WASM_HASH, CODE))
        result = decode_ledger_entry_data(xdr)
        assert result["decoded"] is True
        assert result["type"] == "CONTRACT_CODE"
        detail = result["detail"]
        assert detail["wasm_hash"] == WASM_HASH_HEX
        assert detail["code_byte_size"] == len(CODE)
        assert detail["code_truncated"] is False
        # The wasm body itself must never be dumped into the result.
        assert "code" not in detail

    def test_code_entry_with_ext_v1_cost_inputs(self):
        blob = (
            (7).to_bytes(4, "big")
            + (1).to_bytes(4, "big")
            + (0).to_bytes(4, "big")  # ContractCodeCostInputs ExtensionPoint
            + b"".join((n).to_bytes(4, "big") for n in range(10))
            + WASM_HASH
            + (len(CODE)).to_bytes(4, "big")
            + CODE
            + b"\x00" * ((4 - (len(CODE) % 4)) % 4)
        )
        result = decode_ledger_entry_data(_b64of(blob))
        assert result["decoded"] is True
        detail = result["detail"]
        assert detail["ext_version"] == 1
        assert detail["cost_inputs"]["nInstructions"] == 0
        assert detail["cost_inputs"]["nDataSegmentBytes"] == 9
        assert detail["wasm_hash"] == WASM_HASH_HEX
        assert detail["code_byte_size"] == len(CODE)

    def test_truncated_code_entry_still_reports_metadata(self):
        truncated = contract_code_entry(WASM_HASH, CODE)[:60]
        result = decode_ledger_entry_data(_b64of(truncated))
        assert result["decoded"] is True
        detail = result["detail"]
        assert detail["wasm_hash"] == WASM_HASH_HEX
        assert detail["code_byte_size"] == len(CODE)
        assert detail["code_truncated"] is True


class TestUnsupportedAndMalformed:
    def test_unsupported_entry_type_reported(self):
        # A LedgerEntryData of type ACCOUNT (disc 0) is structurally valid but
        # outside this decoder's supported contract-data/code subset.
        result = decode_ledger_entry_data("AAAAAA==")
        assert result["decoded"] is False
        assert result["type"] == "ACCOUNT"
        assert "no structured decoder" in result["reason"]

    def test_unknown_entry_type_reported(self):
        result = decode_ledger_entry_data(_b64of((99).to_bytes(4, "big")))
        assert result["decoded"] is False
        assert result["type"] == "UNKNOWN"

    def test_empty_xdr_is_undecodable(self):
        result = decode_ledger_entry_data("")
        assert result["decoded"] is False
        assert result["reason"]

    def test_non_base64_xdr_is_undecodable(self):
        result = decode_ledger_entry_data("not base64!!")
        assert result["decoded"] is False
        assert "base64" in result["reason"]

    def test_truncated_instance_entry_is_undecodable(self):
        contract = contract_address_to_bytes(DOCS_CONTRACT)
        full = contract_data_instance_entry(contract, WASM_HASH)
        result = decode_ledger_entry_data(_b64of(full[:30]))
        assert result["decoded"] is False
        assert "truncated" in result["reason"]

    def test_invalid_durability_reported(self):
        contract = contract_address_to_bytes(DOCS_CONTRACT)
        blob = (
            (6).to_bytes(4, "big")
            + (0).to_bytes(4, "big")
            + scaddress_contract(contract)
            + scv(20)
            + (7).to_bytes(4, "big")  # durability 7 is invalid
            + instance_scv(WASM_HASH)
        )
        result = decode_ledger_entry_data(_b64of(blob))
        assert result["decoded"] is False
        assert "durability" in result["reason"].lower()

    def test_invalid_scval_discriminant_reported(self):
        contract = bytes(range(32))
        blob = contract_data_symbol_entry(contract, "balance", (250).to_bytes(4, "big"))
        result = decode_ledger_entry_data(_b64of(blob))
        assert result["decoded"] is False
        assert "Unknown SCVal type" in result["reason"]

    def test_unsupported_scval_leaf_is_not_guessed(self):
        # u256 (disc 11) is a fixed-size variant we do not render.
        result = decode_scval_xdr(_b64of((11).to_bytes(4, "big") + b"\x00" * 32))
        assert result["decoded"] is False
        assert "u256" in result["reason"]

    def test_deeply_nested_scval_is_bounded(self):
        leaf = symbol_scv("x")
        for _ in range(40):
            leaf = vec_scv([leaf])
        result = decode_scval_xdr(_b64of(leaf))
        assert result["decoded"] is False
        assert "nesting" in result["reason"]

    def test_oversized_collection_is_bounded(self):
        # A vec declaring more items than could ever fit in the payload.
        blob = (16).to_bytes(4, "big") + (1).to_bytes(4, "big") + (100000).to_bytes(4, "big")
        result = decode_scval_xdr(_b64of(blob))
        assert result["decoded"] is False
        assert "exceeds" in result["reason"]

    def test_oversized_bytes_length_is_bounded(self):
        # An SCV_BYTES declaring a length larger than the payload present.
        blob = (13).to_bytes(4, "big") + (2**32 - 1).to_bytes(4, "big")
        result = decode_scval_xdr(_b64of(blob))
        assert result["decoded"] is False


class TestDecodeScval:
    def test_symbol(self):
        result = decode_scval_xdr(_b64of(symbol_scv("transfer")))
        assert result == {"type": "symbol", "value": "transfer", "decoded": True}

    def test_string(self):
        result = decode_scval_xdr(_b64of(string_scv("hello")))
        assert result["value"] == "hello"

    def test_u64_and_i64(self):
        assert decode_scval_xdr(_b64of(u64_scv(2**63 + 1)))["value"] == 2**63 + 1
        assert decode_scval_xdr(_b64of(i64_scv(-5)))["value"] == -5

    def test_u32_i32(self):
        assert decode_scval_xdr(_b64of(u32_scv(7)))["value"] == 7
        assert decode_scval_xdr(_b64of(i32_scv(-7)))["value"] == -7

    def test_bytes_small(self):
        result = decode_scval_xdr(_b64of(bytes_scv(b"\x01\x02\x03")))
        assert result["length"] == 3
        assert result["hex"] == "010203"

    def test_vec_of_values(self):
        result = decode_scval_xdr(_b64of(vec_scv([symbol_scv("a"), u32_scv(9)])))
        assert result["value"] == [
            {"type": "symbol", "value": "a"},
            {"type": "u32", "value": 9},
        ]

    def test_map_of_values(self):
        result = decode_scval_xdr(_b64of(map_scv([(symbol_scv("k"), u64_scv(1))])))
        assert result["value"][0]["key"]["value"] == "k"
        assert result["value"][0]["value"]["value"] == 1

    def test_empty_vec_is_decodable(self):
        blob = (16).to_bytes(4, "big") + (0).to_bytes(4, "big")  # nil pointer
        result = decode_scval_xdr(_b64of(blob))
        assert result["decoded"] is True
        assert result["value"] == []

    def test_contract_address_round_trips_to_strkey(self):
        result = decode_scval_xdr(_b64of(address_scv(WASM_HASH)))
        assert result["decoded"] is True
        assert result["kind"] == "contract"
        assert result["strkey"] == strkey_encode(0x10, WASM_HASH)

    def test_account_address_round_trips_to_strkey(self):
        account = account_address_to_bytes(VALID_ACCOUNT)
        result = decode_scval_xdr(_b64of(scv(18, scaddress_account(account))))
        assert result["kind"] == "account"
        assert result["strkey"] == VALID_ACCOUNT
