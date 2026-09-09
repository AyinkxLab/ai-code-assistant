"""Bounded, read-only decoding of Stellar/Soroban contract-data XDR.

This module turns the base64 XDR values returned by the Stellar RPC
``getLedgerEntries`` method into a structured, JSON-safe, developer-facing view
without ever signing, simulating, or submitting anything.

What it decodes (all read-only, verified against the authoritative
``stellar/stellar-xdr`` definitions used by current-protocol networks):

* ``LedgerKey`` — the subset relevant to contract inspection (account,
  contract-data, contract-code) so a caller-supplied or server-returned key can
  be identified and summarised.
* ``LedgerEntryData`` — the ``xdr`` field each RPC ``getLedgerEntries`` result
  carries (the ``LedgerEntry`` union discriminant plus its payload):
    - ``CONTRACT_DATA`` → ``ContractDataEntry`` (extension point, ``SCAddress``
      contract, ``SCVal`` key, ``ContractDataDurability``, ``SCVal`` value),
    - ``CONTRACT_CODE``  → ``ContractCodeEntry`` (extension, wasm ``hash`` and
      code length; the wasm bytes themselves are never dumped),
    - every other entry type is reported explicitly as unsupported rather than
      guessed at.
* ``SCVal`` — the common representable variants used in contract data and in a
  contract's instance storage map: bool, void, u32/i32, u64/i64, timepoint,
  duration, u128/i128 parts, bytes, string, symbol, vec, map, address,
  contract-instance (executable + optional storage), the two internal
  ledger-key ``SCVal`` variants, and error values. A small number of fixed-size
  variants that this project deliberately does not render (u256/i256,
  executable-tag, non-account/contract address types) are returned as explicit
  ``unsupported`` markers after safely skipping their authoritative byte
  lengths.
* ``TransactionEnvelope`` — a bounded read-only view of the transaction
  envelopes the RPC returns (V1, legacy V0, and fee-bump) with their common
  operations decoded into structured, developer-facing summaries: payment,
  create account, change trust, bump sequence, invoke host function (including
  a bounded view of the Soroban auth entries), extend footprint TTL, and
  restore footprint. Any other operation type is reported explicitly as
  unsupported and decoding stops there (the remaining operations and
  signatures are then honestly marked "not parsed"); values are never guessed.

Design rules:

* **Never fabricate.** Unsupported or malformed XDR always produces an explicit
  ``decoded: False`` (or ``unsupported``) result with a reason — never a guessed
  value. Decoding a value means reading it from the authoritative byte layout.
* **Bounded.** Recursion depth and collection sizes are capped; variable-length
  arrays/strings are checked against the bytes actually present; raw wasm bytes
  and oversized byte blobs are never copied into results.
* **Malformed-safe.** Truncation, invalid discriminants, bad UTF-8, and
  non-zero extension discriminants produce a structured ``decoded: False``
  result; nothing here raises out of the public API.

The decode entry points (``decode_ledger_key``, ``decode_ledger_entry_data``,
``decode_scval_xdr``, ``decode_transaction_envelope``) never raise: each
returns a dict whose ``decoded`` key is ``True`` on success and ``False`` with
a ``reason`` otherwise.
"""

from __future__ import annotations

import base64
import binascii
from decimal import Decimal
from typing import Any

from app.services.stellar_xdr import StrkeyError, strkey_encode

# ---------------------------------------------------------------------------
# Authoritative discriminants (stellar/stellar-xdr, current protocol).
# ---------------------------------------------------------------------------

_LEDGER_ENTRY_NAMES: dict[int, str] = {
    0: "ACCOUNT",
    1: "TRUSTLINE",
    2: "OFFER",
    3: "DATA",
    4: "CLAIMABLE_BALANCE",
    5: "LIQUIDITY_POOL",
    6: "CONTRACT_DATA",
    7: "CONTRACT_CODE",
    8: "CONFIG_SETTING",
    9: "TTL",
}

#: ``SCValType`` discriminants (Stellar-contract.x).
_SCV = {
    "BOOL": 0,
    "VOID": 1,
    "ERROR": 2,
    "U32": 3,
    "I32": 4,
    "U64": 5,
    "I64": 6,
    "TIMEPOINT": 7,
    "DURATION": 8,
    "U128": 9,
    "I128": 10,
    "U256": 11,
    "I256": 12,
    "BYTES": 13,
    "STRING": 14,
    "SYMBOL": 15,
    "VEC": 16,
    "MAP": 17,
    "ADDRESS": 18,
    "CONTRACT_INSTANCE": 19,
    "LEDGER_KEY_CONTRACT_INSTANCE": 20,
    "LEDGER_KEY_NONCE": 21,
    "EXECUTABLE_TAG": 22,
}
_SCV_NAME_BY_DISC = {disc: name for name, disc in _SCV.items()}

#: ``SCAddressType`` discriminants (Stellar-contract.x).
_SC_ADDRESS_ACCOUNT = 0
_SC_ADDRESS_CONTRACT = 1

#: ``SCErrorType`` discriminants (Stellar-contract.x).
_SC_ERROR_TYPE_NAMES = {
    0: "contract",
    1: "wasm_vm",
    2: "context",
    3: "storage",
    4: "object",
    5: "crypto",
    6: "events",
    7: "budget",
    8: "value",
    9: "auth",
}

#: ``ContractExecutableType`` discriminants (Stellar-contract.x).
_SC_EXECUTABLE_WASM = 0
_SC_EXECUTABLE_STELLAR_ASSET = 1

#: ``ContractDataDurability`` discriminants (Stellar-ledger-entries.x).
_DURABILITY_NAMES = {0: "temporary", 1: "persistent"}

#: Strkey version bytes used to present decoded addresses (SEP-23).
_STRKEY_ACCOUNT = 0x30  # "G..." ed25519 account id
_STRKEY_CONTRACT = 0x10  # "C..." contract id

# ---------------------------------------------------------------------------
# Decode limits (defense in depth; inputs are already transport-bounded).
# ---------------------------------------------------------------------------

#: Maximum ``SCVal`` nesting depth accepted before a value is undecodable.
MAX_SCVAL_DEPTH = 16
#: Maximum number of entries in a decoded vec/map/instance-storage collection.
MAX_COLLECTION_ITEMS = 512
#: Byte blobs longer than this are reported by length only (never dumped).
BYTES_REPR_LIMIT = 32


class XdrDecodeError(ValueError):
    """Raised internally when XDR is malformed or structurally undecodable.

    The public decode helpers catch this and return ``{"decoded": False,
    "reason": ...}``; it is never surfaced to callers as an exception.
    """


def _undecodable(reason: str, **extra: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"decoded": False, "reason": reason}
    result.update(extra)
    return result


def _decode_b64(value: str, label: str) -> bytes:
    """Validate-and-decode a base64 XDR value into raw bytes."""
    if not isinstance(value, str) or not value.strip():
        raise XdrDecodeError(f"{label} is empty.")
    try:
        return base64.b64decode(value.strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise XdrDecodeError(f"{label} is not valid base64.") from exc


class _Reader:
    """Big-endian XDR reader with strict bounds checking (RFC 4506)."""

    __slots__ = ("_data", "_pos")

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0

    def remaining(self) -> int:
        return len(self._data) - self._pos

    def _need(self, count: int) -> None:
        if count < 0 or self._pos + count > len(self._data):
            raise XdrDecodeError("XDR value is truncated.")

    def u32(self) -> int:
        self._need(4)
        value = int.from_bytes(self._data[self._pos : self._pos + 4], "big")
        self._pos += 4
        return value

    def i32(self) -> int:
        self._need(4)
        value = int.from_bytes(self._data[self._pos : self._pos + 4], "big", signed=True)
        self._pos += 4
        return value

    def u64(self) -> int:
        self._need(8)
        value = int.from_bytes(self._data[self._pos : self._pos + 8], "big")
        self._pos += 8
        return value

    def i64(self) -> int:
        self._need(8)
        value = int.from_bytes(self._data[self._pos : self._pos + 8], "big", signed=True)
        self._pos += 8
        return value

    def read(self, count: int) -> bytes:
        self._need(count)
        chunk = self._data[self._pos : self._pos + count]
        self._pos += count
        return chunk

    def skip(self, count: int) -> None:
        self._need(count)
        self._pos += count

    def read_bool(self) -> bool:
        # XDR booleans are an enum { FALSE = 0, TRUE = 1 } (4 bytes).
        value = self.u32()
        if value not in (0, 1):
            raise XdrDecodeError("XDR bool must be 0 or 1.")
        return bool(value)

    def read_var_octets(self) -> bytes:
        """Read an ``opaque``/``string`` body (u32 length + padded bytes)."""
        length = self.u32()
        if length > self.remaining():
            raise XdrDecodeError("XDR byte array length exceeds available data.")
        body = self.read(length)
        pad = (4 - (length % 4)) % 4
        self.skip(pad)
        return body

    def ensure_exhausted(self) -> None:
        if self.remaining() != 0:
            raise XdrDecodeError(f"XDR has {self.remaining()} trailing bytes.")


def _expect(value: int, expected: int, label: str) -> None:
    if value != expected:
        raise XdrDecodeError(f"{label} discriminant is {value}, expected {expected}.")


def _read_scstring(reader: _Reader) -> str:
    """Read an XDR ``string`` as UTF-8 text (RFC 4506 string encoding)."""
    raw = reader.read_var_octets()
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise XdrDecodeError("XDR string is not valid UTF-8.") from exc


# ---------------------------------------------------------------------------
# Address decoding
# ---------------------------------------------------------------------------


def _skip_scaddress_payload(reader: _Reader, address_type: int) -> None:
    """Safely skip a non-account/contract ``SCAddress`` payload.

    The payload sizes for the remaining ``SCAddressType`` members are fixed and
    authoritative (Stellar-contract.x): muxed accounts are 40 bytes, claimable
    balances and liquidity pools are opaque 32-byte hashes.
    """
    if address_type == 2:  # SC_ADDRESS_TYPE_MUXED_ACCOUNT
        reader.skip(40)
    elif address_type in (3, 4):  # CLAIMABLE_BALANCE / LIQUIDITY_POOL
        reader.skip(32)
    else:
        raise XdrDecodeError(f"Unknown SCAddress type {address_type}.")


def _read_scaddress(reader: _Reader) -> dict[str, Any]:
    """Decode an ``SCAddress`` union into a strkey-based summary."""
    address_type = reader.u32()
    if address_type == _SC_ADDRESS_CONTRACT:
        raw = reader.read(32)
        return {
            "type": "address",
            "kind": "contract",
            "strkey": strkey_encode(_STRKEY_CONTRACT, raw),
        }
    if address_type == _SC_ADDRESS_ACCOUNT:
        # ``AccountID`` is a ``PublicKey`` union with a single ED25519 arm.
        _expect(reader.u32(), 0, "PublicKeyType")
        raw = reader.read(32)
        return {
            "type": "address",
            "kind": "account",
            "strkey": strkey_encode(_STRKEY_ACCOUNT, raw),
        }
    _skip_scaddress_payload(reader, address_type)
    return {
        "type": "unsupported",
        "variant": "address",
        "reason": f"SCAddress type {address_type} is not rendered",
    }


# ---------------------------------------------------------------------------
# Contract executable decoding
# ---------------------------------------------------------------------------


def _read_contract_executable(reader: _Reader) -> dict[str, Any]:
    """Decode a ``ContractExecutable`` union (wasm hash or stellar asset)."""
    executable_type = reader.u32()
    if executable_type == _SC_EXECUTABLE_WASM:
        return {"type": "wasm", "wasm_hash": reader.read(32).hex()}
    if executable_type == _SC_EXECUTABLE_STELLAR_ASSET:
        return {"type": "stellar_asset"}
    raise XdrDecodeError(f"Unsupported ContractExecutable type {executable_type}.")


# ---------------------------------------------------------------------------
# SCVal decoding
# ---------------------------------------------------------------------------


def _bounded_bytes(raw: bytes) -> dict[str, Any]:
    """Present a decoded byte blob bounded (never dump large blobs)."""
    if len(raw) <= BYTES_REPR_LIMIT:
        return {"type": "bytes", "length": len(raw), "hex": raw.hex()}
    preview = raw[:BYTES_REPR_LIMIT].hex() + "…"
    return {"type": "bytes", "length": len(raw), "hex": preview, "truncated": True}


def _read_scval(reader: _Reader, depth: int) -> dict[str, Any]:
    """Decode a single ``SCVal`` union into a structured representation.

    Every supported variant is read from its authoritative byte layout. Fixed-
    size variants this project does not render are skipped (their sizes are
    authoritative) and reported as ``unsupported``; anything genuinely
    unreadable raises :class:`XdrDecodeError`.
    """
    if depth > MAX_SCVAL_DEPTH:
        raise XdrDecodeError(f"SCVal nesting exceeds {MAX_SCVAL_DEPTH}.")

    disc = reader.u32()
    if disc not in _SCV_NAME_BY_DISC:
        raise XdrDecodeError(f"Unknown SCVal type {disc}.")

    if disc == _SCV["BOOL"]:
        return {"type": "bool", "value": reader.read_bool()}
    if disc == _SCV["VOID"]:
        return {"type": "void"}
    if disc == _SCV["U32"]:
        return {"type": "u32", "value": reader.u32()}
    if disc == _SCV["I32"]:
        return {"type": "i32", "value": reader.i32()}
    if disc == _SCV["U64"]:
        return {"type": "u64", "value": reader.u64()}
    if disc == _SCV["I64"]:
        return {"type": "i64", "value": reader.i64()}
    if disc == _SCV["TIMEPOINT"]:
        return {"type": "timepoint", "value": reader.u64()}
    if disc == _SCV["DURATION"]:
        return {"type": "duration", "value": reader.u64()}
    if disc == _SCV["U128"]:
        return {"type": "u128", "hi": reader.u64(), "lo": reader.u64()}
    if disc == _SCV["I128"]:
        return {"type": "i128", "hi": reader.i64(), "lo": reader.u64()}
    if disc == _SCV["U256"]:
        reader.skip(32)  # UInt256Parts: 4 x u64
        return _unsupported_scval("u256")
    if disc == _SCV["I256"]:
        reader.skip(32)  # Int256Parts: 4 x i64/u64
        return _unsupported_scval("i256")
    if disc == _SCV["BYTES"]:
        return _bounded_bytes(reader.read_var_octets())
    if disc == _SCV["STRING"]:
        return {"type": "string", "value": _read_scstring(reader)}
    if disc == _SCV["SYMBOL"]:
        text = _read_scstring(reader)
        if len(text.encode("utf-8")) > 32:  # SCSYMBOL_LIMIT = 32 (XDR string limit)
            raise XdrDecodeError("SCVal symbol exceeds the 32-byte limit.")
        return {"type": "symbol", "value": text}
    if disc == _SCV["VEC"]:
        return {"type": "vec", "value": _read_scval_collection(reader, depth)}
    if disc == _SCV["MAP"]:
        return {"type": "map", "value": _read_scmap(reader, depth)}
    if disc == _SCV["ADDRESS"]:
        return _read_scaddress(reader)
    if disc == _SCV["CONTRACT_INSTANCE"]:
        return _read_scinstance(reader, depth)
    if disc == _SCV["LEDGER_KEY_CONTRACT_INSTANCE"]:
        return {"type": "ledger_key_contract_instance"}
    if disc == _SCV["LEDGER_KEY_NONCE"]:
        return {"type": "ledger_key_nonce", "nonce": reader.i64()}
    if disc == _SCV["EXECUTABLE_TAG"]:
        _read_scstring(reader)  # tag is read and intentionally not rendered
        return _unsupported_scval("executable_tag")
    if disc == _SCV["ERROR"]:
        return _read_scerror(reader)
    raise XdrDecodeError(f"SCVal type {disc} is not decodable.")


def _unsupported_scval(name: str) -> dict[str, Any]:
    return {"type": "unsupported", "variant": name, "reason": f"SCVal {name} is not rendered"}


def _read_scerror(reader: _Reader) -> dict[str, Any]:
    """Decode an ``SCError`` union (type + code/contract code)."""
    error_type = reader.u32()
    if error_type == 0:  # SCE_CONTRACT carries a user-defined uint32 code.
        return {"type": "error", "error_type": "contract", "contract_code": reader.u32()}
    name = _SC_ERROR_TYPE_NAMES.get(error_type)
    if name is None:
        raise XdrDecodeError(f"Unknown SCError type {error_type}.")
    return {"type": "error", "error_type": name, "code": reader.u32()}


def _read_scval_collection(reader: _Reader, depth: int) -> list[dict[str, Any]]:
    """Decode an ``SCVec`` (an optional pointer followed by ``SCVal[]``).

    ``SCVal.vec``/``SCVal.map`` are declared ``SCVec*``/``SCMap*``: an XDR
    pointer, encoded as a 4-byte 0 (nil) or 1 followed by the pointed-to value.
    """
    pointer = reader.u32()
    if pointer == 0:
        return []
    if pointer != 1:
        raise XdrDecodeError("SCVal collection pointer must be 0 or 1.")
    count = reader.u32()
    if count > MAX_COLLECTION_ITEMS:
        raise XdrDecodeError(f"SCVal collection exceeds {MAX_COLLECTION_ITEMS} items.")
    return [_read_scval(reader, depth + 1) for _ in range(count)]


def _read_scmap(reader: _Reader, depth: int) -> list[dict[str, Any]]:
    """Decode an ``SCMap`` (``SCMapEntry[]`` of key/value ``SCVal`` pairs)."""
    pointer = reader.u32()
    if pointer == 0:
        return []
    if pointer != 1:
        raise XdrDecodeError("SCVal map pointer must be 0 or 1.")
    count = reader.u32()
    if count > MAX_COLLECTION_ITEMS:
        raise XdrDecodeError(f"SCVal map exceeds {MAX_COLLECTION_ITEMS} entries.")
    entries: list[dict[str, Any]] = []
    for _ in range(count):
        key = _read_scval(reader, depth + 1)
        value = _read_scval(reader, depth + 1)
        entries.append({"key": key, "value": value})
    return entries


def _read_scinstance(reader: _Reader, depth: int) -> dict[str, Any]:
    """Decode an ``SCContractInstance`` (executable + optional storage map)."""
    executable = _read_contract_executable(reader)
    storage = _read_scmap(reader, depth) or None
    return {"type": "instance", "executable": executable, "storage": storage}


# ---------------------------------------------------------------------------
# LedgerKey decoding
# ---------------------------------------------------------------------------


def decode_ledger_key(value: str) -> dict[str, Any]:
    """Decode a base64 ``LedgerKey`` into a structured summary.

    Supports account, contract-data, and contract-code keys (the key forms the
    project itself constructs for ``getLedgerEntries``). Other key types are
    reported explicitly as unsupported rather than guessed at.
    """
    try:
        reader = _Reader(_decode_b64(value, "LedgerKey"))
        entry_type = reader.u32()
        name = _LEDGER_ENTRY_NAMES.get(entry_type)

        if name == "ACCOUNT":
            _expect(reader.u32(), 0, "PublicKeyType")  # ED25519
            account = strkey_encode(_STRKEY_ACCOUNT, reader.read(32))
            reader.ensure_exhausted()
            return {"type": name, "decoded": True, "detail": {"account": account}}
        if name == "CONTRACT_DATA":
            contract = _read_scaddress(reader)
            key = _read_scval(reader, 0)
            durability = reader.u32()
            if durability not in _DURABILITY_NAMES:
                raise XdrDecodeError(f"Unknown ContractDataDurability {durability}.")
            reader.ensure_exhausted()
            contract_id = contract.get("strkey") if contract.get("kind") == "contract" else None
            return {
                "type": name,
                "decoded": True,
                "detail": {
                    "contract": contract,
                    "contract_id": contract_id,
                    "key": key,
                    "durability": _DURABILITY_NAMES[durability],
                },
            }
        if name == "CONTRACT_CODE":
            wasm_hash = reader.read(32).hex()
            reader.ensure_exhausted()
            return {"type": name, "decoded": True, "detail": {"wasm_hash": wasm_hash}}

        if name is None:
            return _undecodable(f"Unknown LedgerKey type {entry_type}.", type="UNKNOWN")
        return _undecodable(f"LedgerKey type {name} is not decoded.", type=name)
    except XdrDecodeError as exc:
        return _undecodable(str(exc))


# ---------------------------------------------------------------------------
# LedgerEntryData decoding (the ``xdr`` field of a getLedgerEntries result)
# ---------------------------------------------------------------------------


def _read_contract_data_entry(reader: _Reader) -> dict[str, Any]:
    """Decode a ``ContractDataEntry`` (CONTRACT_DATA ledger entry data)."""
    # The entry begins with an ``ExtensionPoint`` union; only case 0 exists.
    _expect(reader.u32(), 0, "ContractDataEntry extension")
    contract = _read_scaddress(reader)
    key = _read_scval(reader, 0)
    durability = reader.u32()
    if durability not in _DURABILITY_NAMES:
        raise XdrDecodeError(f"Unknown ContractDataDurability {durability}.")
    value = _read_scval(reader, 0)
    reader.ensure_exhausted()
    contract_id = contract.get("strkey") if contract.get("kind") == "contract" else None
    return {
        "contract": contract,
        "contract_id": contract_id,
        "key": key,
        "durability": _DURABILITY_NAMES[durability],
        "value": value,
    }


def _read_contract_code_entry(reader: _Reader) -> dict[str, Any]:
    """Decode a ``ContractCodeEntry`` (CONTRACT_CODE ledger entry data).

    The wasm ``code<>`` body is never copied: its declared length (an
    authoritative XDR field) is reported as the byte size and the bytes are
    skipped. If the surrounding buffer is clipped before the whole body (as can
    happen for very large wasm through a bounded transport), the hash and byte
    size are still surfaced with ``code_truncated: True``.
    """
    ext_version = reader.u32()  # ContractCodeEntry ``ext`` union discriminant.
    cost_inputs: dict[str, int] | None = None
    if ext_version == 1:
        _expect(reader.u32(), 0, "ContractCodeCostInputs extension")
        field_names = (
            "nInstructions",
            "nFunctions",
            "nGlobals",
            "nTableEntries",
            "nTypes",
            "nDataSegments",
            "nElemSegments",
            "nImports",
            "nExports",
            "nDataSegmentBytes",
        )
        cost_inputs = {name: reader.u32() for name in field_names}
    elif ext_version != 0:
        raise XdrDecodeError(f"Unknown ContractCodeEntry extension {ext_version}.")

    wasm_hash = reader.read(32).hex()
    code_length = reader.u32()
    if code_length > reader.remaining():
        # Bounded/clipped transport: metadata above is authoritative, code is not.
        reader.skip(reader.remaining())
        return {
            "wasm_hash": wasm_hash,
            "code_byte_size": code_length,
            "ext_version": ext_version,
            "cost_inputs": cost_inputs,
            "code_truncated": True,
        }
    pad = (4 - (code_length % 4)) % 4
    reader.skip(code_length + pad)
    reader.ensure_exhausted()
    return {
        "wasm_hash": wasm_hash,
        "code_byte_size": code_length,
        "ext_version": ext_version,
        "cost_inputs": cost_inputs,
        "code_truncated": False,
    }


def decode_ledger_entry_data(value: str) -> dict[str, Any]:
    """Decode a base64 ``LedgerEntryData`` value (an RPC entry ``xdr`` field).

    Returns ``{"decoded": True, "type": ..., "detail": {...}}`` for the
    supported contract-data/contract-code entries and an explicit
    ``{"decoded": False, "reason": ...}`` result for unsupported or malformed
    XDR. Never raises and never fabricates values.
    """
    try:
        reader = _Reader(_decode_b64(value, "LedgerEntryData"))
        entry_type = reader.u32()
        name = _LEDGER_ENTRY_NAMES.get(entry_type)

        if name == "CONTRACT_DATA":
            return {
                "decoded": True,
                "type": name,
                "detail": _read_contract_data_entry(reader),
            }
        if name == "CONTRACT_CODE":
            return {
                "decoded": True,
                "type": name,
                "detail": _read_contract_code_entry(reader),
            }
        if name is None:
            return _undecodable(f"Unknown LedgerEntryData type {entry_type}.", type="UNKNOWN")
        return _undecodable(f"Ledger entry type {name} has no structured decoder.", type=name)
    except XdrDecodeError as exc:
        return _undecodable(str(exc))
    except StrkeyError as exc:
        return _undecodable(f"Could not render an embedded address: {exc}")


def decode_scval_xdr(value: str) -> dict[str, Any]:
    """Decode a standalone base64 ``SCVal`` (e.g. an event topic/value)."""
    try:
        reader = _Reader(_decode_b64(value, "SCVal"))
        result = _read_scval(reader, 0)
        reader.ensure_exhausted()
        if result.get("type") == "unsupported":
            return _undecodable(str(result.get("reason", "SCVal is not rendered.")))
        result["decoded"] = True
        return result
    except XdrDecodeError as exc:
        return _undecodable(str(exc))
    except StrkeyError as exc:
        return _undecodable(f"Could not render an embedded address: {exc}")


# ---------------------------------------------------------------------------
# Transaction envelope decoding (Stellar-transaction.x, current protocol)
# ---------------------------------------------------------------------------

#: ``EnvelopeType`` discriminants relevant to developer transaction inspection.
_ENVELOPE_TYPE_TX_V0 = 0
_ENVELOPE_TYPE_TX = 2
_ENVELOPE_TYPE_TX_FEE_BUMP = 5
_ENVELOPE_NAMES: dict[int, str] = {
    0: "ENVELOPE_TYPE_TX_V0",
    2: "ENVELOPE_TYPE_TX",
    5: "ENVELOPE_TYPE_TX_FEE_BUMP",
}

#: ``OperationType`` discriminants (Stellar-transaction.x).
_OPERATION_NAMES: dict[int, str] = {
    0: "CREATE_ACCOUNT",
    1: "PAYMENT",
    2: "PATH_PAYMENT_STRICT_RECEIVE",
    3: "MANAGE_SELL_OFFER",
    4: "CREATE_PASSIVE_SELL_OFFER",
    5: "SET_OPTIONS",
    6: "CHANGE_TRUST",
    7: "ALLOW_TRUST",
    8: "ACCOUNT_MERGE",
    9: "INFLATION",
    10: "MANAGE_DATA",
    11: "BUMP_SEQUENCE",
    12: "MANAGE_BUY_OFFER",
    13: "PATH_PAYMENT_STRICT_SEND",
    14: "CREATE_CLAIMABLE_BALANCE",
    15: "CLAIM_CLAIMABLE_BALANCE",
    16: "BEGIN_SPONSORING_FUTURE_RESERVES",
    17: "END_SPONSORING_FUTURE_RESERVES",
    18: "REVOKE_SPONSORSHIP",
    19: "CLAWBACK",
    20: "CLAWBACK_CLAIMABLE_BALANCE",
    21: "SET_TRUST_LINE_FLAGS",
    22: "LIQUIDITY_POOL_DEPOSIT",
    23: "LIQUIDITY_POOL_WITHDRAW",
    24: "INVOKE_HOST_FUNCTION",
    25: "EXTEND_FOOTPRINT_TTL",
    26: "RESTORE_FOOTPRINT",
}

#: ``CryptoKeyType`` discriminants (Stellar-transaction.x / Stellar-SCP.x).
_KEY_TYPE_ED25519 = 0
_KEY_TYPE_PRE_AUTH_TX = 1
_KEY_TYPE_HASH_X = 2
_KEY_TYPE_ED25519_SIGNED_PAYLOAD = 3
_KEY_TYPE_MUXED_ED25519 = 256

#: ``MemoType`` discriminants.
_MEMO_NAMES = {0: "none", 1: "text", 2: "id", 3: "hash", 4: "return"}

#: ``PreconditionType`` discriminants.
_PRECOND_NONE = 0
_PRECOND_TIME = 1
_PRECOND_V2 = 2

#: ``AssetType`` discriminants.
_ASSET_NATIVE = 0
_ASSET_CREDIT_ALPHANUM4 = 1
_ASSET_CREDIT_ALPHANUM12 = 2

#: ``HostFunctionType`` discriminants.
_HOST_FN_INVOKE_CONTRACT = 0
_HOST_FN_CREATE_CONTRACT = 1
_HOST_FN_UPLOAD_WASM = 2
_HOST_FN_CREATE_CONTRACT_V2 = 3

#: ``ContractIDPreimageType`` discriminants.
_CONTRACT_ID_PREIMAGE_FROM_ADDRESS = 0
_CONTRACT_ID_PREIMAGE_FROM_ASSET = 1

#: ``SorobanCredentialsType`` discriminants.
_CREDENTIALS_SOURCE_ACCOUNT = 0
_CREDENTIALS_ADDRESS = 1
_CREDENTIALS_ACCOUNT = 2

#: ``SorobanAuthorizedFunctionType`` discriminants.
_AUTHORIZED_FN_CONTRACT = 0
_AUTHORIZED_FN_CREATE_CONTRACT = 1

#: Strkey version bytes used to present extra signer keys (SEP-23).
_STRKEY_PRE_AUTH_TX = 0x98  # "T..."
_STRKEY_HASH_X = 0xB8  # "X..."
_STRKEY_MUXED = 0x60  # "M..."

#: Decode limits for transaction structures (defense in depth).
MAX_TX_OPERATIONS = 256
MAX_TX_SIGNATURES = 128
MAX_AUTH_ENTRIES = 64
MAX_AUTH_DEPTH = 8
MAX_SUBINVOCATIONS = 128
MAX_EXTRA_SIGNERS = 16
_SIGNATURE_MAX_BYTES = 64  # XDR ``Signature`` is ``opaque<64>``.


def _lumens(stroops: int) -> str:
    """Format an int64 stroop amount as an exact decimal lumen string."""
    if not stroops:
        return "0"
    value = Decimal(stroops) / Decimal(10**7)
    if value == value.to_integral_value():
        return format(value.quantize(Decimal(1)), "f")
    return format(value.normalize(), "f")


def _amount(stroops: int) -> dict[str, Any]:
    """Present an amount in stroops together with its lumen equivalent."""
    return {"stroops": stroops, "lumens": _lumens(stroops)}


def _skip_opaque(reader: _Reader) -> int:
    """Skip an XDR ``opaque``/``string`` body, returning its byte length."""
    length = reader.u32()
    if length > reader.remaining():
        raise XdrDecodeError("XDR byte array length exceeds available data.")
    pad = (4 - (length % 4)) % 4
    reader.skip(length + pad)
    return length


def _read_account_id(reader: _Reader) -> str:
    """Decode an ``AccountID`` (a ``PublicKey`` union) into a ``G`` strkey."""
    _expect(reader.u32(), 0, "PublicKeyType")  # ED25519
    return strkey_encode(_STRKEY_ACCOUNT, reader.read(32))


def _read_muxed_account(reader: _Reader) -> dict[str, Any]:
    """Decode a ``MuxedAccount`` into a strkey-based summary."""
    key_type = reader.u32()
    if key_type == _KEY_TYPE_ED25519:
        payload = reader.read(32)
        return {
            "type": "ed25519",
            "account": strkey_encode(_STRKEY_ACCOUNT, payload),
        }
    if key_type == _KEY_TYPE_MUXED_ED25519:
        account_id = reader.u64()
        payload = reader.read(32)
        account = strkey_encode(_STRKEY_ACCOUNT, payload)
        muxed = strkey_encode(_STRKEY_MUXED, account_id.to_bytes(8, "big") + payload)
        return {"type": "muxed", "id": account_id, "account": account, "muxed_account": muxed}
    raise XdrDecodeError(f"Unsupported MuxedAccount key type {key_type}.")


def _read_optional_muxed(reader: _Reader) -> dict[str, Any] | None:
    """Decode an optional ``MuxedAccount*`` (an Operation source account)."""
    pointer = reader.u32()
    if pointer == 0:
        return None
    if pointer != 1:
        raise XdrDecodeError("Operation source-account pointer must be 0 or 1.")
    return _read_muxed_account(reader)


def _read_memo(reader: _Reader) -> dict[str, Any]:
    """Decode a ``Memo`` union into a bounded summary."""
    memo_type = reader.u32()
    if memo_type == 0:
        return {"type": "none"}
    if memo_type == 1:  # MEMO_TEXT string<28>
        raw = reader.read_var_octets()
        if len(raw) > 28:
            raise XdrDecodeError("Text memo exceeds the 28-byte limit.")
        try:
            return {"type": "text", "value": raw.decode("utf-8")}
        except UnicodeDecodeError:
            return {"type": "text", "value": _bounded_bytes(raw)}
    if memo_type == 2:
        return {"type": "id", "value": reader.u64()}
    if memo_type in (3, 4):  # MEMO_HASH / MEMO_RETURN (32-byte hashes)
        raw = reader.read(32)
        name = "hash" if memo_type == 3 else "return"
        return {"type": name, "hex": raw.hex()}
    raise XdrDecodeError(f"Unsupported Memo type {memo_type}.")


def _read_asset(reader: _Reader) -> dict[str, Any]:
    """Decode an ``Asset`` union into a bounded summary."""
    asset_type = reader.u32()
    if asset_type == _ASSET_NATIVE:
        return {"type": "native"}
    if asset_type in (_ASSET_CREDIT_ALPHANUM4, _ASSET_CREDIT_ALPHANUM12):
        code_bytes = reader.read(4 if asset_type == _ASSET_CREDIT_ALPHANUM4 else 12)
        issuer = _read_account_id(reader)
        try:
            code = code_bytes.rstrip(b"\x00").decode("ascii")
        except UnicodeDecodeError:
            code = code_bytes.hex()
        kind = "alphanum4" if asset_type == _ASSET_CREDIT_ALPHANUM4 else "alphanum12"
        return {"type": kind, "code": code, "issuer": issuer}
    raise XdrDecodeError(f"Unsupported Asset type {asset_type}.")


def _read_time_bounds(reader: _Reader) -> dict[str, Any]:
    return {"min_time": reader.u64(), "max_time": reader.u64()}


def _read_ledger_bounds(reader: _Reader) -> dict[str, Any]:
    return {"min_ledger": reader.u32(), "max_ledger": reader.u32()}


def _read_extra_signer(reader: _Reader) -> dict[str, Any]:
    """Decode a ``SignerKey`` union into a strkey-based summary."""
    key_type = reader.u32()
    if key_type == _KEY_TYPE_ED25519:
        return {"type": "ed25519", "account": strkey_encode(_STRKEY_ACCOUNT, reader.read(32))}
    if key_type == _KEY_TYPE_PRE_AUTH_TX:
        return {
            "type": "pre_auth_tx",
            "strkey": strkey_encode(_STRKEY_PRE_AUTH_TX, reader.read(32)),
        }
    if key_type == _KEY_TYPE_HASH_X:
        return {"type": "hash_x", "strkey": strkey_encode(_STRKEY_HASH_X, reader.read(32))}
    if key_type == _KEY_TYPE_ED25519_SIGNED_PAYLOAD:
        account = _read_account_id(reader)
        payload = reader.read_var_octets()
        return {
            "type": "signed_payload",
            "account": account,
            "payload": _bounded_bytes(payload),
        }
    raise XdrDecodeError(f"Unsupported SignerKey type {key_type}.")


def _read_preconditions(reader: _Reader) -> dict[str, Any]:
    """Decode the ``Preconditions`` union into a bounded summary."""
    precondition_type = reader.u32()
    if precondition_type == _PRECOND_NONE:
        return {"type": "none"}
    if precondition_type == _PRECOND_TIME:
        return {"type": "time", "time_bounds": _read_time_bounds(reader)}
    if precondition_type == _PRECOND_V2:
        time_bounds = None
        pointer = reader.u32()
        if pointer not in (0, 1):
            raise XdrDecodeError("PreconditionsV2 time-bounds pointer must be 0 or 1.")
        if pointer == 1:
            time_bounds = _read_time_bounds(reader)

        ledger_bounds = None
        pointer = reader.u32()
        if pointer not in (0, 1):
            raise XdrDecodeError("PreconditionsV2 ledger-bounds pointer must be 0 or 1.")
        if pointer == 1:
            ledger_bounds = _read_ledger_bounds(reader)

        min_sequence = None
        pointer = reader.u32()
        if pointer not in (0, 1):
            raise XdrDecodeError("PreconditionsV2 min-sequence pointer must be 0 or 1.")
        if pointer == 1:
            min_sequence = reader.i64()

        min_sequence_age = None
        pointer = reader.u32()
        if pointer not in (0, 1):
            raise XdrDecodeError("PreconditionsV2 min-sequence-age pointer must be 0 or 1.")
        if pointer == 1:
            min_sequence_age = reader.i64()

        min_sequence_ledger_gap = reader.u32()
        extra_signers: list[dict[str, Any]] = []
        count = reader.u32()
        if count > MAX_EXTRA_SIGNERS:
            raise XdrDecodeError(f"Extra signers exceed {MAX_EXTRA_SIGNERS}.")
        for _ in range(count):
            extra_signers.append(_read_extra_signer(reader))
        return {
            "type": "v2",
            "time_bounds": time_bounds,
            "ledger_bounds": ledger_bounds,
            "min_sequence": min_sequence,
            "min_sequence_age": min_sequence_age,
            "min_sequence_ledger_gap": min_sequence_ledger_gap,
            "extra_signers": extra_signers,
        }
    raise XdrDecodeError(f"Unsupported Preconditions type {precondition_type}.")


def _read_contract_id_preimage(reader: _Reader) -> dict[str, Any]:
    """Decode a ``ContractIDPreimage`` union."""
    preimage_type = reader.u32()
    if preimage_type == _CONTRACT_ID_PREIMAGE_FROM_ADDRESS:
        address = _read_scaddress(reader)
        return {"type": "from_address", "address": address, "salt": reader.read(32).hex()}
    if preimage_type == _CONTRACT_ID_PREIMAGE_FROM_ASSET:
        return {"type": "from_asset", "asset": _read_asset(reader)}
    raise XdrDecodeError(f"Unsupported ContractIDPreimage type {preimage_type}.")


def _read_scval_array_count(reader: _Reader, depth: int) -> int:
    """Consume a plain ``SCVal<>`` vector, returning its length (bounded)."""
    count = reader.u32()
    if count > MAX_COLLECTION_ITEMS:
        raise XdrDecodeError(f"SCVal vector exceeds {MAX_COLLECTION_ITEMS} items.")
    for _ in range(count):
        _read_scval(reader, depth + 1)
    return count


def _read_authorized_function(reader: _Reader, depth: int) -> dict[str, Any]:
    """Decode a ``SorobanAuthorizedFunction`` union into a compact summary.

    The argument ``SCVal``s of a contract function are decoded (so their bytes
    are consumed exactly) but not echoed — only their count is surfaced.
    """
    function_type = reader.u32()
    if function_type == _AUTHORIZED_FN_CONTRACT:
        contract = _read_scaddress(reader)
        name = _read_scstring(reader)
        if len(name.encode("utf-8")) > 32:
            raise XdrDecodeError("Soroban function name exceeds the 32-byte limit.")
        args_count = _read_scval_array_count(reader, depth)
        return {
            "function": "contract_fn",
            "contract": contract,
            "function_name": name,
            "args_count": args_count,
        }
    if function_type == _AUTHORIZED_FN_CREATE_CONTRACT:
        preimage = _read_contract_id_preimage(reader)
        executable = _read_contract_executable(reader)
        return {
            "function": "create_contract",
            "preimage": {"type": preimage.get("type")},
            "executable": executable,
        }
    raise XdrDecodeError(f"Unsupported SorobanAuthorizedFunction type {function_type}.")


def _read_authorized_invocation(reader: _Reader, depth: int) -> dict[str, Any]:
    """Decode a ``SorobanAuthorizedInvocation`` into a compact summary."""
    if depth > MAX_AUTH_DEPTH:
        raise XdrDecodeError(f"Soroban invocation nesting exceeds {MAX_AUTH_DEPTH}.")
    function = _read_authorized_function(reader, depth)
    sub_invocations: list[dict[str, Any]] = []
    count = reader.u32()
    if count > MAX_SUBINVOCATIONS:
        raise XdrDecodeError(f"Soroban sub-invocations exceed {MAX_SUBINVOCATIONS}.")
    for _ in range(count):
        sub_invocations.append(_read_authorized_invocation(reader, depth + 1))
    return {"function": function, "sub_invocations": sub_invocations}


def _read_soroban_credentials(reader: _Reader) -> dict[str, Any]:
    """Decode ``SorobanCredentials`` (signature bytes are never dumped)."""
    credentials_type = reader.u32()
    if credentials_type == _CREDENTIALS_SOURCE_ACCOUNT:
        return {"type": "source_account"}
    if credentials_type == _CREDENTIALS_ADDRESS:
        address = _read_scaddress(reader)
        return {"type": "address", "address": address, "nonce": reader.i64()}
    if credentials_type == _CREDENTIALS_ACCOUNT:
        account = _read_account_id(reader)
        signature_length = _skip_opaque(reader)
        return {
            "type": "account",
            "account": account,
            "signature_length": signature_length,
        }
    raise XdrDecodeError(f"Unsupported SorobanCredentials type {credentials_type}.")


def _decode_host_function(reader: _Reader) -> dict[str, Any]:
    """Decode the ``HostFunction`` union of an INVOKE_HOST_FUNCTION operation."""
    function_type = reader.u32()
    if function_type == _HOST_FN_INVOKE_CONTRACT:
        contract = _read_scaddress(reader)
        args_count = _read_scval_array_count(reader, 1)
        return {
            "type": "invoke_contract",
            "contract": contract,
            "args_count": args_count,
        }
    if function_type == _HOST_FN_CREATE_CONTRACT:
        preimage = _read_contract_id_preimage(reader)
        executable = _read_contract_executable(reader)
        constructor_count = _read_scval_array_count(reader, 1)
        return {
            "type": "create_contract",
            "preimage": preimage,
            "executable": executable,
            "constructor_args_count": constructor_count,
        }
    if function_type == _HOST_FN_UPLOAD_WASM:
        return {"type": "upload_wasm", "wasm_byte_size": _skip_opaque(reader)}
    if function_type == _HOST_FN_CREATE_CONTRACT_V2:
        preimage = _read_contract_id_preimage(reader)
        executable = _read_contract_executable(reader)
        constructor_count = _read_scval_array_count(reader, 1)
        return {
            "type": "create_contract_v2",
            "preimage": preimage,
            "executable": executable,
            "constructor_args_count": constructor_count,
        }
    raise XdrDecodeError(f"Unsupported HostFunction type {function_type}.")


def _decode_operation_payload(reader: _Reader, operation_type: int) -> dict[str, Any]:
    """Decode the body of a supported operation type (read-only)."""
    if operation_type == 0:  # CREATE_ACCOUNT
        destination = _read_account_id(reader)
        starting_balance = reader.i64()
        return {
            "destination": destination,
            "starting_balance": _amount(starting_balance),
        }
    if operation_type == 1:  # PAYMENT
        destination = _read_muxed_account(reader)
        asset = _read_asset(reader)
        amount = reader.i64()
        return {"destination": destination, "asset": asset, "amount": _amount(amount)}
    if operation_type == 6:  # CHANGE_TRUST
        line = _read_asset(reader)
        limit = reader.i64()
        ext_version = reader.u32()
        if ext_version == 1:
            liquidity_pool = {
                "asset_a": _read_asset(reader),
                "asset_b": _read_asset(reader),
                "fee_bps": reader.i32(),
            }
        elif ext_version == 0:
            liquidity_pool = None
        else:
            raise XdrDecodeError(f"Unsupported ChangeTrust extension {ext_version}.")
        return {"line": line, "limit": _amount(limit), "liquidity_pool": liquidity_pool}
    if operation_type == 11:  # BUMP_SEQUENCE
        return {"bump_to": reader.i64()}
    if operation_type == 24:  # INVOKE_HOST_FUNCTION
        host_function = _decode_host_function(reader)
        auth_count = reader.u32()
        if auth_count > MAX_AUTH_ENTRIES:
            raise XdrDecodeError(f"Soroban auth entries exceed {MAX_AUTH_ENTRIES}.")
        auth: list[dict[str, Any]] = []
        for _ in range(auth_count):
            credentials = _read_soroban_credentials(reader)
            invocation = _read_authorized_invocation(reader, 1)
            auth.append({"credentials": credentials, "root_invocation": invocation})
        return {"host_function": host_function, "auth_count": auth_count, "auth": auth}
    if operation_type == 25:  # EXTEND_FOOTPRINT_TTL
        return {"extend_to": reader.u32()}
    if operation_type == 26:  # RESTORE_FOOTPRINT (no payload)
        return {}
    raise XdrDecodeError(f"Operation type {operation_type} has no decoder.")


def _decode_operations(
    reader: _Reader, declared: int
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Decode an ``Operation<>`` vector; stop cleanly at the first unsupported op.

    Returns ``(operations, parsing)``. Because each operation type has its own
    variable-length layout, an unsupported operation cannot be skipped: decoding
    stops at that index and the remaining operations (and the signature vector
    that follows the transaction) are honestly marked as not parsed.
    """
    operations: list[dict[str, Any]] = []
    note: str | None = None
    stopped_at: int | None = None
    for index in range(declared):
        source_account = _read_optional_muxed(reader)
        operation_type = reader.u32()
        name = _OPERATION_NAMES.get(operation_type)
        if name is None:
            stopped_at = index
            note = f"Operation type {operation_type} is unknown and not parsed."
            operations.append(
                {
                    "index": index,
                    "decoded": False,
                    "type": "UNKNOWN",
                    "reason": note,
                }
            )
            break
        if operation_type not in (0, 1, 6, 11, 24, 25, 26):
            stopped_at = index
            note = (
                f"Operation type {name} is not decoded; the operations after it " "were not parsed."
            )
            operations.append(
                {
                    "index": index,
                    "decoded": False,
                    "type": name,
                    "reason": note,
                }
            )
            break
        payload = _decode_operation_payload(reader, operation_type)
        operations.append(
            {
                "index": index,
                "decoded": True,
                "type": name,
                "source_account": source_account,
                "payload": payload,
            }
        )
    if stopped_at is None:
        parsing = {"complete": True, "declared": declared, "decoded": declared, "note": None}
    else:
        parsing = {
            "complete": False,
            "declared": declared,
            "decoded": stopped_at,
            "note": note,
        }
    return operations, parsing


def _decode_signatures(reader: _Reader) -> list[dict[str, Any]]:
    """Decode a ``DecoratedSignature<>`` vector (hints + bounded signatures)."""
    count = reader.u32()
    if count > MAX_TX_SIGNATURES:
        raise XdrDecodeError(f"Transaction signatures exceed {MAX_TX_SIGNATURES}.")
    signatures: list[dict[str, Any]] = []
    for _ in range(count):
        hint = reader.read(4).hex()
        signature = reader.read_var_octets()
        if len(signature) > _SIGNATURE_MAX_BYTES:
            raise XdrDecodeError("Signature exceeds the 64-byte limit.")
        signatures.append({"hint": hint, "signature_hex": signature.hex()})
    return signatures


def _decode_transaction(reader: _Reader, *, v0: bool) -> dict[str, Any]:
    """Decode a ``Transaction``/``TransactionV0`` body (read-only)."""
    if v0:
        source_account = {
            "type": "ed25519",
            "account": strkey_encode(_STRKEY_ACCOUNT, reader.read(32)),
        }
    else:
        source_account = _read_muxed_account(reader)

    fee = reader.u32()
    sequence = reader.i64()

    if v0:
        preconditions: dict[str, Any] = {"type": "none"}
        pointer = reader.u32()
        if pointer not in (0, 1):
            raise XdrDecodeError("V0 time-bounds pointer must be 0 or 1.")
        if pointer == 1:
            preconditions = {"type": "time", "time_bounds": _read_time_bounds(reader)}
    else:
        preconditions = _read_preconditions(reader)

    memo = _read_memo(reader)
    declared_ops = reader.u32()
    if declared_ops > MAX_TX_OPERATIONS:
        raise XdrDecodeError(f"Transaction operations exceed {MAX_TX_OPERATIONS}.")
    operations, operation_parsing = _decode_operations(reader, declared_ops)

    signatures: list[dict[str, Any]] | None = None
    signature_parsing: dict[str, Any] = {"complete": True, "note": None}
    if operation_parsing["complete"]:
        ext_version = reader.u32()
        if ext_version != 0:
            raise XdrDecodeError(f"Unsupported transaction extension {ext_version}.")
        signatures = _decode_signatures(reader)
    else:
        signature_parsing = {
            "complete": False,
            "note": "Signatures were not parsed because an operation could not be decoded.",
        }

    return {
        "source_account": source_account,
        "fee": _amount(fee),
        "sequence": sequence,
        "preconditions": preconditions,
        "memo": memo,
        "operations": operations,
        "operation_parsing": operation_parsing,
        "signatures": signatures,
        "signature_parsing": signature_parsing,
    }


def _decode_fee_bump(reader: _Reader) -> dict[str, Any]:
    """Decode a ``FeeBumpTransaction`` plus its (single-level) inner envelope."""
    fee_source = _read_muxed_account(reader)
    fee = reader.i64()
    inner_type = reader.u32()
    if inner_type != _ENVELOPE_TYPE_TX:
        raise XdrDecodeError(f"Fee-bump inner envelope must be V1, got {inner_type}.")
    inner = _decode_transaction(reader, v0=False)
    signatures = _decode_signatures(reader)
    reader.ensure_exhausted()
    return {
        "fee_source": fee_source,
        "fee": _amount(fee),
        "inner_transaction": inner,
        "signatures": signatures,
        "signature_parsing": {"complete": True, "note": None},
    }


def decode_transaction_envelope(value: str) -> dict[str, Any]:
    """Decode a base64 ``TransactionEnvelope`` into a structured, read-only view.

    Supported envelopes: V1 (``ENVELOPE_TYPE_TX``), legacy V0
    (``ENVELOPE_TYPE_TX_V0``), and fee-bump (``ENVELOPE_TYPE_TX_FEE_BUMP``).
    Common operations (payment, create account, change trust, bump sequence,
    invoke host function, extend footprint TTL, restore footprint) are decoded;
    any other operation type is reported explicitly as unsupported and the
    decode of that transaction's remaining operations/signatures is honestly
    marked as not parsed. Malformed or truncated XDR returns ``decoded: False``
    with a reason. This function never raises and never fabricates values.
    """
    try:
        reader = _Reader(_decode_b64(value, "TransactionEnvelope"))
        envelope_type = reader.u32()
        name = _ENVELOPE_NAMES.get(envelope_type)
        if name is None:
            return _undecodable(
                f"Unknown TransactionEnvelope type {envelope_type}.", type="UNKNOWN"
            )
        if envelope_type == _ENVELOPE_TYPE_TX_FEE_BUMP:
            detail = _decode_fee_bump(reader)
        else:
            detail = _decode_transaction(reader, v0=envelope_type == _ENVELOPE_TYPE_TX_V0)
            if detail["signature_parsing"]["complete"]:
                reader.ensure_exhausted()
        result: dict[str, Any] = {"envelope_type": name}
        result.update(detail)
        result["decoded"] = True
        return result
    except XdrDecodeError as exc:
        return _undecodable(str(exc))
    except StrkeyError as exc:
        return _undecodable(f"Could not render an embedded address: {exc}")
