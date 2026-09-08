"""Tests for the bounded read-only transaction-envelope XDR decoder (#177).

The fixtures are produced by the independent encoders in
:mod:`stellar_xdr_fixtures`, which are written directly against the
authoritative ``stellar/stellar-xdr`` byte layout — so these tests exercise the
real wire format rather than the decoder re-encoding itself.

Covered:
- V1 / V0 / fee-bump envelope decoding,
- payment, create account, change trust, bump sequence, invoke host function
  (with a Soroban auth entry), extend footprint TTL, and restore footprint,
- unsupported operation types reported clearly (never crashed on),
- malformed/truncated/unknown XDR producing explicit undecodable results,
- bounding (operation count, memo size) enforced.
"""

from app.services.stellar_xdr import strkey_encode
from app.services.stellar_xdr_decode import decode_transaction_envelope
from tests.stellar_xdr_fixtures import (
    asset_alphanum,
    asset_native,
    auth_entry_source_account,
    b64,
    bump_sequence_op,
    change_trust_op,
    create_account_op,
    extend_footprint_ttl_op,
    invoke_host_function_op,
    memo_hash,
    memo_id,
    memo_return,
    memo_text,
    muxed_account_med25519,
    payment_op,
    restore_footprint_op,
    symbol_scv,
    tx_fee_bump_envelope,
    tx_v0_envelope,
    tx_v1_envelope,
)

KEY_A = bytes(range(32))
KEY_B = bytes(range(32, 64))
KEY_C = bytes(range(64, 96))
ISSUER = bytes(range(100, 132))
STRKEY_A = strkey_encode(0x30, KEY_A)
STRKEY_B = strkey_encode(0x30, KEY_B)


def test_payment_envelope_decodes():
    destination = muxed_account_med25519(12345, KEY_B)
    envelope = tx_v1_envelope(
        KEY_A,
        fee=100,
        sequence=123456,
        ops=[payment_op(destination, asset_native(), 1_000_000_000)],
        memo=memo_text("hi"),
    )
    result = decode_transaction_envelope(b64(envelope))
    assert result["decoded"] is True
    assert result["envelope_type"] == "ENVELOPE_TYPE_TX"
    assert result["source_account"]["account"] == STRKEY_A
    assert result["fee"] == {"stroops": 100, "lumens": "0.00001"}
    assert result["sequence"] == 123456
    assert result["memo"] == {"type": "text", "value": "hi"}
    assert result["preconditions"] == {"type": "none"}
    assert result["operation_parsing"]["complete"] is True
    op = result["operations"][0]
    assert op["decoded"] is True
    assert op["type"] == "PAYMENT"
    assert op["source_account"] is None
    payload = op["payload"]
    assert payload["destination"]["type"] == "muxed"
    assert payload["destination"]["id"] == 12345
    assert payload["destination"]["account"] == STRKEY_B
    assert payload["asset"] == {"type": "native"}
    assert payload["amount"]["stroops"] == 1_000_000_000
    assert payload["amount"]["lumens"] == "100"
    assert result["signatures"] == []


def test_create_account_envelope_decodes():
    envelope = tx_v1_envelope(
        KEY_A,
        fee=200,
        sequence=7,
        ops=[create_account_op(KEY_B, 5_000_000_000)],
        memo=memo_id(42),
    )
    result = decode_transaction_envelope(b64(envelope))
    assert result["decoded"] is True
    assert result["memo"] == {"type": "id", "value": 42}
    op = result["operations"][0]
    assert op["type"] == "CREATE_ACCOUNT"
    assert op["payload"]["destination"] == STRKEY_B
    assert op["payload"]["starting_balance"]["stroops"] == 5_000_000_000
    assert op["payload"]["starting_balance"]["lumens"] == "500"


def test_change_trust_and_bump_sequence_decode():
    envelope = tx_v1_envelope(
        KEY_A,
        fee=100,
        sequence=9,
        ops=[
            change_trust_op(asset_alphanum("USDC", ISSUER), 10_000_000_000),
            bump_sequence_op(100),
        ],
    )
    result = decode_transaction_envelope(b64(envelope))
    ops = result["operations"]
    assert [op["type"] for op in ops] == ["CHANGE_TRUST", "BUMP_SEQUENCE"]
    trust = ops[0]["payload"]
    assert trust["line"]["code"] == "USDC"
    assert trust["line"]["issuer"] == strkey_encode(0x30, ISSUER)
    assert trust["line"]["type"] == "alphanum4"
    assert trust["limit"]["lumens"] == "1000"
    assert trust["liquidity_pool"] is None
    assert ops[1]["payload"]["bump_to"] == 100


def test_extend_ttl_and_restore_footprint_decode():
    envelope = tx_v1_envelope(
        KEY_A,
        fee=100,
        sequence=11,
        ops=[extend_footprint_ttl_op(6_000_000), restore_footprint_op()],
    )
    result = decode_transaction_envelope(b64(envelope))
    ops = result["operations"]
    assert ops[0]["type"] == "EXTEND_FOOTPRINT_TTL"
    assert ops[0]["payload"]["extend_to"] == 6_000_000
    assert ops[1]["type"] == "RESTORE_FOOTPRINT"
    assert ops[1]["payload"] == {}


def test_invoke_host_function_with_auth_decodes():
    auth = len_marker(auth_entry_source_account(KEY_C, "transfer", [symbol_scv("arg1")]))
    envelope = tx_v1_envelope(
        KEY_A,
        fee=100,
        sequence=3,
        ops=[invoke_host_function_op(KEY_C, [symbol_scv("transfer")], auth=auth)],
    )
    result = decode_transaction_envelope(b64(envelope))
    op = result["operations"][0]
    assert op["type"] == "INVOKE_HOST_FUNCTION"
    host = op["payload"]["host_function"]
    assert host["type"] == "invoke_contract"
    assert host["contract"]["kind"] == "contract"
    assert host["contract"]["strkey"] == strkey_encode(0x10, KEY_C)
    assert host["args_count"] == 1
    assert op["payload"]["auth_count"] == 1
    entry = op["payload"]["auth"][0]
    assert entry["credentials"] == {"type": "source_account"}
    assert entry["root_invocation"]["function"]["function_name"] == "transfer"


def len_marker(body: bytes) -> bytes:
    """Prefix ``body`` with its u32 length (used to build a one-entry vector)."""
    return (1).to_bytes(4, "big") + body


def test_memo_hash_and_return_decoded_as_hex():
    digest = bytes(range(32))
    envelope = tx_v1_envelope(KEY_A, 100, 5, [bump_sequence_op(1)], memo=memo_hash(digest))
    result = decode_transaction_envelope(b64(envelope))
    assert result["memo"] == {"type": "hash", "hex": digest.hex()}

    envelope = tx_v1_envelope(KEY_A, 100, 6, [bump_sequence_op(1)], memo=memo_return(digest))
    result = decode_transaction_envelope(b64(envelope))
    assert result["memo"] == {"type": "return", "hex": digest.hex()}


def test_v0_envelope_decodes_with_time_bounds():
    envelope = tx_v0_envelope(
        KEY_A,
        100,
        12,
        [payment_op(muxed_account_med25519(0, KEY_B), asset_native(), 5_000_000_000)],
        memo=memo_none(),
        time_bounds=(1000).to_bytes(8, "big") + (2000).to_bytes(8, "big"),
    )
    result = decode_transaction_envelope(b64(envelope))
    assert result["decoded"] is True
    assert result["envelope_type"] == "ENVELOPE_TYPE_TX_V0"
    assert result["source_account"]["account"] == STRKEY_A
    assert result["preconditions"] == {
        "type": "time",
        "time_bounds": {"min_time": 1000, "max_time": 2000},
    }
    assert result["operations"][0]["type"] == "PAYMENT"


def test_fee_bump_envelope_decodes_inner_transaction():
    inner = tx_v1_envelope(
        KEY_A, 100, 14, [payment_op(muxed_account_med25519(0, KEY_B), asset_native(), 1000)]
    )
    envelope = tx_fee_bump_envelope(KEY_B, 500, inner)
    result = decode_transaction_envelope(b64(envelope))
    assert result["decoded"] is True
    assert result["envelope_type"] == "ENVELOPE_TYPE_TX_FEE_BUMP"
    assert result["fee_source"]["account"] == STRKEY_B
    assert result["fee"]["stroops"] == 500
    inner_decoded = result["inner_transaction"]
    assert inner_decoded["source_account"]["account"] == STRKEY_A
    assert inner_decoded["operations"][0]["type"] == "PAYMENT"


class TestUnsupportedAndMalformed:
    def test_unsupported_operation_reported_not_crashed(self):
        # SET_OPTIONS (type 5) has no decoder here; decoding must stop cleanly
        # and report the rest of the transaction as not parsed.
        unsupported_op = (5).to_bytes(4, "big")
        envelope = tx_v1_envelope(KEY_A, 100, 1, [unsupported_op])
        result = decode_transaction_envelope(b64(envelope))
        assert result["decoded"] is True
        op = result["operations"][0]
        assert op["decoded"] is False
        assert op["type"] == "SET_OPTIONS"
        assert "not decoded" in op["reason"]
        parsing = result["operation_parsing"]
        assert parsing["complete"] is False
        assert parsing["declared"] == 1
        assert parsing["decoded"] == 0
        assert result["signatures"] is None
        assert result["signature_parsing"]["complete"] is False

    def test_unknown_operation_reported_not_crashed(self):
        unknown_op = (99).to_bytes(4, "big")
        envelope = tx_v1_envelope(KEY_A, 100, 1, [unknown_op])
        result = decode_transaction_envelope(b64(envelope))
        assert result["operations"][0]["type"] == "UNKNOWN"
        assert result["operation_parsing"]["complete"] is False

    def test_mixed_ops_decode_then_stop_at_unsupported(self):
        envelope = tx_v1_envelope(
            KEY_A,
            100,
            2,
            [
                payment_op(muxed_account_med25519(0, KEY_B), asset_native(), 100),
                (5).to_bytes(4, "big"),  # SET_OPTIONS -> stop here
            ],
        )
        result = decode_transaction_envelope(b64(envelope))
        ops = result["operations"]
        assert ops[0]["decoded"] is True and ops[0]["type"] == "PAYMENT"
        assert ops[1]["decoded"] is False and ops[1]["type"] == "SET_OPTIONS"
        assert result["operation_parsing"]["decoded"] == 1

    def test_non_base64_xdr_is_undecodable(self):
        result = decode_transaction_envelope("not base64!!")
        assert result["decoded"] is False
        assert "base64" in result["reason"]

    def test_empty_xdr_is_undecodable(self):
        assert decode_transaction_envelope("")["decoded"] is False

    def test_unknown_envelope_type_is_undecodable(self):
        result = decode_transaction_envelope(b64((99).to_bytes(4, "big")))
        assert result["decoded"] is False
        assert result["type"] == "UNKNOWN"

    def test_truncated_envelope_is_undecodable(self):
        full = tx_v1_envelope(
            KEY_A,
            100,
            3,
            [payment_op(muxed_account_med25519(0, KEY_B), asset_native(), 100)],
        )
        result = decode_transaction_envelope(b64(full[:30]))
        assert result["decoded"] is False
        assert "truncated" in result["reason"]

    def test_declared_op_count_exceeding_bound_is_undecodable(self):
        # A V1 envelope claiming more operations than its bytes can hold.
        tx = (
            muxed_account_med25519(0, KEY_A)
            + (100).to_bytes(4, "big")
            + (5).to_bytes(8, "big")
            + (0).to_bytes(4, "big")  # PRECOND_NONE
            + (0).to_bytes(4, "big")  # MEMO_NONE
            + (300).to_bytes(4, "big")  # declared operations (over the cap)
        )
        result = decode_transaction_envelope(b64(tx))
        assert result["decoded"] is False


def memo_none() -> bytes:
    return (0).to_bytes(4, "big")
