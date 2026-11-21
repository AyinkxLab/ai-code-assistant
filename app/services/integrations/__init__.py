"""Notification integrations: Slack, Microsoft Teams, and email.

Each connector stores its secrets through the shared encrypted
``integration_credentials`` service, keeps a non-secret health snapshot, and
delivers bounded event payloads with bounded retries. See
``docs/notification-integrations.md``.
"""

from app.services.integrations.common import (
    INTEGRATIONS,
    IntegrationDeliveryError,
    IntegrationError,
    IntegrationNotConfigured,
    IntegrationNotFound,
    IntegrationPermissionError,
    IntegrationValidationError,
)

__all__ = [
    "INTEGRATIONS",
    "IntegrationDeliveryError",
    "IntegrationError",
    "IntegrationNotConfigured",
    "IntegrationNotFound",
    "IntegrationPermissionError",
    "IntegrationValidationError",
]
