"""AI Code Assistant Python SDK (Phase 12).

A small, dependency-free client for the AI Code Assistant public API.

>>> from ai_code_assistant import Client
>>> client = Client("https://api.example.com", api_token="token")
>>> project = client.get_project(42)  # doctest: +SKIP
"""

from __future__ import annotations

from .client import Client, Response
from .errors import (
    APITimeoutError,
    AuthenticationError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
    SDKError,
    ServerError,
    TransportError,
    ValidationError,
)
from .models import Finding, Project, Review, Usage
from .webhooks import (
    DEFAULT_TOLERANCE_SECONDS,
    SIGNATURE_HEADER,
    SIGNATURE_VERSION,
    TIMESTAMP_HEADER,
    compute_signature,
    extract_signature,
    signed_content,
    verify_signature,
)

__version__ = "0.1.0"

__all__ = [
    "DEFAULT_TOLERANCE_SECONDS",
    "SIGNATURE_HEADER",
    "SIGNATURE_VERSION",
    "TIMESTAMP_HEADER",
    "APITimeoutError",
    "AuthenticationError",
    "Client",
    "ConflictError",
    "Finding",
    "NotFoundError",
    "PermissionDeniedError",
    "Project",
    "RateLimitError",
    "Response",
    "Review",
    "SDKError",
    "ServerError",
    "TransportError",
    "Usage",
    "ValidationError",
    "__version__",
    "compute_signature",
    "extract_signature",
    "signed_content",
    "verify_signature",
]
