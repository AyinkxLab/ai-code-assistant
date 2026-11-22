"""DNS TXT domain verification and SSO enforcement (#590).

The flow is deliberately small:

1. A workspace owner creates a challenge for a domain. We store a random token
   and an expiry (bounded by :data:`CHALLENGE_TTL`).
2. The owner publishes ``aica-verify=<token>`` as a TXT record at
   ``_aica-verification.<domain>``.
3. The owner asks us to verify. We read the TXT record and only mark the domain
   verified when the exact expected value is present before the expiry.
4. The owner may then set ``enforce_sso``; password login for accounts on that
   domain is blocked in :mod:`app.auth.routes`.

Every mutating operation resolves the same ``manage_settings`` workspace
capability used by the web UI, so an API caller can never do more than the
owner could in the browser.
"""

from __future__ import annotations

import re
import secrets
from datetime import UTC, datetime, timedelta

from flask_login import current_user
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import WorkspaceDomain
from app.services.permissions import can

#: A challenge is valid for 24 hours; an abandoned verification must be redone.
CHALLENGE_TTL = timedelta(hours=24)

#: Prefix of the TXT record the organization publishes.
TXT_RECORD_PREFIX = "_aica-verification"

#: Value prefix inside that record.
TXT_VALUE_PREFIX = "aica-verify="

#: DNS lookups must not hang a request.
DNS_TIMEOUT_SECONDS = 5

_DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)


class DomainVerificationError(ValueError):
    """Base class for a rejected domain operation."""


class InvalidDomain(DomainVerificationError):
    """The supplied domain is not a valid DNS name."""


class DomainPermissionDenied(DomainVerificationError):
    """The caller is not allowed to manage this workspace's domains."""


class DomainConflict(DomainVerificationError):
    """The domain is already claimed, verified, or otherwise conflicting."""


class ChallengeExpired(DomainVerificationError):
    """The DNS challenge is past its expiry."""


class VerificationFailed(DomainVerificationError):
    """The expected TXT record was not found."""


class DomainNotVerified(DomainVerificationError):
    """Enforcement requires a verified domain."""


def _now(value: datetime | None = None) -> datetime:
    moment = value or datetime.now(UTC)
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def normalize_domain(raw: str | None) -> str:
    """Return a lowercase DNS domain, or raise :class:`InvalidDomain`."""
    domain = (raw or "").strip().lower().rstrip(".")
    if not domain or len(domain) > 253 or not _DOMAIN_RE.match(domain):
        raise InvalidDomain("Enter a valid domain name such as example.com.")
    return domain


def txt_record_name(domain: str) -> str:
    """Return the DNS name the organization must publish."""
    return f"{TXT_RECORD_PREFIX}.{normalize_domain(domain)}"


def expected_txt_value(token: str) -> str:
    """Return the TXT value that proves the challenge."""
    return f"{TXT_VALUE_PREFIX}{token}"


def _default_resolver(name: str) -> list[str]:
    """Read TXT values via dnspython (already installed for email-validator)."""
    try:
        import dns.resolver
    except ImportError:  # pragma: no cover - dependency is present in production
        raise VerificationFailed("DNS lookups are not available on this server.") from None

    try:
        answers = dns.resolver.resolve(name, "TXT", lifetime=DNS_TIMEOUT_SECONDS)
    except Exception:  # dnspython exception hierarchy is broad; treat as "not found"
        return []

    values: list[str] = []
    for rdata in answers:
        try:
            values.append("".join(part.decode("utf-8") for part in rdata.strings))
        except (AttributeError, UnicodeDecodeError):
            continue
    return values


def _resolve_txt(name: str, resolver=None) -> list[str]:
    return list((resolver or _default_resolver)(name))


def _require_manager(workspace_id: int, user=None) -> None:
    if user is None:
        user = current_user
    if not can("manage_settings", workspace_id, user):
        raise DomainPermissionDenied("You do not have permission to manage domains.")


def _for_workspace(workspace_id: int, domain: str) -> WorkspaceDomain | None:
    return WorkspaceDomain.query.filter_by(workspace_id=workspace_id, domain=domain).first()


def create_challenge(
    workspace_id: int,
    raw_domain: str | None,
    *,
    user=None,
    now: datetime | None = None,
) -> WorkspaceDomain:
    """Create or refresh a bounded DNS challenge for ``raw_domain``."""
    domain = normalize_domain(raw_domain)
    _require_manager(workspace_id, user)
    moment = _now(now)

    row = _for_workspace(workspace_id, domain)
    if row is not None and row.is_verified:
        raise DomainConflict("This domain is already verified for the workspace.")

    claimed = WorkspaceDomain.query.filter(
        WorkspaceDomain.domain == domain,
        WorkspaceDomain.workspace_id != workspace_id,
    ).first()
    if claimed is not None:
        raise DomainConflict("This domain is already claimed by another organization.")

    if row is None:
        row = WorkspaceDomain(workspace_id=workspace_id, domain=domain)
        db.session.add(row)

    row.verification_token = secrets.token_urlsafe(32)
    row.challenge_expires_at = moment + CHALLENGE_TTL
    row.verified_at = None
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raise DomainConflict("This domain is already claimed by another organization.") from None
    return row


def verify_domain(
    workspace_id: int,
    raw_domain: str | None,
    *,
    user=None,
    now: datetime | None = None,
    resolver=None,
) -> WorkspaceDomain:
    """Verify the challenge by reading the domain's TXT record."""
    domain = normalize_domain(raw_domain)
    _require_manager(workspace_id, user)
    moment = _now(now)

    row = _for_workspace(workspace_id, domain)
    if row is None:
        raise InvalidDomain("Create a verification challenge before verifying the domain.")
    if row.is_verified:
        return row
    if _now(row.challenge_expires_at) <= moment:
        raise ChallengeExpired("The verification challenge has expired. Create a new one.")

    expected = expected_txt_value(row.verification_token)
    if expected not in _resolve_txt(txt_record_name(domain), resolver):
        raise VerificationFailed(
            f"Add a TXT record {txt_record_name(domain)} with the value {expected}."
        )

    row.verified_at = moment
    db.session.commit()
    return row


def set_enforcement(
    workspace_id: int,
    domain_id: int,
    enforce: bool,
    *,
    user=None,
) -> WorkspaceDomain:
    """Turn password-login blocking on or off for a verified domain."""
    _require_manager(workspace_id, user)
    row = WorkspaceDomain.query.filter_by(id=domain_id, workspace_id=workspace_id).first()
    if row is None:
        raise LookupError("Domain not found.")
    if enforce and not row.is_verified:
        raise DomainNotVerified("Verify the domain before enforcing SSO.")
    row.enforce_sso = bool(enforce)
    db.session.commit()
    return row


def list_domains(workspace_id: int, *, user=None) -> list[WorkspaceDomain]:
    """List the workspace's domains for a manager."""
    _require_manager(workspace_id, user)
    return (
        WorkspaceDomain.query.filter_by(workspace_id=workspace_id)
        .order_by(WorkspaceDomain.domain.asc())
        .all()
    )


def enforced_domain_for_email(email: str) -> WorkspaceDomain | None:
    """Return the verified, enforcing domain matching ``email``'s domain."""
    domain = (email or "").rsplit("@", 1)[-1].strip().lower()
    if not domain:
        return None
    return (
        WorkspaceDomain.query.filter_by(domain=domain, enforce_sso=True)
        .filter(WorkspaceDomain.verified_at.isnot(None))
        .first()
    )


def is_email_domain_enforced(email: str) -> bool:
    """Return ``True`` when password login must be blocked for ``email``."""
    return enforced_domain_for_email(email) is not None
