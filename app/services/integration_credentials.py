"""Encrypted integration credential storage and rotation (#683).

Only workspace owners may read or change credentials (the
``manage_integrations`` capability). Every mutation re-encrypts with the
provider-key AES-GCM helper, so a rotation produces different ciphertext even
when the plaintext is unchanged.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.extensions import db
from app.models import IntegrationCredential, Workspace
from app.services.crypto import decrypt_api_key, encrypt_api_key
from app.services.permissions import can

#: Stable error codes returned by callers/APIs.
INVALID_INPUT = "invalid_input"
NOT_FOUND = "not_found"
PERMISSION_DENIED = "permission_denied"
NOT_CONFIGURED = "not_configured"


class IntegrationCredentialError(ValueError):
    """Invalid integration credential input."""

    code = INVALID_INPUT


class IntegrationCredentialNotFound(IntegrationCredentialError):
    """The requested credential does not exist for the workspace."""

    code = NOT_FOUND


class IntegrationCredentialPermissionError(PermissionError):
    """The caller may not read or change the credential."""

    code = PERMISSION_DENIED


class IntegrationCredentialNotConfigured(IntegrationCredentialError):
    """Encryption configuration is unavailable (for example no SECRET_KEY)."""

    code = NOT_CONFIGURED


def _normalize(value, label: str, max_length: int) -> str:
    text = str(value or "").strip()
    if not text:
        raise IntegrationCredentialError(f"{label} is required.")
    if len(text) > max_length:
        raise IntegrationCredentialError(f"{label} must be {max_length} characters or fewer.")
    return text


def _require_owner(workspace_id: int, user) -> Workspace:
    workspace = Workspace.query.filter_by(id=workspace_id).first()
    if workspace is None or not can("manage_integrations", workspace_id, user):
        raise IntegrationCredentialPermissionError(
            "You do not have permission to manage integration credentials."
        )
    return workspace


def _encrypt(secret: str) -> str:
    try:
        return encrypt_api_key(secret)
    except Exception as exc:  # missing/empty SECRET_KEY
        raise IntegrationCredentialNotConfigured(
            "Integration credential encryption is unavailable."
        ) from exc


def set_credential(
    workspace_id: int,
    integration,
    name,
    secret,
    *,
    user,
) -> IntegrationCredential:
    """Create or rotate one credential, returning the stored row.

    Re-setting an existing credential stores fresh ciphertext (new nonce), so
    ``encrypted_value`` always differs across writes.
    """
    _require_owner(workspace_id, user)
    integration = _normalize(integration, "integration", 50)
    name = _normalize(name, "name", 100)
    secret = str(secret or "").strip()
    if not secret:
        raise IntegrationCredentialError("A secret is required.")

    credential = IntegrationCredential.query.filter_by(
        workspace_id=workspace_id, integration=integration, name=name
    ).first()
    if credential is None:
        credential = IntegrationCredential(
            workspace_id=workspace_id,
            integration=integration,
            name=name,
            encrypted_value=_encrypt(secret),
        )
        db.session.add(credential)
    else:
        credential.encrypted_value = _encrypt(secret)
        credential.last_rotated_at = datetime.now(UTC)
    db.session.commit()
    return credential


def list_credentials(workspace_id: int, *, user) -> list[IntegrationCredential]:
    """Return the workspace's credentials in integration/name order."""
    _require_owner(workspace_id, user)
    return (
        IntegrationCredential.query.filter_by(workspace_id=workspace_id)
        .order_by(IntegrationCredential.integration, IntegrationCredential.name)
        .all()
    )


def get_credential(workspace_id: int, integration, name, *, user) -> IntegrationCredential:
    """Return one credential or raise :class:`IntegrationCredentialNotFound`."""
    _require_owner(workspace_id, user)
    integration = _normalize(integration, "integration", 50)
    name = _normalize(name, "name", 100)
    credential = IntegrationCredential.query.filter_by(
        workspace_id=workspace_id, integration=integration, name=name
    ).first()
    if credential is None:
        raise IntegrationCredentialNotFound("Integration credential not found.")
    return credential


def decrypt_for_use(credential: IntegrationCredential) -> str:
    """Decrypt a credential in memory for an outgoing integration call.

    The permission check happens when the credential is read with
    :func:`get_credential`; this helper never persists or serializes the
    plaintext.
    """
    try:
        return decrypt_api_key(credential.encrypted_value)
    except ValueError as exc:
        raise IntegrationCredentialNotConfigured(
            "The stored integration credential cannot be decrypted."
        ) from exc


def rotate_credential(credential: IntegrationCredential, *, user) -> IntegrationCredential:
    """Re-encrypt ``credential`` with a fresh nonce, preserving its secret."""
    _require_owner(credential.workspace_id, user)
    plaintext = decrypt_for_use(credential)
    credential.encrypted_value = _encrypt(plaintext)
    credential.last_rotated_at = datetime.now(UTC)
    db.session.commit()
    return credential


def rotate_workspace_credentials(workspace_id: int, *, user) -> int:
    """Rotate every credential in a workspace and return the count."""
    credentials = list_credentials(workspace_id, user=user)
    for credential in credentials:
        plaintext = decrypt_for_use(credential)
        credential.encrypted_value = _encrypt(plaintext)
        credential.last_rotated_at = datetime.now(UTC)
    db.session.commit()
    return len(credentials)


def delete_credential(workspace_id: int, integration, name, *, user) -> None:
    """Delete one credential or raise :class:`IntegrationCredentialNotFound`."""
    credential = get_credential(workspace_id, integration, name, user=user)
    db.session.delete(credential)
    db.session.commit()


__all__ = [
    "INVALID_INPUT",
    "NOT_CONFIGURED",
    "NOT_FOUND",
    "PERMISSION_DENIED",
    "IntegrationCredentialError",
    "IntegrationCredentialNotConfigured",
    "IntegrationCredentialNotFound",
    "IntegrationCredentialPermissionError",
    "decrypt_for_use",
    "delete_credential",
    "get_credential",
    "list_credentials",
    "rotate_credential",
    "rotate_workspace_credentials",
    "set_credential",
]
