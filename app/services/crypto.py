"""Symmetric encryption for sensitive values.

Two schemes live here:

* ``encrypt_secret`` / ``decrypt_secret`` — Fernet (AES-128-CBC + HMAC-SHA256)
  for the GitHub OAuth access tokens.
* ``encrypt_api_key`` / ``decrypt_api_key`` — AES-256-GCM (an AEAD scheme) with a
  fresh random 96-bit nonce per record, for user-supplied LLM provider keys.

Both derive their key deterministically from the application ``SECRET_KEY`` so no
extra key material needs to be provisioned, while still ensuring the stored
ciphertext is useless without the secret key.

Key rotation: because the key is derived from ``SECRET_KEY``, rotating
``SECRET_KEY`` invalidates existing ciphertext (decryption raises
:class:`ValueError`). To rotate safely, re-encrypt stored values while the old
secret is still configured (read with the old key, write with the new), then
switch ``SECRET_KEY``. The ``v1`` prefix on API-key ciphertext lets a future
version be detected rather than mis-decrypted.
"""

import base64
import hashlib
import os

from cryptography.exceptions import InvalidTag
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from flask import current_app

#: Version tag prepended to AES-GCM ciphertext so the format can evolve.
_API_KEY_VERSION = b"v1"
_API_KEY_NONCE_BYTES = 12


def _fernet():
    """Return a Fernet instance derived from the application secret key."""
    secret = current_app.config.get("SECRET_KEY", "")
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_secret(plaintext: str) -> str:
    """Encrypt ``plaintext`` and return a base64 ciphertext string."""
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_secret(ciphertext: str) -> str:
    """Decrypt ``ciphertext`` produced by :func:`encrypt_secret`.

    Raises :class:`ValueError` if the ciphertext is invalid or the secret key
    changed (which invalidates all previously encrypted values).
    """
    try:
        return _fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError("Could not decrypt stored secret (invalid key or corrupt data).") from exc


def _aesgcm() -> AESGCM:
    """Return an AES-256-GCM cipher derived from the application secret key.

    A distinct derivation salt separates this key from the Fernet key so the two
    schemes never share key material.
    """
    secret = current_app.config.get("SECRET_KEY", "")
    key = hashlib.sha256((secret + "|llm-api-keys-v1").encode("utf-8")).digest()
    return AESGCM(key)


def encrypt_api_key(plaintext: str) -> str:
    """Encrypt an LLM provider API key with AES-256-GCM (random nonce per call)."""
    nonce = os.urandom(_API_KEY_NONCE_BYTES)
    ciphertext = _aesgcm().encrypt(nonce, plaintext.encode("utf-8"), None)
    return base64.urlsafe_b64encode(_API_KEY_VERSION + nonce + ciphertext).decode("ascii")


def decrypt_api_key(token: str) -> str:
    """Decrypt a value produced by :func:`encrypt_api_key`.

    Raises :class:`ValueError` on tampering, a malformed token, or a rotated
    secret key. The plaintext only ever exists in memory for the caller.
    """
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii"))
    except (ValueError, TypeError) as exc:
        raise ValueError("Stored API key is not valid ciphertext.") from exc
    if not raw.startswith(_API_KEY_VERSION):
        raise ValueError("Unsupported API key ciphertext version.")
    body = raw[len(_API_KEY_VERSION) :]
    if len(body) <= _API_KEY_NONCE_BYTES:
        raise ValueError("Stored API key is not valid ciphertext.")
    nonce, ciphertext = body[:_API_KEY_NONCE_BYTES], body[_API_KEY_NONCE_BYTES:]
    try:
        return _aesgcm().decrypt(nonce, ciphertext, None).decode("utf-8")
    except (InvalidTag, ValueError) as exc:
        raise ValueError(
            "Could not decrypt stored API key (invalid key or tampered data)."
        ) from exc


def mask_api_key(plaintext: str) -> str:
    """Return a short, non-sensitive hint for display (never the full key)."""
    if not plaintext:
        return ""
    if len(plaintext) <= 8:
        return "*" * len(plaintext)
    return f"{plaintext[:4]}…{plaintext[-4:]}"
