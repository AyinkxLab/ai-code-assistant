"""Deterministic XDR fixture builders for the SCVal/ledger-entry decode tests.

These tiny encoders are written directly against the authoritative byte layout
of the ``stellar/stellar-xdr`` definitions (big-endian, RFC 4506) so the decode
tests exercise real wire format rather than the module under test re-encoding
itself. They deliberately share no code with
``app.services.stellar_xdr_decode``.
"""

from __future__ import annotations

import base64

# Discriminants (authoritative; see stellar_xdr_decode for the full tables).
_CONTRACT_DATA = 6
_CONTRACT_CODE = 7
_SCV_U32 = 3
_SCV_I32 = 4
_SCV_U64 = 5
_SCV_I64 = 6
_SCV_BYTES = 13
_SCV_STRING = 14
_SCV_SYMBOL = 15
_SCV_VEC = 16
_SCV_MAP = 17
_SCV_ADDRESS = 18
_SCV_CONTRACT_INSTANCE = 19
_SCV_LEDGER_KEY_CONTRACT_INSTANCE = 20
_SC_ADDRESS_CONTRACT = 1
_SC_ADDRESS_ACCOUNT = 0
_EXECUTABLE_WASM = 0
_DURABILITY_PERSISTENT = 1


def b64(blob: bytes) -> str:
    return base64.b64encode(blob).decode("ascii")


def p32(value: int) -> bytes:
    return (value & 0xFFFFFFFF).to_bytes(4, "big")


def p64(value: int) -> bytes:
    return (value & 0xFFFFFFFFFFFFFFFF).to_bytes(8, "big")


def opaque(body: bytes) -> bytes:
    """Encode an XDR variable-length opaque/string (length + padded body)."""
    pad = (4 - (len(body) % 4)) % 4
    return p32(len(body)) + body + b"\x00" * pad


def xdr_string(text: str) -> bytes:
    return opaque(text.encode("utf-8"))


def scv(discriminant: int, payload: bytes = b"") -> bytes:
    """Encode a bare ``SCVal`` union arm (discriminant + payload)."""
    return p32(discriminant) + payload


def symbol_scv(text: str) -> bytes:
    return scv(_SCV_SYMBOL, xdr_string(text))


def string_scv(text: str) -> bytes:
    return scv(_SCV_STRING, xdr_string(text))


def u32_scv(value: int) -> bytes:
    return scv(_SCV_U32, p32(value))


def i32_scv(value: int) -> bytes:
    return scv(_SCV_I32, p32(value))


def u64_scv(value: int) -> bytes:
    return scv(_SCV_U64, p64(value))


def i64_scv(value: int) -> bytes:
    return scv(_SCV_I64, p64(value))


def bytes_scv(body: bytes) -> bytes:
    return scv(_SCV_BYTES, opaque(body))


def scaddress_contract(contract_bytes: bytes) -> bytes:
    """Encode an ``SCAddress`` of type contract (``C...`` id payload)."""
    return p32(_SC_ADDRESS_CONTRACT) + contract_bytes


def scaddress_account(account_bytes: bytes) -> bytes:
    """Encode an ``SCAddress`` of type account (PublicKey ED25519 arm)."""
    return p32(_SC_ADDRESS_ACCOUNT) + p32(0) + account_bytes


def address_scv(contract_bytes: bytes) -> bytes:
    return scv(_SCV_ADDRESS, scaddress_contract(contract_bytes))


def instance_scv(wasm_hash: bytes, storage: list[tuple[bytes, bytes]] | None = None) -> bytes:
    """Encode ``SCV_CONTRACT_INSTANCE`` with a WASM executable.

    ``storage`` is a list of ``(key_scv, value_scv)`` already-encoded ``SCVal``
    arms that become the instance's storage ``SCMap``.
    """
    executable = p32(_EXECUTABLE_WASM) + wasm_hash
    if storage is None:
        storage_bytes = p32(0)  # SCMap* -> nil pointer
    else:
        body = p32(1) + p32(len(storage))  # SCMap* present + SCMapEntry count
        body += b"".join(key + value for key, value in storage)
        storage_bytes = body
    return scv(_SCV_CONTRACT_INSTANCE, executable + storage_bytes)


def vec_scv(items: list[bytes]) -> bytes:
    """Encode ``SCV_VEC`` with a non-nil ``SCVec*`` pointer."""
    body = p32(1) + p32(len(items)) + b"".join(items)
    return scv(_SCV_VEC, body)


def map_scv(entries: list[tuple[bytes, bytes]]) -> bytes:
    """Encode ``SCV_MAP`` with a non-nil ``SCMap*`` pointer."""
    body = p32(1) + p32(len(entries)) + b"".join(k + v for k, v in entries)
    return scv(_SCV_MAP, body)


def contract_data_entry(contract_bytes: bytes, key: bytes, durability: int, value: bytes) -> bytes:
    """Encode a ``LedgerEntryData`` CONTRACT_DATA entry.

    Byte layout: type disc, ExtensionPoint (0), ``SCAddress`` contract,
    ``SCVal`` key, ``ContractDataDurability``, then the ``SCVal`` value.
    """
    return (
        p32(_CONTRACT_DATA)
        + p32(0)
        + scaddress_contract(contract_bytes)
        + key
        + p32(durability)
        + value
    )


def contract_data_instance_entry(contract_bytes: bytes, wasm_hash: bytes) -> bytes:
    """Encode a persistent contract-instance entry with a WASM executable."""
    return contract_data_entry(
        contract_bytes,
        scv(_SCV_LEDGER_KEY_CONTRACT_INSTANCE),  # instance key (void payload)
        _DURABILITY_PERSISTENT,
        instance_scv(wasm_hash),
    )


def contract_data_symbol_entry(
    contract_bytes: bytes, key_text: str, value: bytes, durability: int = 1
) -> bytes:
    """Encode a CONTRACT_DATA entry keyed by a symbol with an arbitrary value."""
    return contract_data_entry(contract_bytes, symbol_scv(key_text), durability, value)


def contract_code_entry(wasm_hash: bytes, code: bytes, ext: int = 0) -> bytes:
    """Encode a ``LedgerEntryData`` CONTRACT_CODE entry.

    Byte layout: type disc, ``ext`` union (0/1), 32-byte ``Hash``, then the
    ``opaque code<>`` body.
    """
    return p32(_CONTRACT_CODE) + p32(ext) + wasm_hash + opaque(code)


def ledger_data_b64(blob: bytes) -> str:
    return b64(blob)


# ---------------------------------------------------------------------------
# Transaction-envelope fixture builders (Stellar-transaction.x byte layout)
# ---------------------------------------------------------------------------

_OP_CREATE_ACCOUNT = 0
_OP_PAYMENT = 1
_OP_CHANGE_TRUST = 6
_OP_BUMP_SEQUENCE = 11
_OP_INVOKE_HOST_FUNCTION = 24
_OP_EXTEND_FOOTPRINT_TTL = 25
_OP_RESTORE_FOOTPRINT = 26

_ENVELOPE_TYPE_TX_V0 = 0
_ENVELOPE_TYPE_TX = 2
_ENVELOPE_TYPE_TX_FEE_BUMP = 5

_KEY_TYPE_ED25519 = 0
_KEY_TYPE_MUXED_ED25519 = 256
_CREDENTIALS_SOURCE_ACCOUNT = 0
_HOST_FN_INVOKE_CONTRACT = 0
_AUTHORIZED_FN_CONTRACT = 0

_PUBLIC_KEY_ED25519 = 0
_ASSET_NATIVE = 0
_ASSET_CREDIT_ALPHANUM4 = 1
_ASSET_CREDIT_ALPHANUM12 = 2


def s64(value: int) -> bytes:
    return (value & 0xFFFFFFFFFFFFFFFF).to_bytes(8, "big")


def public_key(payload: bytes) -> bytes:
    """Encode an ``AccountID`` (``PublicKey`` ED25519 union arm)."""
    return p32(_PUBLIC_KEY_ED25519) + payload


def muxed_account(payload: bytes) -> bytes:
    """Encode a ``MuxedAccount`` of type ``KEY_TYPE_ED25519``."""
    return p32(_KEY_TYPE_ED25519) + payload


def muxed_account_med25519(account_id: int, payload: bytes) -> bytes:
    """Encode a ``MuxedAccount`` of type ``KEY_TYPE_MUXED_ED25519``."""
    return p32(_KEY_TYPE_MUXED_ED25519) + p64(account_id) + payload


def optional_pointer(body: bytes | None) -> bytes:
    """Encode an XDR pointer to ``body`` (0 when ``None``, else 1 + body)."""
    return p32(0) if body is None else p32(1) + body


def memo_none() -> bytes:
    return p32(0)


def memo_text(text: str) -> bytes:
    return p32(1) + xdr_string(text)


def memo_id(value: int) -> bytes:
    return p32(2) + p64(value)


def memo_hash(payload: bytes) -> bytes:
    return p32(3) + payload


def memo_return(payload: bytes) -> bytes:
    return p32(4) + payload


def asset_native() -> bytes:
    return p32(_ASSET_NATIVE)


def asset_alphanum(code: str, issuer_payload: bytes, *, length: int = 4) -> bytes:
    """Encode a credit asset with a fixed-width, NUL-padded code."""
    raw = code.encode("ascii")
    if len(raw) > length:
        raise ValueError(f"asset code {code!r} exceeds {length} bytes")
    asset_type = _ASSET_CREDIT_ALPHANUM4 if length == 4 else _ASSET_CREDIT_ALPHANUM12
    return p32(asset_type) + raw.ljust(length, b"\x00") + public_key(issuer_payload)


# --- Operation bodies (without the Operation source-account pointer) --------


def create_account_op(destination_payload: bytes, starting_balance: int) -> bytes:
    return p32(_OP_CREATE_ACCOUNT) + public_key(destination_payload) + s64(starting_balance)


def payment_op(destination: bytes, asset: bytes, amount: int) -> bytes:
    return p32(_OP_PAYMENT) + destination + asset + s64(amount)


def change_trust_op(line: bytes, limit: int, *, ext: int = 0) -> bytes:
    return p32(_OP_CHANGE_TRUST) + line + s64(limit) + p32(ext)


def bump_sequence_op(bump_to: int) -> bytes:
    return p32(_OP_BUMP_SEQUENCE) + s64(bump_to)


def extend_footprint_ttl_op(extend_to: int) -> bytes:
    return p32(_OP_EXTEND_FOOTPRINT_TTL) + p32(extend_to)


def restore_footprint_op() -> bytes:
    return p32(_OP_RESTORE_FOOTPRINT)


def invoke_host_function_op(
    contract_payload: bytes, args: list[bytes], *, auth: bytes | None = None
) -> bytes:
    """Encode ``INVOKE_HOST_FUNCTION`` (contract fn + optional auth entries).

    ``auth`` is the already-encoded ``SorobanAuthorizationEntry<>`` vector
    (including its count); when ``None`` an empty vector is used.
    """
    host_fn = p32(_HOST_FN_INVOKE_CONTRACT) + scaddress_contract(contract_payload) + p32(len(args))
    host_fn += b"".join(args)
    auth_vector = auth if auth is not None else p32(0)
    return p32(_OP_INVOKE_HOST_FUNCTION) + host_fn + auth_vector


def auth_entry_source_account(
    contract_payload: bytes, function_name: str, args: list[bytes]
) -> bytes:
    """Encode one ``SorobanAuthorizationEntry`` (source-account credentials).

    The root invocation authorizes a contract function call with ``args`` and
    no sub-invocations.
    """
    credentials = p32(_CREDENTIALS_SOURCE_ACCOUNT)
    fn = p32(_AUTHORIZED_FN_CONTRACT) + scaddress_contract(contract_payload)
    fn += xdr_string(function_name) + p32(len(args)) + b"".join(args)
    root_invocation = fn + p32(0)  # no sub-invocations
    return credentials + root_invocation


# --- Envelope assembly ------------------------------------------------------


def _operation_vector(ops: list[bytes]) -> bytes:
    """Encode an ``Operation<>`` vector (each op has no source account)."""
    return p32(len(ops)) + b"".join(p32(0) + op for op in ops)


def _preconditions_none() -> bytes:
    return p32(0)


def _preconditions_time(min_time: int, max_time: int) -> bytes:
    return p32(1) + p64(min_time) + p64(max_time)


def _signatures_empty() -> bytes:
    return p32(0)


def tx_v1_envelope(
    source_payload: bytes,
    fee: int,
    sequence: int,
    ops: list[bytes],
    *,
    memo: bytes | None = None,
    preconditions: bytes | None = None,
    source_muxed: bool = False,
) -> bytes:
    """Encode an ``ENVELOPE_TYPE_TX`` (V1) transaction envelope."""
    tx = muxed_account(source_payload) if not source_muxed else source_payload
    tx += p32(fee) + s64(sequence)
    tx += preconditions if preconditions is not None else _preconditions_none()
    tx += memo if memo is not None else memo_none()
    tx += _operation_vector(ops)
    tx += p32(0)  # Transaction ext (v0)
    return p32(_ENVELOPE_TYPE_TX) + tx + _signatures_empty()


def tx_v0_envelope(
    source_payload: bytes,
    fee: int,
    sequence: int,
    ops: list[bytes],
    *,
    memo: bytes | None = None,
    time_bounds: bytes | None = None,
) -> bytes:
    """Encode a legacy ``ENVELOPE_TYPE_TX_V0`` transaction envelope."""
    tx = source_payload + p32(fee) + s64(sequence)
    tx += optional_pointer(time_bounds)
    tx += memo if memo is not None else memo_none()
    tx += _operation_vector(ops)
    tx += p32(0)  # TransactionV0 ext
    return p32(_ENVELOPE_TYPE_TX_V0) + tx + _signatures_empty()


def tx_fee_bump_envelope(
    fee_source_payload: bytes,
    fee: int,
    inner_v1_envelope: bytes,
) -> bytes:
    """Encode an ``ENVELOPE_TYPE_TX_FEE_BUMP`` transaction envelope."""
    return (
        p32(_ENVELOPE_TYPE_TX_FEE_BUMP)
        + muxed_account(fee_source_payload)
        + s64(fee)
        + inner_v1_envelope
        + _signatures_empty()
    )
