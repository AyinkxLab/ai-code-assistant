"""TOTP multi-factor authentication and workspace enforcement policy (#618).

The implementation is intentionally dependency-free: RFC 6238 TOTP over
HMAC-SHA1 with a 30 second step and six digits, which is compatible with common
authenticator apps. Secrets are stored per user and never serialized.

Organizations opt in through ``WorkspaceSettings.require_mfa``. A login is
blocked when the user belongs to a workspace that requires MFA and the user has
not enrolled. Operators can list documented break-glass accounts in
``MFA_BREAK_GLASS_EMAILS`` (comma separated); those accounts skip enforcement.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote

from flask import current_app

from app.models import Workspace, WorkspaceMember, WorkspaceSettings
from app.models.workspace_member import ROLE_CONTRIBUTOR, ROLE_OWNER, STATUS_ACTIVE

TOTP_STEP_SECONDS = 30
TOTP_DIGITS = 6
TOTP_WINDOW = 1
SECRET_BYTES = 20


def generate_secret() -> str:
    """Return a fresh base32 secret (without padding) for an authenticator app."""
    return base64.b32encode(secrets.token_bytes(SECRET_BYTES)).decode("ascii").rstrip("=")


def _decode_secret(secret: str) -> bytes:
    padded = secret.strip().upper()
    padded += "=" * ((8 - len(padded) % 8) % 8)
    try:
        return base64.b32decode(padded, casefold=True)
    except Exception as exc:  # pragma: no cover - defensive
        raise ValueError("invalid MFA secret") from exc


def totp(secret: str, at: float | None = None) -> str:
    """Return the six-digit code for ``secret`` at ``at`` (Unix seconds)."""
    counter = int((time.time() if at is None else at) // TOTP_STEP_SECONDS)
    digest = hmac.new(_decode_secret(secret), struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % (10**TOTP_DIGITS)).zfill(TOTP_DIGITS)


def verify_code(secret: str | None, code: str, at: float | None = None) -> bool:
    """Verify ``code`` for ``secret`` allowing one step of clock skew."""
    if not secret:
        return False
    candidate = (code or "").strip()
    if len(candidate) != TOTP_DIGITS or not candidate.isdigit():
        return False
    now = time.time() if at is None else at
    for offset in range(-TOTP_WINDOW, TOTP_WINDOW + 1):
        expected = totp(secret, now + offset * TOTP_STEP_SECONDS)
        if hmac.compare_digest(expected, candidate):
            return True
    return False


def provisioning_uri(email: str, secret: str) -> str:
    """Return an ``otpauth://`` URI for authenticator apps."""
    issuer = current_app.config.get("MFA_ISSUER", "AI Code Assistant")
    label = quote(f"{issuer}:{email}")
    return (
        f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer)}"
        f"&algorithm=SHA1&digits={TOTP_DIGITS}&period={TOTP_STEP_SECONDS}"
    )


def break_glass_emails() -> set[str]:
    """Emails explicitly exempted from MFA enforcement (documented break-glass)."""
    raw = current_app.config.get("MFA_BREAK_GLASS_EMAILS", "") or ""
    return {email.strip().lower() for email in raw.split(",") if email.strip()}


def _workspace_roles(user) -> dict[int, str]:
    roles: dict[int, str] = {}
    for workspace in Workspace.query.filter_by(user_id=user.id).all():
        roles[workspace.id] = ROLE_OWNER
    for membership in WorkspaceMember.query.filter_by(user_id=user.id, status=STATUS_ACTIVE).all():
        roles.setdefault(membership.workspace_id, membership.role)
    return roles


def mfa_required_for(user) -> bool:
    """Return ``True`` when a workspace policy requires MFA for this user."""
    if not user or not user.is_authenticated:
        return False
    if (user.email or "").lower() in break_glass_emails():
        return False
    roles = _workspace_roles(user)
    if not roles:
        return False
    settings = WorkspaceSettings.query.filter(
        WorkspaceSettings.workspace_id.in_(roles.keys())
    ).all()
    for setting in settings:
        if not setting.require_mfa:
            continue
        if setting.mfa_privileged_only:
            role = roles.get(setting.workspace_id)
            if role not in (ROLE_OWNER, ROLE_CONTRIBUTOR):
                continue
        return True
    return False
