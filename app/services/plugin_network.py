"""Plugin outbound network policy: an allowlist + SSRF guard (#193).

Plugins may include code that makes outbound HTTP requests. This module is the
single, reusable guard every such request must pass. It keeps plugins from
reaching internal services (SSRF) by:

* allowing **https only** (no plaintext, no other schemes);
* denying **private / loopback / link-local / reserved / multicast / unspecified**
  targets and obviously-private hostnames **by default**;
* resolving the target host (best effort) and denying names that resolve to a
  private address;
* restricting requests to hosts on an operator-configured **allowlist** — an
  empty allowlist denies every plugin request (fail closed).

The policy is read from the application configuration (see
``app/config.py`` and ``.env.example``)::

    PLUGIN_NETWORK_ALLOWLIST=api.example.com,*.trusted.test
    PLUGIN_NETWORK_HTTPS_ONLY=1
    PLUGIN_NETWORK_ALLOW_PRIVATE=0
    PLUGIN_NETWORK_TIMEOUT=15
    PLUGIN_NETWORK_MAX_BYTES=2097152

Plugin code should make requests through :func:`guarded_request` (or
:func:`guarded_get` / :func:`guarded_post`) rather than calling ``requests``
directly, so the guard runs before any socket is opened. A request that violates
the policy raises :class:`PluginNetworkError`.
"""

from __future__ import annotations

import logging
import socket
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import requests
from flask import current_app, has_app_context

from app.services.plugins import PluginError

# Reuse the Stellar SSRF primitives so host handling stays consistent across the
# codebase (the RPC client imports them the same way).
from app.services.stellar import (
    _host_is_private_literal,
    _hostname_is_obviously_private,
    _parse_host,
)

logger = logging.getLogger(__name__)

#: Default cap on a plugin response body (bytes) when not overridden by config.
DEFAULT_MAX_BYTES = 2 * 1024 * 1024
#: Default request timeout (seconds) when not overridden by config.
DEFAULT_TIMEOUT = 15

#: A host resolver returns the IP address strings a hostname resolves to.
HostResolver = Callable[[str], list[str]]


class PluginNetworkError(PluginError):
    """A plugin outbound request violated the network policy."""


class _DNSFailure(Exception):
    """Raised internally when a hostname cannot be resolved."""


def _default_resolver(host: str) -> list[str]:
    """Resolve ``host`` to IP address strings, or raise :class:`_DNSFailure`."""
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, OSError) as exc:
        raise _DNSFailure(str(exc)) from exc
    addresses: list[str] = []
    for info in infos:
        sockaddr = info[4] if isinstance(info, tuple) and len(info) > 4 else None
        if sockaddr and sockaddr[0]:
            addresses.append(str(sockaddr[0]))
    return addresses


def parse_allowlist(raw: Any) -> tuple[str, ...]:
    """Normalize an allowlist value into a tuple of lowercased entries.

    Accepts a comma-separated string (as read from the environment) or an
    iterable of strings. Blank entries are ignored.
    """
    if raw is None:
        return ()
    items = raw if isinstance(raw, (list, tuple, set, frozenset)) else str(raw).split(",")
    return tuple(str(item).strip().lower() for item in items if str(item).strip())


def _host_matches_entry(host: str, entry: str) -> bool:
    """Return ``True`` when ``host`` matches a single allowlist ``entry``.

    ``*`` matches any host; ``*.example.com`` matches subdomains only; any other
    entry must match the host exactly.
    """
    if entry == "*":
        return True
    if entry.startswith("*."):
        suffix = entry[2:]
        return bool(suffix) and host.endswith("." + suffix)
    return host == entry


@dataclass(frozen=True)
class PluginNetworkPolicy:
    """Immutable plugin outbound network policy."""

    allowlist: tuple[str, ...] = ()
    https_only: bool = True
    allow_private: bool = False
    timeout: int = DEFAULT_TIMEOUT
    max_bytes: int = DEFAULT_MAX_BYTES
    strict_dns: bool = False

    @classmethod
    def from_config(cls, config: Any | None = None) -> PluginNetworkPolicy:
        """Build a policy from ``config`` (a Flask config or mapping).

        Falls back to the current app config when available, then to safe
        defaults (empty allowlist = deny all) when there is no app context.
        """
        source = config
        if source is None and has_app_context():
            source = current_app.config
        if source is None:
            source = {}

        def _get(key: str, default: Any) -> Any:
            try:
                return source.get(key, default)
            except AttributeError:  # not a mapping — use defaults
                return default

        return cls(
            allowlist=parse_allowlist(_get("PLUGIN_NETWORK_ALLOWLIST", "")),
            https_only=bool(_get("PLUGIN_NETWORK_HTTPS_ONLY", True)),
            allow_private=bool(_get("PLUGIN_NETWORK_ALLOW_PRIVATE", False)),
            timeout=int(_get("PLUGIN_NETWORK_TIMEOUT", DEFAULT_TIMEOUT)),
            max_bytes=int(_get("PLUGIN_NETWORK_MAX_BYTES", DEFAULT_MAX_BYTES)),
            strict_dns=bool(_get("PLUGIN_NETWORK_STRICT_DNS", False)),
        )

    def is_host_allowed(self, host: str) -> bool:
        """Return ``True`` when ``host`` is explicitly on the allowlist."""
        normalized = (host or "").lower()
        return any(_host_matches_entry(normalized, entry) for entry in self.allowlist)

    def check_url(self, url: str, *, resolver: HostResolver | None = None) -> str:
        """Validate ``url`` against the policy and return it, or raise.

        Raises :class:`PluginNetworkError` for any violation (fail closed).
        """
        if not isinstance(url, str) or not url.strip():
            raise PluginNetworkError("Plugin network request requires a non-empty URL")
        text = url.strip()

        parsed = _parse_host(text)
        if parsed is None:
            raise PluginNetworkError(f"Unsupported plugin network URL: {url!r}")
        scheme, host, _port = parsed

        if self.https_only and scheme != "https":
            raise PluginNetworkError("Plugin network requests must use https")

        try:
            info = urlparse(text)
        except ValueError:
            info = None
        if info is not None and (info.username or info.password):
            raise PluginNetworkError("Plugin network requests must not embed credentials")

        if not self.is_host_allowed(host):
            raise PluginNetworkError(f"Host is not on the plugin network allowlist: {host}")

        if not self.allow_private:
            if _host_is_private_literal(host) or _hostname_is_obviously_private(host):
                raise PluginNetworkError(f"Refusing private or internal plugin host: {host}")
            self._assert_public_resolution(host, resolver)

        return text

    def is_allowed(self, url: str, *, resolver: HostResolver | None = None) -> bool:
        """Return ``True`` when ``url`` satisfies the policy (never raises)."""
        try:
            self.check_url(url, resolver=resolver)
        except PluginNetworkError:
            return False
        return True

    def _assert_public_resolution(self, host: str, resolver: HostResolver | None) -> None:
        """Deny a hostname that resolves to a private address.

        Best effort by default: a DNS failure is tolerated because the scheme,
        literal-IP and allowlist guards still apply. Set ``strict_dns`` to fail
        closed on unresolvable hosts as well.
        """
        if _host_is_private_literal(host):
            return
        resolve = resolver or _default_resolver
        try:
            addresses = resolve(host)
        except _DNSFailure as exc:
            if self.strict_dns:
                raise PluginNetworkError(f"Could not resolve plugin host: {host}") from exc
            return
        except Exception as exc:  # a misbehaving resolver must not bypass the guard
            if self.strict_dns:
                raise PluginNetworkError(f"Could not resolve plugin host: {host}") from exc
            return
        for address in addresses:
            if _host_is_private_literal(str(address)):
                raise PluginNetworkError(
                    f"Plugin host {host} resolves to a private address: {address}"
                )


def get_plugin_network_policy() -> PluginNetworkPolicy:
    """Return the plugin network policy from the current app config."""
    return PluginNetworkPolicy.from_config()


def guard_plugin_url(
    url: str,
    *,
    policy: PluginNetworkPolicy | None = None,
    resolver: HostResolver | None = None,
) -> str:
    """Validate ``url`` against the plugin network policy and return it.

    This is the reusable guard: callers receive the (stripped) URL when it is
    allowed and a :class:`PluginNetworkError` otherwise.
    """
    return (policy or get_plugin_network_policy()).check_url(url, resolver=resolver)


def is_plugin_url_allowed(
    url: str,
    *,
    policy: PluginNetworkPolicy | None = None,
    resolver: HostResolver | None = None,
) -> bool:
    """Return ``True`` when the plugin policy allows ``url`` (never raises)."""
    return (policy or get_plugin_network_policy()).is_allowed(url, resolver=resolver)


def guarded_request(
    method: str,
    url: str,
    *,
    policy: PluginNetworkPolicy | None = None,
    session: requests.Session | None = None,
    resolver: HostResolver | None = None,
    timeout: int | None = None,
    max_bytes: int | None = None,
    **kwargs: Any,
) -> requests.Response:
    """Run a plugin HTTP request only after it passes the network guard.

    The guard runs **before** any socket is opened. Redirects are never
    followed, the timeout is bounded, and a declared response size above the cap
    is refused. Raises :class:`PluginNetworkError` on a policy violation.
    """
    active = policy or get_plugin_network_policy()
    safe_url = active.check_url(url, resolver=resolver)

    kwargs["allow_redirects"] = False
    kwargs["timeout"] = timeout if timeout is not None else active.timeout

    client = session or requests.Session()
    response = client.request(method, safe_url, **kwargs)

    limit = max_bytes if max_bytes is not None else active.max_bytes
    declared = response.headers.get("Content-Length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise PluginNetworkError(f"Plugin response exceeds the {limit} byte cap")
    return response


def guarded_get(url: str, **kwargs: Any) -> requests.Response:
    """Allowed-only ``GET`` for plugin code (see :func:`guarded_request`)."""
    return guarded_request("GET", url, **kwargs)


def guarded_post(url: str, **kwargs: Any) -> requests.Response:
    """Allowed-only ``POST`` for plugin code (see :func:`guarded_request`)."""
    return guarded_request("POST", url, **kwargs)


__all__ = [
    "DEFAULT_MAX_BYTES",
    "DEFAULT_TIMEOUT",
    "HostResolver",
    "PluginNetworkError",
    "PluginNetworkPolicy",
    "get_plugin_network_policy",
    "guard_plugin_url",
    "guarded_get",
    "guarded_post",
    "guarded_request",
    "is_plugin_url_allowed",
    "parse_allowlist",
]
