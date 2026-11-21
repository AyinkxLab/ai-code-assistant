"""Inbound and outbound email integration (#682).

Outbound
    Reply-to notifications are rendered from bounded templates and sent through
    the existing SMTP service with a ``Reply-To`` header and a signed
    ``List-Unsubscribe`` link. A per-workspace suppression list honours opt-outs.

Inbound
    The public inbound endpoint accepts JSON over HTTP from a mail relay. Every
    request must carry a fresh HMAC-SHA256 signature over ``<timestamp>.<body>``
    using the workspace's encrypted ``inbound_secret``; stale timestamps are
    rejected. The parser is bounded in body size, attachment count, attachment
    size, and text length, and rejects executable or path-traversing attachment
    names. Network I/O is never performed by this module in tests: an optional
    ``sender``/``notify_fn`` is injectable.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
import os
import time
from datetime import UTC, datetime

from flask import current_app, render_template

from app.extensions import db
from app.models import EmailSuppression, User, Workspace
from app.services.email import send_email
from app.services.integration_credentials import (
    IntegrationCredentialError,
    delete_credential,
    set_credential,
)
from app.services.integrations import health
from app.services.integrations.common import (
    EVENT_TEST,
    IntegrationDeliveryError,
    IntegrationError,
    IntegrationNotConfigured,
    IntegrationNotFound,
    IntegrationValidationError,
    bounded_text,
    build_event_summary,
    load_credential_value,
    normalize_event_type,
    sanitize_error,
    summary_lines,
)

INTEGRATION = "email"
INBOUND_SECRET_CREDENTIAL = "inbound_secret"
REPLY_TO_CREDENTIAL = "reply_to"

DEFAULT_SIGNATURE_TOLERANCE = 300
DEFAULT_MAX_ATTACHMENTS = 5
DEFAULT_MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
DEFAULT_MAX_BODY_CHARS = 20000
DEFAULT_INBOUND_MAX_BYTES = 512 * 1024
MAX_SUBJECT_CHARS = 200
MAX_FILENAME_CHARS = 255

#: Attachment extensions that must never be accepted from inbound mail.
UNSAFE_EXTENSIONS = frozenset(
    {
        ".ade",
        ".adp",
        ".apk",
        ".app",
        ".bat",
        ".cmd",
        ".com",
        ".cpl",
        ".dll",
        ".dmg",
        ".ex",
        ".exe",
        ".hta",
        ".ins",
        ".iso",
        ".jar",
        ".js",
        ".jse",
        ".lnk",
        ".mjs",
        ".msi",
        ".msp",
        ".pif",
        ".ps1",
        ".reg",
        ".scr",
        ".sh",
        ".sys",
        ".vb",
        ".vbe",
        ".vbs",
        ".wsf",
        ".wsh",
    }
)


class InboundEmailError(IntegrationError):
    """A structured inbound-email authentication or parsing failure."""

    def __init__(self, message, *, code="invalid_input", http_status=400):
        super().__init__(message)
        self.code = code
        self.http_status = http_status


def _config(name: str, default):
    try:
        return current_app.config.get(name, default)
    except RuntimeError:
        return default


def _max_bytes() -> int:
    value = int(_config("INTEGRATION_INBOUND_MAX_BYTES", DEFAULT_INBOUND_MAX_BYTES))
    return value if value > 0 else DEFAULT_INBOUND_MAX_BYTES


def _max_attachments() -> int:
    value = int(_config("INTEGRATION_EMAIL_MAX_ATTACHMENTS", DEFAULT_MAX_ATTACHMENTS))
    return max(0, value)


def _max_attachment_bytes() -> int:
    value = int(_config("INTEGRATION_EMAIL_MAX_ATTACHMENT_BYTES", DEFAULT_MAX_ATTACHMENT_BYTES))
    return value if value > 0 else DEFAULT_MAX_ATTACHMENT_BYTES


def _max_body_chars() -> int:
    value = int(_config("INTEGRATION_EMAIL_MAX_BODY_CHARS", DEFAULT_MAX_BODY_CHARS))
    return value if value > 0 else DEFAULT_MAX_BODY_CHARS


def _tolerance() -> int:
    value = int(_config("INTEGRATION_INBOUND_SIGNATURE_TOLERANCE", DEFAULT_SIGNATURE_TOLERANCE))
    return value if value >= 0 else DEFAULT_SIGNATURE_TOLERANCE


def _normalize_email(value, label="email") -> str:
    email = str(value or "").strip().lower()
    if len(email) > 255 or "@" not in email or any(ch.isspace() for ch in email):
        raise IntegrationValidationError(f"{label} is not a valid email address.")
    return email


def configure(workspace_id: int, user, *, inbound_secret=None, reply_to=None) -> dict:
    """Store the inbound signing secret and/or reply-to address (owner only)."""
    if inbound_secret is None and reply_to is None:
        raise IntegrationValidationError("Provide inbound_secret and/or reply_to.")
    stored: list[str] = []
    if inbound_secret is not None:
        secret = str(inbound_secret or "").strip()
        if len(secret) < 16 or len(secret) > 255:
            raise IntegrationValidationError(
                "inbound_secret must be between 16 and 255 characters."
            )
        set_credential(workspace_id, INTEGRATION, INBOUND_SECRET_CREDENTIAL, secret, user=user)
        stored.append(INBOUND_SECRET_CREDENTIAL)
    if reply_to is not None:
        reply = _normalize_email(reply_to, "reply_to")
        set_credential(workspace_id, INTEGRATION, REPLY_TO_CREDENTIAL, reply, user=user)
        stored.append(REPLY_TO_CREDENTIAL)
    return {"integration": INTEGRATION, "configured": True, "stored": sorted(stored)}


def delete_configuration(workspace_id: int, user) -> None:
    for name in (INBOUND_SECRET_CREDENTIAL, REPLY_TO_CREDENTIAL):
        with contextlib.suppress(IntegrationCredentialError):
            delete_credential(workspace_id, INTEGRATION, name, user=user)


# ---------------------------------------------------------------------------
# Unsubscribe tokens and suppression
# ---------------------------------------------------------------------------


def _unsubscribe_key() -> bytes:
    secret = str(_config("SECRET_KEY", "") or "")
    return hmac.new(secret.encode("utf-8"), b"email-unsubscribe-v1", hashlib.sha256).digest()


def make_unsubscribe_token(workspace_id: int, email: str) -> str:
    """Return a signed, URL-safe unsubscribe token for one recipient."""
    email = _normalize_email(email)
    payload = f"{int(workspace_id)}:{email}".encode()
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    signature = hmac.new(_unsubscribe_key(), encoded.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"


def verify_unsubscribe_token(token) -> tuple[int, str]:
    """Validate an unsubscribe token and return ``(workspace_id, email)``."""
    text = str(token or "").strip()
    if "." not in text:
        raise InboundEmailError("Unsubscribe token is malformed.", code="invalid_token")
    encoded, signature = text.rsplit(".", 1)
    expected = hmac.new(_unsubscribe_key(), encoded.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature.lower()):
        raise InboundEmailError("Unsubscribe token is invalid.", code="invalid_token")
    padding = "=" * (-len(encoded) % 4)
    try:
        payload = base64.urlsafe_b64decode(encoded + padding).decode("utf-8")
        workspace_raw, email = payload.split(":", 1)
        workspace_id = int(workspace_raw)
    except (ValueError, TypeError, UnicodeDecodeError) as exc:
        raise InboundEmailError("Unsubscribe token is malformed.", code="invalid_token") from exc
    return workspace_id, _normalize_email(email)


def unsubscribe_url(workspace_id: int, email: str) -> str:
    """Return an unsubscribe URL, absolute when ``PUBLIC_BASE_URL`` is configured."""
    token = make_unsubscribe_token(workspace_id, email)
    base = str(_config("PUBLIC_BASE_URL", "") or "").rstrip("/")
    path = f"/integrations/email/unsubscribe?token={token}"
    return f"{base}{path}" if base else path


def is_suppressed(workspace_id: int, email: str) -> bool:
    email = _normalize_email(email)
    return (
        EmailSuppression.query.filter_by(workspace_id=workspace_id, email=email).first() is not None
    )


def suppress(workspace_id: int, email: str, *, reason: str = "unsubscribe"):
    """Add a recipient to the suppression list (idempotent)."""
    email = _normalize_email(email)
    row = EmailSuppression.query.filter_by(workspace_id=workspace_id, email=email).first()
    if row is None:
        row = EmailSuppression(
            workspace_id=workspace_id,
            email=email,
            reason=str(reason or "unsubscribe")[:50],
        )
        db.session.add(row)
        db.session.commit()
    return row


def unsubscribe(token) -> dict:
    """Verify an unsubscribe token and persist the suppression."""
    workspace_id, email = verify_unsubscribe_token(token)
    if db.session.get(Workspace, workspace_id) is None:
        raise InboundEmailError("Workspace not found.", code="not_found", http_status=404)
    suppress(workspace_id, email)
    return {"status": "unsubscribed", "workspace_id": workspace_id, "email": email}


# ---------------------------------------------------------------------------
# Message rendering and outbound delivery
# ---------------------------------------------------------------------------


def build_message(
    event_type: str,
    data: dict | None,
    *,
    workspace_name: str,
    unsubscribe_url_value: str | None = None,
) -> dict:
    """Render the bounded subject/text/html for one event."""
    event_type = normalize_event_type(event_type)
    summary = build_event_summary(event_type, data)
    title = str(summary.get("title") or "Notification")[:MAX_SUBJECT_CHARS]
    lines = summary_lines(summary)
    subject = f"[{bounded_text(workspace_name, 100)}] {title}"[:MAX_SUBJECT_CHARS]
    context = {
        "workspace_name": bounded_text(workspace_name, 100),
        "title": title,
        "lines": lines,
        "unsubscribe_url": unsubscribe_url_value,
    }
    text = render_template("integrations/email_notification.txt", **context)
    html = render_template("integrations/email_notification.html", **context)
    return {"subject": subject, "text": text, "html": html}


def _owner_for(workspace) -> User | None:
    return db.session.get(User, workspace.user_id) if workspace is not None else None


def _deliver(
    workspace,
    event_type: str,
    data: dict | None,
    *,
    to=None,
    user=None,
    sender=None,
    reply_to=None,
    unsubscribe_url_value=None,
) -> dict:
    event_type = normalize_event_type(event_type)
    recipient = to or getattr(user, "email", None)
    if not recipient:
        owner = _owner_for(workspace)
        recipient = getattr(owner, "email", None)
    if not recipient:
        raise IntegrationNotConfigured("No recipient email is available.")
    recipient = _normalize_email(recipient, "recipient")

    if is_suppressed(workspace.id, recipient):
        return {
            "status": "skipped",
            "integration": INTEGRATION,
            "event_type": event_type,
            "reason": "recipient unsubscribed",
        }

    if reply_to is None:
        try:
            reply_to = load_credential_value(workspace.id, INTEGRATION, REPLY_TO_CREDENTIAL)
        except IntegrationNotConfigured:
            reply_to = None

    unsubscribe = unsubscribe_url_value or unsubscribe_url(workspace.id, recipient)
    message = build_message(
        event_type,
        data,
        workspace_name=getattr(workspace, "name", "workspace"),
        unsubscribe_url_value=unsubscribe,
    )
    headers = {"List-Unsubscribe": f"<{unsubscribe}>"}

    try:
        if sender is not None:
            delivered = bool(
                sender(
                    to=recipient,
                    subject=message["subject"],
                    text_body=message["text"],
                    html_body=message["html"],
                    reply_to=reply_to,
                    headers=headers,
                )
            )
        else:
            delivered = send_email(
                recipient,
                message["subject"],
                message["text"],
                message["html"],
                reply_to=reply_to,
                headers=headers,
            )
    except Exception as exc:
        health.record_error(workspace.id, INTEGRATION, exc)
        raise IntegrationDeliveryError(sanitize_error(exc)) from exc

    if not delivered:
        exc = IntegrationDeliveryError("Email delivery failed or SMTP is not configured.")
        health.record_error(workspace.id, INTEGRATION, exc, code=exc.code)
        return {
            "status": "skipped",
            "integration": INTEGRATION,
            "event_type": event_type,
            "reason": "email delivery failed",
            "code": exc.code,
        }

    health.record_success(workspace.id, INTEGRATION)
    return {
        "status": "sent",
        "integration": INTEGRATION,
        "event_type": event_type,
        "reply_to": reply_to,
    }


def send_event(
    workspace,
    event_type: str,
    data: dict | None = None,
    *,
    to=None,
    user=None,
    sender=None,
    reply_to=None,
    unsubscribe_url_value=None,
) -> dict:
    """Send one reply-to notification email."""
    return _deliver(
        workspace,
        event_type,
        data,
        to=to,
        user=user,
        sender=sender,
        reply_to=reply_to,
        unsubscribe_url_value=unsubscribe_url_value,
    )


def test_connection(workspace, user, *, sender=None) -> dict:
    """Send a test email to the requesting owner and record health."""
    return _deliver(
        workspace,
        EVENT_TEST,
        {"message": "Test notification from the AI Code Assistant."},
        user=user,
        sender=sender,
    )


# ---------------------------------------------------------------------------
# Inbound authenticity and bounded parser
# ---------------------------------------------------------------------------


def verify_inbound_signature(
    raw_body: bytes,
    timestamp,
    signature,
    secret,
    *,
    tolerance: int | None = None,
    now=None,
) -> bool:
    """Verify ``HMAC-SHA256(secret, "<timestamp>.<raw_body>")`` and freshness."""
    if not secret:
        raise IntegrationNotConfigured("Inbound email signing secret is not configured.")
    body = raw_body if isinstance(raw_body, bytes) else str(raw_body or "").encode("utf-8")
    try:
        ts = int(str(timestamp).strip())
    except (TypeError, ValueError) as exc:
        raise InboundEmailError(
            "Missing or invalid inbound timestamp.", code="invalid_signature", http_status=401
        ) from exc
    current = int(time.time()) if now is None else int(now)
    window = _tolerance() if tolerance is None else max(0, int(tolerance))
    if abs(current - ts) > window:
        raise InboundEmailError(
            "Inbound signature timestamp is outside the allowed window.",
            code="expired_signature",
            http_status=401,
        )
    expected = hmac.new(
        str(secret).encode("utf-8"),
        f"{ts}.".encode("ascii") + body,
        hashlib.sha256,
    ).hexdigest()
    provided = str(signature or "").strip().lower()
    if not provided or not hmac.compare_digest(expected, provided):
        raise InboundEmailError(
            "Inbound signature is invalid.", code="invalid_signature", http_status=401
        )
    return True


def _safe_filename(value) -> str:
    filename = str(value or "").strip()
    if not filename or len(filename) > MAX_FILENAME_CHARS:
        raise InboundEmailError(
            "Attachment filename is missing or too long.", code="unsafe_attachment"
        )
    if "/" in filename or "\\" in filename or filename in (".", ".."):
        raise InboundEmailError(
            "Attachment filename may not contain a path.", code="unsafe_attachment"
        )
    extension = os.path.splitext(filename)[1].lower()
    if extension in UNSAFE_EXTENSIONS:
        raise InboundEmailError(
            f"Attachment type {extension} is not allowed.", code="unsafe_attachment"
        )
    return filename


def parse_inbound(payload) -> dict:
    """Validate and bound an inbound message; never touch the network."""
    if not isinstance(payload, dict):
        raise InboundEmailError("Inbound email payload must be a JSON object.")
    sender = _normalize_email(payload.get("from") or payload.get("sender"), "sender")
    subject = bounded_text(payload.get("subject"), MAX_SUBJECT_CHARS) or "(no subject)"
    text = str(payload.get("text") or payload.get("body") or "")
    text = text[: _max_body_chars()]

    raw_attachments = payload.get("attachments") or []
    if not isinstance(raw_attachments, list):
        raise InboundEmailError("attachments must be a list.")
    if len(raw_attachments) > _max_attachments():
        raise InboundEmailError(
            f"At most {_max_attachments()} attachments are accepted.",
            code="unsafe_attachment",
        )
    attachments = []
    for item in raw_attachments:
        if not isinstance(item, dict):
            raise InboundEmailError("Each attachment must be an object.")
        filename = _safe_filename(item.get("filename") or item.get("name"))
        try:
            size = int(item.get("size") or 0)
        except (TypeError, ValueError) as exc:
            raise InboundEmailError("Attachment size must be an integer.") from exc
        if size < 0 or size > _max_attachment_bytes():
            raise InboundEmailError(
                "Attachment exceeds the allowed size.", code="unsafe_attachment"
            )
        content_type = bounded_text(item.get("content_type") or item.get("type"), 120)
        attachments.append({"filename": filename, "content_type": content_type, "size": size})
    return {
        "from": sender,
        "subject": subject,
        "text": text,
        "attachments": attachments,
        "attachment_count": len(attachments),
    }


def process_inbound(
    workspace_id: int,
    raw_body: bytes,
    timestamp,
    signature,
    *,
    now=None,
    notify_fn=None,
) -> dict:
    """Authenticate, parse, and record an inbound email for one workspace."""
    body = raw_body if isinstance(raw_body, bytes) else str(raw_body or "").encode("utf-8")
    if len(body) > _max_bytes():
        exc = InboundEmailError(
            "Inbound email payload is too large.", code="payload_too_large", http_status=413
        )
        health.record_error(workspace_id, INTEGRATION, exc, code=exc.code)
        raise exc
    try:
        secret = load_credential_value(workspace_id, INTEGRATION, INBOUND_SECRET_CREDENTIAL)
        verify_inbound_signature(body, timestamp, signature, secret, now=now)
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError) as exc:
            raise InboundEmailError("Inbound email body is not valid JSON.") from exc
        parsed = parse_inbound(payload)
        workspace = db.session.get(Workspace, workspace_id)
        if workspace is None:
            raise IntegrationNotFound("Workspace not found.")
        owner = _owner_for(workspace)
        if owner is not None:
            if notify_fn is not None:
                notify_fn(owner, parsed)
            else:
                from app.services.notifications import notify

                notify(
                    owner,
                    "ai_event",
                    workspace=workspace,
                    payload={
                        "title": "Email reply received",
                        "from": parsed["from"],
                        "subject": parsed["subject"],
                        "attachment_count": parsed["attachment_count"],
                    },
                    link="/workspaces/notifications",
                )
            db.session.commit()
    except IntegrationError as exc:
        health.record_error(workspace_id, INTEGRATION, exc, code=exc.code)
        raise

    health.record_success(workspace_id, INTEGRATION, status_code=202)
    return {
        "status": "accepted",
        "workspace_id": workspace_id,
        "subject": parsed["subject"],
        "attachment_count": parsed["attachment_count"],
        "received_at": datetime.now(UTC).isoformat(),
    }


__all__ = [
    "INBOUND_SECRET_CREDENTIAL",
    "INTEGRATION",
    "REPLY_TO_CREDENTIAL",
    "UNSAFE_EXTENSIONS",
    "InboundEmailError",
    "build_message",
    "configure",
    "delete_configuration",
    "is_suppressed",
    "make_unsubscribe_token",
    "parse_inbound",
    "process_inbound",
    "send_event",
    "suppress",
    "test_connection",
    "unsubscribe",
    "unsubscribe_url",
    "verify_inbound_signature",
    "verify_unsubscribe_token",
]
