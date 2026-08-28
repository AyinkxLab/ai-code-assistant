"""Minimal Stellar strkey and ledger-key XDR encoding (read-only).

This module implements just enough of the Stellar primitives to support the
read-only Soroban/Stellar inspection features:

* **Stellar strkeys** (SEP-23): ``base32(version_byte || payload || crc16)``
  used for account ids (``G...``), contract ids (``C...``) and friends, with
  full CRC16-XMODEM checksum verification.
* **LedgerKey XDR**: the subset of ``LedgerKey`` encodings needed to query the
  live ledger with the RPC ``getLedgerEntries`` method (account, contract-data
  instance, and contract-code keys).

Values and discriminants are taken from the authoritative Stellar XDR
(``stellar/stellar-xdr``, v22) and the RPC documentation. Every encoder is
verified against known-good fixture values in the test suite; nothing here
decodes user data, signs, or submits transactions.

XDR notes (big-endian): enums/discriminants are ``int32``; ``Hash`` is 32 raw
bytes; the relevant values are::

    LedgerEntryType:            ACCOUNT = 0, CONTRACT_DATA = 6, CONTRACT_CODE = 7
    ContractDataDurability:     TEMPORARY = 0, PERSISTENT = 1
    SCAddressType:              ACCOUNT = 0, CONTRACT = 1
    SCValType:                  SCV_SYMBOL = 15, SCV_LEDGER_KEY_CONTRACT_INSTANCE = 20

Strkey version bytes (upper 5 bits select the first base32 character):
``G`` = 0x30, ``C`` = 0x10, ``M`` = 0x60, ``N`` = 0x18.
"""

from __future__ import annotations

import base64
import binascii
import hashlib  # noqa: F401  (documented intent; used by crc16 helpers in tests)

_STRKEY_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
_STRKEY_ALPHABET_INDEX = {char: i for i, char in enumerate(_STRKEY_ALPHABET)}

#: version byte -> first strkey character
_STRKEY_VERSION_BY_CHAR = {
    "G": 0x30,  # ed25519 account id
    "C": 0x10,  # contract id
    "M": 0x60,  # muxed account
    "S": 0x90,  # seed
    "T": 0x98,  # pre-authorized transaction
    "X": 0xB8,  # hash-x
    "P": 0x78,  # signed payload
    "N": 0x18,  # soroban nonce
}
_CHAR_BY_VERSION = {version: char for char, version in _STRKEY_VERSION_BY_CHAR.items()}

#: Minimum and maximum sizes (in bytes) accepted for a base64 ledger key.
_LEDGER_KEY_MIN_BYTES = 32
_LEDGER_KEY_MAX_BYTES = 2048


class StrkeyError(ValueError):
    """Raised when a strkey cannot be decoded or fails checksum validation."""


def _crc16_xmodem(data: bytes) -> int:
    """Return the CRC16-XMODEM checksum of ``data`` (poly 0x1021, init 0)."""
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if (crc & 0x8000) else (crc << 1)
            crc &= 0xFFFF
    return crc


# ---------------------------------------------------------------------------
# Base32 (RFC 4648, no padding) — the strkey wire format
# ---------------------------------------------------------------------------


def _base32_encode(data: bytes) -> str:
    """Encode ``data`` with Stellar base32 (no padding)."""
    bits = 0
    value = 0
    output: list[str] = []
    for byte in data:
        value = (value << 8) | byte
        bits += 8
        while bits >= 5:
            output.append(_STRKEY_ALPHABET[(value >> (bits - 5)) & 0x1F])
            bits -= 5
            value &= (1 << bits) - 1
    if bits:
        output.append(_STRKEY_ALPHABET[(value << (5 - bits)) & 0x1F])
    return "".join(output)


def _base32_decode(text: str) -> bytes:
    """Decode Stellar base32 (no padding), raising on invalid characters."""
    if not isinstance(text, str) or not text:
        raise StrkeyError("Empty or non-string strkey.")
    bits = 0
    value = 0
    output = bytearray()
    for char in text:
        if char not in _STRKEY_ALPHABET_INDEX:
            raise StrkeyError(f"Invalid base32 character: {char!r}")
        value = (value << 5) | _STRKEY_ALPHABET_INDEX[char]
        bits += 5
        if bits >= 8:
            output.append((value >> (bits - 8)) & 0xFF)
            bits -= 8
            value &= (1 << bits) - 1
    return bytes(output)


# ---------------------------------------------------------------------------
# Strkey encode / decode (SEP-23)
# ---------------------------------------------------------------------------


def strkey_encode(version_byte: int, payload: bytes) -> str:
    """Encode ``payload`` with ``version_byte`` into a Stellar strkey string."""
    if not isinstance(payload, bytes) or not payload:
        raise StrkeyError("A non-empty payload is required.")
    data = bytes([version_byte]) + payload
    checksum = _crc16_xmodem(data).to_bytes(2, "little")
    return _base32_encode(data + checksum)


def strkey_decode(value: str) -> tuple[int, bytes]:
    """Decode a Stellar strkey, verifying the CRC16 checksum.

    Returns ``(version_byte, payload)``. Raises :class:`StrkeyError` on any
    structural or checksum failure. The CRC16-XMODEM checksum is stored in the
    strkey with the least-significant byte first (SEP-23).
    """
    raw = _base32_decode(value.strip())
    if len(raw) < 3:
        raise StrkeyError("Strkey is too short.")
    version, payload, checksum = raw[0], raw[1:-2], raw[-2:]
    if _crc16_xmodem(raw[:-2]).to_bytes(2, "little") != checksum:
        raise StrkeyError("Strkey checksum failed.")
    return version, payload


def validate_stellar_strkey(value: str, *, prefix: str | None = None) -> tuple[bool, str]:
    """Return ``(ok, reason)`` for a full strkey checksum validation.

    ``prefix`` (e.g. ``"G"`` or ``"C"``) optionally requires the decoded
    version byte to correspond to that strkey prefix.
    """
    if not isinstance(value, str):
        return False, "Address must be a string."
    value = value.strip()
    if not value or value[0] not in _STRKEY_VERSION_BY_CHAR:
        return False, "Unknown strkey prefix."
    if prefix is not None and value[0] != prefix:
        return False, f"Expected a {prefix}... strkey."
    try:
        version, _payload = strkey_decode(value)
    except StrkeyError as exc:
        return False, str(exc)
    if value[0] != _CHAR_BY_VERSION.get(version):
        return False, "Strkey prefix does not match its version byte."
    return True, "ok"


_STRKEY_ALPHABET_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567")


def validate_contract_address(value: str) -> bool:
    """Return ``True`` when ``value`` is structurally a Soroban contract id.

    Performs the same structural check used for account ids (56 chars, ``C``
    prefix, valid strkey alphabet) without requiring a checksum. Full checksum
    validation is available via :func:`validate_stellar_strkey`.
    """
    if not isinstance(value, str) or len(value) != 56:
        return False
    if not value.startswith("C"):
        return False
    return all(char in _STRKEY_ALPHABET_CHARS for char in value[1:])


def account_address_to_bytes(address: str) -> bytes:
    """Return the 32-byte ed25519 payload of a ``G`` strkey.

    Raises :class:`StrkeyError` when the address is not a valid ``G`` strkey.
    """
    ok, reason = validate_stellar_strkey(address, prefix="G")
    if not ok:
        raise StrkeyError(reason)
    _version, payload = strkey_decode(address.strip())
    if len(payload) != 32:
        raise StrkeyError("Account strkey payload must be 32 bytes.")
    return payload


def contract_address_to_bytes(contract_id: str) -> bytes:
    """Return the 32-byte contract id payload of a ``C`` strkey.

    Raises :class:`StrkeyError` when the value is not a valid ``C`` strkey.
    """
    ok, reason = validate_stellar_strkey(contract_id, prefix="C")
    if not ok:
        raise StrkeyError(reason)
    _version, payload = strkey_decode(contract_id.strip())
    if len(payload) != 32:
        raise StrkeyError("Contract strkey payload must be 32 bytes.")
    return payload


# ---------------------------------------------------------------------------
# Minimal LedgerKey XDR encoding
# ---------------------------------------------------------------------------

#: ``LedgerEntryType`` discriminants used here (authoritative XDR values).
_LEDGER_ENTRY_ACCOUNT = 0
_LEDGER_ENTRY_CONTRACT_DATA = 6
_LEDGER_ENTRY_CONTRACT_CODE = 7
#: ``PublicKeyType.ED25519`` — the subtype discriminant of ``AccountID``.
_PUBLIC_KEY_ED25519 = 0
#: ``SCAddressType.CONTRACT``.
_SC_ADDRESS_CONTRACT = 1
#: ``SCV_LEDGER_KEY_CONTRACT_INSTANCE`` (void payload).
_SCV_LEDGER_KEY_CONTRACT_INSTANCE = 20
#: ``ContractDataDurability.PERSISTENT``.
_DURABILITY_PERSISTENT = 1


def _u32(value: int) -> bytes:
    return value.to_bytes(4, "big")


def ledger_key_account(account_bytes: bytes) -> str:
    """Return the base64 ``LedgerKey::Account`` for a 32-byte account payload.

    ``AccountID`` is a ``PublicKey`` union, so the key carries the LedgerEntry
    discriminant, the ``PUBLIC_KEY_TYPE_ED25519`` discriminant, and the key.
    """
    if len(account_bytes) != 32:
        raise StrkeyError("Account payload must be exactly 32 bytes.")
    data = _u32(_LEDGER_ENTRY_ACCOUNT) + _u32(_PUBLIC_KEY_ED25519) + account_bytes
    return base64.b64encode(data).decode("ascii")


def ledger_key_contract_data_instance(contract_bytes: bytes) -> str:
    """Return the base64 ``LedgerKey::ContractData`` for a contract's instance.

    The key is ``ScVal::LedgerKeyContractInstance`` with persistent durability,
    the entry that stores a contract's code-hash reference and instance storage.
    """
    if len(contract_bytes) != 32:
        raise StrkeyError("Contract payload must be exactly 32 bytes.")
    data = (
        _u32(_LEDGER_ENTRY_CONTRACT_DATA)
        + _u32(_SC_ADDRESS_CONTRACT)
        + contract_bytes
        + _u32(_SCV_LEDGER_KEY_CONTRACT_INSTANCE)
        + _u32(_DURABILITY_PERSISTENT)
    )
    return base64.b64encode(data).decode("ascii")


def ledger_key_contract_code(wasm_hash_bytes: bytes) -> str:
    """Return the base64 ``LedgerKey::ContractCode`` for a 32-byte wasm hash."""
    if len(wasm_hash_bytes) != 32:
        raise StrkeyError("Wasm hash must be exactly 32 bytes.")
    return base64.b64encode(_u32(_LEDGER_ENTRY_CONTRACT_CODE) + wasm_hash_bytes).decode("ascii")


def ledger_key_for_account(address: str) -> str:
    """Return the base64 account ``LedgerKey`` for a ``G`` strkey address."""
    return ledger_key_account(account_address_to_bytes(address))


def ledger_key_for_contract(contract_id: str) -> str:
    """Return the base64 contract-instance ``LedgerKey`` for a ``C`` strkey."""
    return ledger_key_contract_data_instance(contract_address_to_bytes(contract_id))


def wasm_hash_from_hex(value: str) -> bytes:
    """Return the 32 bytes of a hex wasm hash, raising on malformed input."""
    if not isinstance(value, str):
        raise StrkeyError("A wasm hash string is required.")
    text = value.strip().lower()
    if len(text) != 64:
        raise StrkeyError("A wasm hash must be 64 hex characters (32 bytes).")
    try:
        raw = binascii.unhexlify(text)
    except (binascii.Error, ValueError) as exc:
        raise StrkeyError("Wasm hash is not valid hex.") from exc
    return raw


def validate_ledger_key_base64(value: str) -> tuple[bool, str]:
    """Return ``(ok, reason)`` for a base64 ledger key submitted by a caller.

    Only structural checks are performed (base64, sane length); the node
    validates the key's XDR on its side.
    """
    if not isinstance(value, str) or not value.strip():
        return False, "A ledger key is required."
    text = value.strip()
    try:
        raw = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        return False, "Ledger key is not valid base64."
    if not _LEDGER_KEY_MIN_BYTES <= len(raw) <= _LEDGER_KEY_MAX_BYTES:
        return False, "Ledger key has an unexpected length."
    return True, "ok"
