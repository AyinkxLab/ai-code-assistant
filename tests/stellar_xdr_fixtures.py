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
