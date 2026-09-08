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
``decode_scval_xdr``) never raise: each returns a dict whose ``decoded`` key is
``True`` on success and ``False`` with a ``reason`` otherwise.
"""

from __future__ import annotations

import base64
import binascii
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
