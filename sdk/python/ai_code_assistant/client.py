"""HTTP client for the AI Code Assistant public API.

Features (Phase 12, issues #657 and #661):

* API-token authentication via ``Authorization: Bearer``.
* Configurable request timeouts.
* Bounded retries that only apply to idempotent requests and transient status
  codes, honoring ``Retry-After`` when the server sends it.
* Structured, typed errors that preserve the raw server code and request id.
* Typed models for projects, reviews, findings and usage.
"""

from __future__ import annotations

import json as jsonlib
import random
import socket
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib import error, parse
from urllib import request as urlrequest

from . import errors
from .errors import (
    APITimeoutError,
    SDKError,
    TransportError,
    ValidationError,
    error_from_response,
)
from .models import Project, Review, Usage

__all__ = ["Client", "Response"]

_DEFAULT_USER_AGENT = "ai-code-assistant-python/0.1.0"


@dataclass(frozen=True)
class Response:
    """A decoded API response."""

    status_code: int
    headers: Mapping[str, str]
    data: Any
    request_id: str | None = None


class _HTTPFailure(Exception):
    def __init__(self, mapped: SDKError, retry_after: float | None) -> None:
        super().__init__(str(mapped))
        self.error = mapped
        self.retry_after = retry_after


class _TransportFailure(Exception):
    def __init__(self, mapped: SDKError) -> None:
        super().__init__(str(mapped))
        self.error = mapped


def _header(headers: Any, name: str) -> str | None:
    if headers is None:
        return None
    value = headers.get(name)
    if value is not None:
        return str(value)
    lowered = name.lower()
    for key, candidate in getattr(headers, "items", lambda: ())():
        if str(key).lower() == lowered:
            return str(candidate)
    return None


def _decode_body(raw: bytes, content_type: str) -> Any:
    if not raw:
        return None
    text = raw.decode("utf-8", "replace")
    if "json" in content_type.lower():
        try:
            return jsonlib.loads(text)
        except ValueError:
            return text
    try:
        return jsonlib.loads(text)
    except ValueError:
        return text


def _payload_request_id(payload: Any) -> str | None:
    if not isinstance(payload, Mapping):
        return None
    for key in ("request_id", "requestId"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    try:
        seconds = float(value)
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


def _items(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, Mapping) and isinstance(payload.get("items"), list):
        return payload["items"]
    raise ValidationError("expected a list response from the API")


def _resource_id(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValidationError(f"{label} must be a positive integer")
    return value


class Client:
    """A small, dependency-free client for the public API.

    >>> client = Client("https://api.example.com", api_token="token")
    >>> project = client.get_project(42)
    """

    def __init__(
        self,
        base_url: str,
        *,
        api_token: str | None = None,
        timeout: float = 30.0,
        max_retries: int = 2,
        backoff_factor: float = 0.5,
        max_backoff: float = 8.0,
        user_agent: str = _DEFAULT_USER_AGENT,
        jitter: bool = True,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        if not isinstance(base_url, str):
            raise ValueError("base_url must be a string")
        parsed = parse.urlparse(base_url.strip())
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("base_url must be an absolute http(s) URL")
        if api_token is not None and not isinstance(api_token, str):
            raise ValueError("api_token must be a string or None")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError("timeout must be a positive number of seconds")
        if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 0:
            raise ValueError("max_retries must be a non-negative integer")
        if (
            isinstance(backoff_factor, bool)
            or not isinstance(backoff_factor, (int, float))
            or backoff_factor < 0
        ):
            raise ValueError("backoff_factor must be a non-negative number")
        if (
            isinstance(max_backoff, bool)
            or not isinstance(max_backoff, (int, float))
            or max_backoff < 0
        ):
            raise ValueError("max_backoff must be a non-negative number")

        self.base_url = base_url.strip().rstrip("/")
        self.api_token = api_token
        self.timeout = float(timeout)
        self.max_retries = max_retries
        self.backoff_factor = float(backoff_factor)
        self.max_backoff = float(max_backoff)
        self.user_agent = user_agent
        self.jitter = jitter
        self._sleep = sleep or time.sleep

    def __repr__(self) -> str:
        return f"Client(base_url={self.base_url!r}, max_retries={self.max_retries})"

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        headers: Mapping[str, str] | None = None,
        idempotent: bool | None = None,
    ) -> Response:
        """Send a request and return the decoded :class:`Response`.

        Retries are only attempted when the request is idempotent **and** the
        failure is transient (a :data:`~ai_code_assistant.errors.RETRYABLE_STATUSES`
        status or a transport failure).
        """
        if not isinstance(method, str) or not method.strip():
            raise ValueError("method must be a non-empty string")
        method = method.strip().upper()
        if idempotent is None:
            idempotent = method in errors.IDEMPOTENT_METHODS

        url = self._build_url(path, params)
        body = None
        request_headers: dict[str, str] = {
            "Accept": "application/json",
            "User-Agent": self.user_agent,
        }
        if json is not None:
            try:
                body = jsonlib.dumps(json, separators=(",", ":")).encode("utf-8")
            except (TypeError, ValueError) as exc:
                raise ValueError("json payload is not serializable") from exc
            request_headers["Content-Type"] = "application/json"
        if self.api_token:
            request_headers["Authorization"] = f"Bearer {self.api_token}"
        if headers:
            request_headers.update({str(key): str(value) for key, value in headers.items()})

        attempt = 0
        while True:
            attempt += 1
            try:
                return self._attempt(method, url, body, request_headers)
            except _HTTPFailure as exc:
                if (
                    exc.error.status_code not in errors.RETRYABLE_STATUSES
                    or not idempotent
                    or attempt > self.max_retries
                ):
                    raise exc.error from None
                self._sleep(self._retry_delay(attempt, exc.retry_after))
            except _TransportFailure as exc:
                if not idempotent or attempt > self.max_retries:
                    raise exc.error from None
                self._sleep(self._retry_delay(attempt, None))

    def get(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Response:
        return self.request("GET", path, params=params, headers=headers)

    def post(
        self,
        path: str,
        *,
        json: Any = None,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Response:
        return self.request(
            "POST", path, json=json, params=params, headers=headers, idempotent=False
        )

    def put(
        self,
        path: str,
        *,
        json: Any = None,
        headers: Mapping[str, str] | None = None,
    ) -> Response:
        return self.request("PUT", path, json=json, headers=headers, idempotent=True)

    def patch(
        self,
        path: str,
        *,
        json: Any = None,
        headers: Mapping[str, str] | None = None,
    ) -> Response:
        return self.request("PATCH", path, json=json, headers=headers, idempotent=False)

    def delete(self, path: str, *, headers: Mapping[str, str] | None = None) -> Response:
        return self.request("DELETE", path, headers=headers, idempotent=True)

    # -- Typed resource helpers ------------------------------------------------

    def list_projects(self, *, page: int = 1, per_page: int = 20) -> list[Project]:
        response = self.get("/api/projects", params={"page": page, "per_page": per_page})
        return [Project.from_dict(item) for item in _items(response.data)]

    def get_project(self, project_id: int) -> Project:
        project_id = _resource_id(project_id, "project_id")
        return Project.from_dict(self.get(f"/api/projects/{project_id}").data)

    def list_reviews(
        self, *, project_id: int | None = None, page: int = 1, per_page: int = 20
    ) -> list[Review]:
        params: dict[str, Any] = {"page": page, "per_page": per_page, "project_id": project_id}
        response = self.get("/api/reviews", params=params)
        return [Review.from_dict(item) for item in _items(response.data)]

    def get_usage(self, *, project_id: int | None = None) -> Usage:
        response = self.get("/api/usage", params={"project_id": project_id})
        return Usage.from_dict(response.data)

    # -- Internals -------------------------------------------------------------

    def _build_url(self, path: str, params: Mapping[str, Any] | None) -> str:
        if not isinstance(path, str) or not path.strip():
            raise ValueError("path must be a non-empty string")
        if path.startswith(("http://", "https://")):
            raise ValueError("path must be relative; set base_url for the host")
        url = f"{self.base_url}/{path.lstrip('/')}"
        if params:
            query = parse.urlencode(
                {key: value for key, value in params.items() if value is not None}, doseq=True
            )
            if query:
                url = f"{url}?{query}"
        return url

    def _attempt(
        self, method: str, url: str, body: bytes | None, headers: Mapping[str, str]
    ) -> Response:
        prepared = urlrequest.Request(url, data=body, headers=dict(headers), method=method)
        try:
            with urlrequest.urlopen(prepared, timeout=self.timeout) as response:
                payload = _decode_body(
                    response.read(), _header(response.headers, "Content-Type") or ""
                )
                request_id = _header(response.headers, errors.REQUEST_ID_HEADER) or (
                    _payload_request_id(payload)
                )
                return Response(
                    status_code=response.status,
                    headers=dict(response.headers.items()),
                    data=payload,
                    request_id=request_id,
                )
        except error.HTTPError as exc:
            raw = exc.read()
            payload = _decode_body(raw, _header(exc.headers, "Content-Type") or "")
            request_id = _header(exc.headers, errors.REQUEST_ID_HEADER) or _payload_request_id(
                payload
            )
            mapped = error_from_response(exc.code, payload, request_id=request_id)
            retry_after = _parse_retry_after(_header(exc.headers, "Retry-After"))
            raise _HTTPFailure(mapped, retry_after) from None
        except TimeoutError as exc:
            raise _TransportFailure(
                APITimeoutError(f"Request timed out after {self.timeout:g}s")
            ) from exc
        except error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                mapped = APITimeoutError(f"Request timed out after {self.timeout:g}s")
            else:
                mapped = TransportError(f"Unable to reach {self.base_url}: {exc.reason}")
            raise _TransportFailure(mapped) from exc
        except (socket.gaierror, OSError) as exc:
            raise _TransportFailure(
                TransportError(f"Unable to reach {self.base_url}: {exc}")
            ) from exc

    def _retry_delay(self, attempt: int, retry_after: float | None) -> float:
        if retry_after is not None:
            return max(0.0, min(retry_after, self.max_backoff))
        base = self.backoff_factor * (2 ** (attempt - 1))
        if base <= 0:
            return 0.0
        if self.jitter:
            base += random.uniform(0.0, base * 0.1)
        return min(base, self.max_backoff)
