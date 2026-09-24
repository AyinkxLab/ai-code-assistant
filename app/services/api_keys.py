"""CRUD and in-memory decryption for user-owned LLM provider API keys.

Plaintext keys only exist in memory: they are encrypted immediately on create
and only decrypted on demand for an outgoing provider call. List/inspect
surfaces return redacted metadata via :meth:`~app.models.api_key.ApiKey.to_dict`.
"""

from __future__ import annotations

from app.extensions import db
from app.models import ApiKey
from app.services.crypto import decrypt_api_key, encrypt_api_key


class ApiKeyError(ValueError):
    """Raised for invalid API-key input."""


def normalize_provider(provider) -> str:
    value = str(provider or "").strip().lower()
    if not value:
        raise ApiKeyError("A provider is required.")
    return value[:50]


def create_key(user, *, provider, secret, label=None) -> ApiKey:
    """Encrypt and persist a new API key for ``user``."""
    secret = str(secret or "").strip()
    if not secret:
        raise ApiKeyError("An API key is required.")
    key = ApiKey(
        user_id=user.id,
        provider=normalize_provider(provider),
        encrypted_value=encrypt_api_key(secret),
        label=(str(label).strip()[:100] or None) if label else None,
    )
    db.session.add(key)
    db.session.commit()
    return key


def list_keys(user) -> list[ApiKey]:
    """Return the user's keys, newest first."""
    return ApiKey.query.filter_by(user_id=user.id).order_by(ApiKey.created_at.desc()).all()


def get_owned(user, key_id: int) -> ApiKey | None:
    """Return ``user``'s key or ``None`` (owner scoping)."""
    return ApiKey.query.filter_by(id=key_id, user_id=user.id).first()


def update_key(user, key_id: int, *, label=None, is_active=None) -> ApiKey | None:
    key = get_owned(user, key_id)
    if key is None:
        return None
    if label is not None:
        key.label = str(label).strip()[:100] or None
    if is_active is not None:
        key.is_active = bool(is_active)
    db.session.commit()
    return key


def delete_key(user, key_id: int) -> bool:
    key = get_owned(user, key_id)
    if key is None:
        return False
    db.session.delete(key)
    db.session.commit()
    return True


def decrypt_for_use(key: ApiKey) -> str:
    """Decrypt a key in memory for an outgoing provider call.

    The plaintext is returned to the caller only and is never persisted or
    serialized.
    """
    return decrypt_api_key(key.encrypted_value)
