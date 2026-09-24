"""Unit tests for the plugin outbound network guard (#193).

Cover the allowlist policy and SSRF guard in ``app.services.plugin_network``:
allowlisted hosts, non-allowlisted targets, loopback/private/link-local/reserved
ranges, private DNS resolution, non-https schemes, URL hygiene, and the bounded
``guarded_request`` helper.
"""

from __future__ import annotations

import socket

import pytest

from app.services.plugin_network import (
    DEFAULT_MAX_BYTES,
    DEFAULT_TIMEOUT,
    PluginNetworkError,
    PluginNetworkPolicy,
    get_plugin_network_policy,
    guard_plugin_url,
    guarded_get,
    guarded_post,
    guarded_request,
    is_plugin_url_allowed,
    parse_allowlist,
)

PUBLIC_IP = "93.184.216.34"
PRIVATE_IP = "10.1.2.3"


def _resolves_to(*addresses: str):
    """Return a resolver that maps any host to ``addresses``."""
    return lambda host: list(addresses)


PUBLIC_RESOLVER = _resolves_to(PUBLIC_IP)
PRIVATE_RESOLVER = _resolves_to(PRIVATE_IP)


def _dns_failure(host: str):
    raise socket.gaierror("name resolution failed")


ALLOW_ALL = PluginNetworkPolicy(allowlist=("*",))


class TestAllowlist:
    def test_empty_allowlist_denies_all(self):
        policy = PluginNetworkPolicy()
        assert policy.allowlist == ()
        assert policy.is_allowed("https://example.com", resolver=PUBLIC_RESOLVER) is False
        with pytest.raises(PluginNetworkError, match="allowlist"):
            policy.check_url("https://example.com", resolver=PUBLIC_RESOLVER)

    def test_exact_host_allowed(self):
        policy = PluginNetworkPolicy(allowlist=("api.example.com",))
        assert policy.is_allowed("https://api.example.com/v1", resolver=PUBLIC_RESOLVER)

    def test_non_allowlisted_host_rejected(self):
        policy = PluginNetworkPolicy(allowlist=("api.example.com",))
        assert policy.is_allowed("https://evil.example.com", resolver=PUBLIC_RESOLVER) is False

    def test_wildcard_subdomain_allowed(self):
        policy = PluginNetworkPolicy(allowlist=("*.trusted.test",))
        assert policy.is_allowed("https://cdn.trusted.test", resolver=PUBLIC_RESOLVER)

    def test_wildcard_does_not_match_apex(self):
        policy = PluginNetworkPolicy(allowlist=("*.trusted.test",))
        assert policy.is_allowed("https://trusted.test", resolver=PUBLIC_RESOLVER) is False

    def test_star_allows_any_public_host(self):
        assert ALLOW_ALL.is_allowed("https://anywhere.example.com", resolver=PUBLIC_RESOLVER)

    def test_allowlist_is_case_and_trailing_dot_insensitive(self):
        policy = PluginNetworkPolicy(allowlist=("api.example.com",))
        assert policy.is_allowed("https://API.Example.com./", resolver=PUBLIC_RESOLVER)

    def test_explicit_port_does_not_change_host_match(self):
        policy = PluginNetworkPolicy(allowlist=("api.example.com",))
        assert policy.is_allowed("https://api.example.com:8443/x", resolver=PUBLIC_RESOLVER)

    def test_parse_allowlist_handles_csv_and_blanks(self):
        assert parse_allowlist("a.com, *.b.com , ,c.com") == ("a.com", "*.b.com", "c.com")
        assert parse_allowlist(None) == ()
        assert parse_allowlist(["A.com", " b.com "]) == ("a.com", "b.com")


class TestSchemeAndHygiene:
    @pytest.mark.parametrize(
        "url",
        [
            "http://api.example.com",
            "ftp://api.example.com",
            "file:///etc/passwd",
            "not-a-url",
            "https://",
            "/relative/path",
        ],
    )
    def test_non_https_or_malformed_rejected(self, url):
        assert ALLOW_ALL.is_allowed(url, resolver=PUBLIC_RESOLVER) is False

    def test_http_allowed_only_when_https_disabled(self):
        policy = PluginNetworkPolicy(allowlist=("*",), https_only=False)
        assert policy.is_allowed("http://api.example.com", resolver=PUBLIC_RESOLVER)

    def test_https_allowed(self):
        assert ALLOW_ALL.is_allowed("https://api.example.com", resolver=PUBLIC_RESOLVER)

    def test_credentials_in_url_rejected(self):
        assert (
            ALLOW_ALL.is_allowed("https://user:pass@api.example.com", resolver=PUBLIC_RESOLVER)
            is False
        )

    def test_percent_encoded_host_rejected(self):
        assert ALLOW_ALL.is_allowed("https://%31%32%37.0.0.1/", resolver=PUBLIC_RESOLVER) is False

    def test_check_url_returns_stripped_url(self):
        policy = PluginNetworkPolicy(allowlist=("api.example.com",))
        assert policy.check_url("  https://api.example.com/x  ", resolver=PUBLIC_RESOLVER) == (
            "https://api.example.com/x"
        )

    def test_empty_url_rejected(self):
        with pytest.raises(PluginNetworkError):
            ALLOW_ALL.check_url("   ", resolver=PUBLIC_RESOLVER)


class TestPrivateTargets:
    @pytest.mark.parametrize(
        "host",
        [
            "10.0.0.1",
            "172.16.5.4",
            "192.168.1.10",
            "127.0.0.1",
            "169.254.10.10",  # link-local
            "0.0.0.0",  # unspecified
            "224.0.0.1",  # multicast
            "[::1]",  # loopback v6
            "[fc00::1]",  # unique-local v6
            "[fe80::1]",  # link-local v6
        ],
    )
    def test_private_and_special_literals_rejected(self, host):
        assert ALLOW_ALL.is_allowed(f"https://{host}/", resolver=PUBLIC_RESOLVER) is False

    @pytest.mark.parametrize(
        "host",
        ["localhost", "db.localhost", "svc.local", "box.internal", "thing.lan"],
    )
    def test_obviously_private_hostnames_rejected(self, host):
        assert ALLOW_ALL.is_allowed(f"https://{host}/", resolver=PUBLIC_RESOLVER) is False

    def test_hostname_resolving_to_private_rejected(self):
        assert ALLOW_ALL.is_allowed("https://api.example.com", resolver=PRIVATE_RESOLVER) is False

    def test_dns_failure_tolerated_by_default(self):
        assert ALLOW_ALL.is_allowed("https://api.example.com", resolver=_dns_failure)

    def test_strict_dns_rejects_unresolvable_host(self):
        policy = PluginNetworkPolicy(allowlist=("*",), strict_dns=True)
        assert policy.is_allowed("https://api.example.com", resolver=_dns_failure) is False

    def test_allow_private_escape_hatch(self):
        policy = PluginNetworkPolicy(allowlist=("*",), allow_private=True)
        assert policy.is_allowed("https://127.0.0.1/", resolver=PUBLIC_RESOLVER)
        assert policy.is_allowed("https://localhost/", resolver=PRIVATE_RESOLVER)


class TestPolicyFromConfig:
    def test_defaults_are_fail_closed(self):
        policy = PluginNetworkPolicy.from_config({})
        assert policy.allowlist == ()
        assert policy.https_only is True
        assert policy.allow_private is False
        assert policy.timeout == DEFAULT_TIMEOUT
        assert policy.max_bytes == DEFAULT_MAX_BYTES
        assert policy.strict_dns is False

    def test_reads_overrides_from_config_mapping(self):
        policy = PluginNetworkPolicy.from_config(
            {
                "PLUGIN_NETWORK_ALLOWLIST": "api.example.com,*.trusted.test",
                "PLUGIN_NETWORK_HTTPS_ONLY": False,
                "PLUGIN_NETWORK_ALLOW_PRIVATE": True,
                "PLUGIN_NETWORK_TIMEOUT": 5,
                "PLUGIN_NETWORK_MAX_BYTES": 1024,
                "PLUGIN_NETWORK_STRICT_DNS": True,
            }
        )
        assert policy.allowlist == ("api.example.com", "*.trusted.test")
        assert policy.https_only is False
        assert policy.allow_private is True
        assert policy.timeout == 5
        assert policy.max_bytes == 1024
        assert policy.strict_dns is True

    def test_get_policy_without_app_context_is_fail_closed(self):
        assert get_plugin_network_policy().allowlist == ()


class _FakeResponse:
    def __init__(self, headers: dict[str, str] | None = None):
        self.headers = headers or {}


class _FakeSession:
    def __init__(self, response: _FakeResponse | None = None):
        self.calls: list[tuple] = []
        self.response = response or _FakeResponse()

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.response


class TestGuardedRequest:
    def test_blocked_request_never_opens_a_socket(self):
        session = _FakeSession()
        with pytest.raises(PluginNetworkError):
            guarded_request("GET", "http://api.example.com", policy=ALLOW_ALL, session=session)
        assert session.calls == []

    def test_allowed_request_sets_safe_defaults(self):
        session = _FakeSession()
        guarded_request(
            "GET",
            "https://api.example.com/x",
            policy=ALLOW_ALL,
            session=session,
            resolver=PUBLIC_RESOLVER,
        )
        method, url, kwargs = session.calls[0]
        assert (method, url) == ("GET", "https://api.example.com/x")
        assert kwargs["allow_redirects"] is False
        assert kwargs["timeout"] == ALLOW_ALL.timeout

    def test_explicit_timeout_is_honoured(self):
        session = _FakeSession()
        guarded_request(
            "GET",
            "https://api.example.com/x",
            policy=ALLOW_ALL,
            session=session,
            resolver=PUBLIC_RESOLVER,
            timeout=3,
        )
        assert session.calls[0][2]["timeout"] == 3

    def test_oversized_response_rejected(self):
        session = _FakeSession(_FakeResponse({"Content-Length": "999999"}))
        with pytest.raises(PluginNetworkError, match="byte cap"):
            guarded_request(
                "GET",
                "https://api.example.com/x",
                policy=PluginNetworkPolicy(allowlist=("*",), max_bytes=10),
                session=session,
                resolver=PUBLIC_RESOLVER,
            )

    def test_guarded_get_and_post_delegate(self):
        for helper, method in ((guarded_get, "GET"), (guarded_post, "POST")):
            session = _FakeSession()
            helper(
                "https://api.example.com/x",
                policy=ALLOW_ALL,
                session=session,
                resolver=PUBLIC_RESOLVER,
            )
            assert session.calls[0][0] == method


class TestModuleHelpers:
    def test_guard_plugin_url_returns_allowed_url(self):
        policy = PluginNetworkPolicy(allowlist=("api.example.com",))
        assert (
            guard_plugin_url("https://api.example.com/x", policy=policy, resolver=PUBLIC_RESOLVER)
            == "https://api.example.com/x"
        )

    def test_guard_plugin_url_raises_for_blocked_url(self):
        policy = PluginNetworkPolicy(allowlist=("api.example.com",))
        with pytest.raises(PluginNetworkError):
            guard_plugin_url("https://evil.example.com", policy=policy, resolver=PUBLIC_RESOLVER)

    def test_is_plugin_url_allowed_returns_bool(self):
        policy = PluginNetworkPolicy(allowlist=("api.example.com",))
        assert is_plugin_url_allowed(
            "https://api.example.com", policy=policy, resolver=PUBLIC_RESOLVER
        )
        assert not is_plugin_url_allowed(
            "https://evil.example.com", policy=policy, resolver=PUBLIC_RESOLVER
        )
