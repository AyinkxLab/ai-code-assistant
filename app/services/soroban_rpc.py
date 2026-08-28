"""Read-only Soroban/Stellar RPC client.

A bounded, SSRF-safe JSON-RPC 2.0 client for the **Stellar RPC** service
(previously *Soroban RPC*). Only the read-only methods are implemented —
the client never signs, simulates, or submits transactions.

Implemented methods (all read-only, verified against the current Stellar RPC
documentation):

- ``getHealth``           node health / ledger retention window
- ``getVersionInfo``      server version information
- ``getLatestLedger``     latest known ledger
- ``getNetwork``          network passphrase / protocol version
- ``getLedgerEntries``    live ledger entry lookup by ``LedgerKey`` (accounts,
                          contract data, contract code, …)
- ``getLedgers``          bounded list of recent ledgers
- ``getTransaction``      a single transaction by hash
- ``getTransactions``     bounded list of recent transactions
- ``getEvents``           filtered contract/system events over a ledger range
- ``getFeeStats``         inclusion fee statistics

Explicitly **not** implemented: ``sendTransaction`` and ``simulateTransaction``
(they touch signing/submission). RPC endpoints come exclusively from the
validated network configuration — never from user or project input — and every
request is bounded by a timeout, a response-body cap, base-URL restriction, and
redirect rejection (fail closed).

Version assumption: implemented against Stellar RPC as of Protocol 22+ / the
v22 ``stellar-xdr`` (the ``getLedgerEntries``-based contract-data model and the
current method set). See ``docs/soroban.md``.
"""

from __future__ import annotations

import json
import logging
import socket
from typing import Any

import requests
from flask import current_app, has_app_context

from app.services.stellar import (
    DEFAULT_TIMEOUT,
    MAX_RESPONSE_BYTES,
    NetworkError,
    StellarError,
    _host_is_private_literal,
    _hostname_is_obviously_private,
    _parse_host,
    resolve_network_config,
    validate_endpoint_url,
)

logger = logging.getLogger(__name__)

#: JSON-RPC request id counter (module-level, never exposed to callers).
_request_id = 0

#: Maximum number of ledger keys accepted per ``getLedgerEntries`` call
#: (the RPC hard limit is 200; we keep a small safety margin).
MAX_LEDGER_KEYS = 100

#: Maximum number of entries/events/ledgers/transactions returned per call.
MAX_RESULTS = 100

#: Maximum characters of raw XDR kept per entry (response bounding).
MAX_XDR_CHARS = 4096


class SorobanRpcError(StellarError):
    """Base exception for Soroban/Stellar RPC failures."""


class SorobanRpcUnavailableError(SorobanRpcError):
    """The RPC endpoint could not be reached or returned a transport error."""


class SorobanRpcResponseError(SorobanRpcError):
    """The RPC endpoint returned a JSON-RPC error payload."""

    def __init__(self, message: str, *, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class SorobanRpcInvalidParamsError(SorobanRpcError):
    """A caller supplied invalid parameters (fail closed before any request)."""


def _next_request_id() -> int:
    global _request_id
    _request_id += 1
    return _request_id


def _clip_xdr(value: str | None) -> str | None:
    """Bound the length of a raw XDR string returned to callers."""
    if not isinstance(value, str):
        return value
    if len(value) <= MAX_XDR_CHARS:
        return value
    return value[:MAX_XDR_CHARS] + "…[truncated]"


def _bound_entries(entries: list[dict]) -> list[dict]:
    """Bound the number and size of raw ledger entries returned."""
    out: list[dict] = []
    for entry in entries[:MAX_RESULTS]:
        out.append(
            {
                "key": _clip_xdr(entry.get("key")),
                "xdr": _clip_xdr(entry.get("xdr")),
                "lastModifiedLedgerSeq": entry.get("lastModifiedLedgerSeq"),
                "liveUntilLedgerSeq": entry.get("liveUntilLedgerSeq"),
            }
        )
    return out


class SorobanRpcClient:
    """Read-only JSON-RPC client for the configured Stellar RPC endpoint.

    Constructed from the same validated network configuration as
    :class:`app.services.stellar.StellarService`. Explicit endpoint overrides
    are allowed only through configuration (or the constructor) and are
    re-validated exactly like Horizon endpoints.
    """

    def __init__(
        self,
        *,
        network: str | None = None,
        rpc_url: str | None = None,
        timeout: int | None = None,
        max_response_bytes: int | None = None,
        max_ledger_keys: int | None = None,
        session: requests.Session | None = None,
        host_resolver=None,
    ) -> None:
        if has_app_context():
            cfg = current_app.config
            network = network or cfg.get("STELLAR_NETWORK")
            rpc_url = rpc_url or cfg.get("STELLAR_RPC_URL")
            timeout = timeout or cfg.get("STELLAR_REQUEST_TIMEOUT")
            max_response_bytes = max_response_bytes or cfg.get("STELLAR_MAX_RESPONSE_BYTES")
            max_ledger_keys = max_ledger_keys or cfg.get("STELLAR_RPC_MAX_KEYS")
        self._config = resolve_network_config(network, None, rpc_url)
        self._timeout = timeout or DEFAULT_TIMEOUT
        self._max_bytes = max_response_bytes or MAX_RESPONSE_BYTES
        self._max_ledger_keys = max_ledger_keys or MAX_LEDGER_KEYS
        self._session = session or requests.Session()
        self._strict_host_validation = True
        if has_app_context():
            self._strict_host_validation = (
                current_app.config.get("STELLAR_STRICT_HOST_VALIDATION", True) is not False
            )
        self._resolver = host_resolver or (lambda host: socket.getaddrinfo(host, None))

    # ------------------------------------------------------------------
    # Public read-only methods
    # ------------------------------------------------------------------

    def get_health(self) -> dict:
        """Return the RPC node's health and ledger retention window."""
        result = self._rpc_call("getHealth")
        return {
            "status": result.get("status"),
            "latestLedger": result.get("latestLedger"),
            "latestLedgerCloseTime": result.get("latestLedgerCloseTime"),
            "oldestLedger": result.get("oldestLedger"),
            "oldestLedgerCloseTime": result.get("oldestLedgerCloseTime"),
            "ledgerRetentionWindow": result.get("ledgerRetentionWindow"),
        }

    def get_version_info(self) -> dict:
        """Return the RPC server's version information (bounded)."""
        result = self._rpc_call("getVersionInfo")
        return {
            "version": result.get("version"),
            "commitHash": result.get("commit_hash") or result.get("commitHash"),
            "buildTimestamp": result.get("build_timestamp") or result.get("buildTimestamp"),
            "protocolVersion": result.get("protocol_version") or result.get("protocolVersion"),
        }

    def get_latest_ledger(self) -> dict:
        """Return the latest known ledger (metadata XDR is bounded)."""
        result = self._rpc_call("getLatestLedger")
        return {
            "id": result.get("id"),
            "protocolVersion": result.get("protocolVersion"),
            "sequence": result.get("sequence"),
            "closeTime": result.get("closeTime"),
            "headerXdr": _clip_xdr(result.get("headerXdr")),
            "metadataXdr": _clip_xdr(result.get("metadataXdr")),
        }

    def get_network(self) -> dict:
        """Return the network passphrase and protocol version served by the node."""
        result = self._rpc_call("getNetwork")
        return {
            "passphrase": result.get("passphrase"),
            "protocolVersion": result.get("protocolVersion"),
            "friendbotUrl": result.get("friendbotUrl"),
        }

    def get_ledger_entries(
        self,
        keys: list[str],
        *,
        xdr_format: str = "base64",
    ) -> dict:
        """Look up live ledger entries by their base64 ``LedgerKey`` values.

        Args:
            keys: Base64 ``LedgerKey`` strings (validated, at most
                ``max_ledger_keys``).
            xdr_format: ``"base64"`` (default) or ``"json"``.

        Returns:
            ``{"entries": [...], "latestLedger": int}`` with bounded entries.
        """
        validated = self._validate_keys(keys)
        params: dict[str, Any] = {"keys": validated}
        if xdr_format == "json":
            params["xdrFormat"] = "json"
        elif xdr_format != "base64":
            raise SorobanRpcInvalidParamsError('xdr_format must be "base64" or "json".')
        result = self._rpc_call("getLedgerEntries", params)
        return {
            "entries": _bound_entries(result.get("entries") or []),
            "latestLedger": result.get("latestLedger"),
        }

    def get_ledgers(self, *, start_ledger: int | None = None, limit: int | None = None) -> dict:
        """Return a bounded list of recent ledgers.

        Args:
            start_ledger: First ledger sequence (inclusive) to return.
            limit: Maximum number of ledgers (capped at ``MAX_RESULTS``).
        """
        pagination = self._bounded_pagination(limit)
        params: dict[str, Any] = {"pagination": pagination}
        if start_ledger is not None:
            params["startLedger"] = self._positive_int(start_ledger, "start_ledger")
        result = self._rpc_call("getLedgers", params)
        return {
            "ledgers": (result.get("ledgers") or [])[:MAX_RESULTS],
            "latestLedger": result.get("latestLedger"),
            "cursor": result.get("cursor"),
        }

    def get_transaction(self, transaction_hash: str, *, xdr_format: str = "base64") -> dict:
        """Return details for a single transaction by hex hash.

        A transaction the node has not seen returns ``status == "NOT_FOUND"``
        (not an error), matching the RPC contract.
        """
        params = {"hash": self._validate_transaction_hash(transaction_hash)}
        if xdr_format == "json":
            params["xdrFormat"] = "json"
        elif xdr_format != "base64":
            raise SorobanRpcInvalidParamsError('xdr_format must be "base64" or "json".')
        result = self._rpc_call("getTransaction", params)
        return self._bounded_transaction(result)

    def get_transactions(
        self, *, start_ledger: int | None = None, limit: int | None = None
    ) -> dict:
        """Return a bounded list of recent transactions.

        Args:
            start_ledger: First ledger sequence (inclusive).
            limit: Maximum number of transactions (capped at ``MAX_RESULTS``).
        """
        pagination = self._bounded_pagination(limit)
        params: dict[str, Any] = {"pagination": pagination}
        if start_ledger is not None:
            params["startLedger"] = self._positive_int(start_ledger, "start_ledger")
        result = self._rpc_call("getTransactions", params)
        txs = (result.get("transactions") or [])[:MAX_RESULTS]
        return {
            "transactions": [self._bounded_transaction(tx) for tx in txs],
            "latestLedger": result.get("latestLedger"),
            "cursor": result.get("cursor"),
        }

    def get_events(
        self,
        *,
        start_ledger: int | None = None,
        end_ledger: int | None = None,
        contract_ids: list[str] | None = None,
        limit: int | None = None,
    ) -> dict:
        """Return a bounded, optionally filtered list of contract events.

        Args:
            start_ledger: First ledger sequence (inclusive).
            end_ledger: Last ledger sequence (inclusive) — exclusive per the
                RPC contract; kept for callers that pass an explicit window.
            contract_ids: At most 5 contract ids to filter on (``C...``).
            limit: Maximum number of events (capped at ``MAX_RESULTS``).

        Returns:
            ``{"events": [...], "latestLedger": int, "oldestLedger": int,
            "cursor": str}`` with raw event topics/values bounded.
        """
        params: dict[str, Any] = {}
        if start_ledger is not None:
            params["startLedger"] = self._positive_int(start_ledger, "start_ledger")
        if end_ledger is not None:
            params["endLedger"] = self._positive_int(end_ledger, "end_ledger")
        if contract_ids:
            params["filters"] = self._validate_event_filters(contract_ids)
        params["pagination"] = self._bounded_pagination(limit)
        result = self._rpc_call("getEvents", params)
        return {
            "events": self._bound_events(result.get("events") or []),
            "latestLedger": result.get("latestLedger"),
            "oldestLedger": result.get("oldestLedger"),
            "latestLedgerCloseTime": result.get("latestLedgerCloseTime"),
            "oldestLedgerCloseTime": result.get("oldestLedgerCloseTime"),
            "cursor": result.get("cursor"),
        }

    def get_fee_stats(self) -> dict:
        """Return inclusion fee statistics (Soroban + classic transactions)."""
        result = self._rpc_call("getFeeStats")
        return {
            "sorobanInclusionFee": result.get("sorobanInclusionFee"),
            "inclusionFee": result.get("inclusionFee"),
            "latestLedger": result.get("latestLedger"),
        }

    # ------------------------------------------------------------------
    # JSON-RPC transport
    # ------------------------------------------------------------------

    def _rpc_call(self, method: str, params: dict[str, Any] | None = None) -> dict:
        """POST a JSON-RPC 2.0 request and return the ``result`` object.

        Fails closed on: unsafe endpoints, redirects, HTTP errors, oversized
        responses, malformed JSON, and RPC error payloads. RPC-level "errors"
        raised by the node (e.g. out-of-range ledger) become
        :class:`SorobanRpcResponseError`.
        """
        if self._config.rpc_url is None:
            raise SorobanRpcUnavailableError(
                f"No RPC endpoint is configured for network {self._config.network.value}."
            )

        request = {
            "jsonrpc": "2.0",
            "id": _next_request_id(),
            "method": method,
            "params": params or {},
        }
        body = json.dumps(request, separators=(",", ":"))

        url = self._config.rpc_url
        self._check_url(url)
        try:
            response = self._session.post(
                url,
                data=body,
                timeout=self._timeout,
                stream=True,
                allow_redirects=False,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "User-Agent": "ai-code-assistant",
                },
            )
        except requests.RequestException as exc:
            raise SorobanRpcUnavailableError(f"Soroban RPC request failed: {exc}") from exc

        with response:
            payload = self._read_response_body(response, url)
            if response.status_code in (300, 301, 302, 303, 307, 308):
                raise SorobanRpcUnavailableError(
                    "Soroban RPC refused a redirect response (fail closed)."
                )
            if response.status_code >= 400:
                raise self._http_error(response.status_code, payload)

        try:
            parsed = json.loads(payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise SorobanRpcUnavailableError("Soroban RPC returned invalid JSON") from exc

        if not isinstance(parsed, dict):
            raise SorobanRpcUnavailableError("Soroban RPC returned a malformed payload")
        if parsed.get("jsonrpc") != "2.0":
            raise SorobanRpcUnavailableError("Soroban RPC payload is not JSON-RPC 2.0")
        if parsed.get("id") != request["id"]:
            raise SorobanRpcUnavailableError("Soroban RPC request id mismatch")
        if "error" in parsed:
            error = parsed.get("error") or {}
            message = error.get("message") if isinstance(error, dict) else None
            code = error.get("code") if isinstance(error, dict) else None
            raise SorobanRpcResponseError(message or "Soroban RPC returned an error", code=code)
        if "result" not in parsed:
            raise SorobanRpcUnavailableError("Soroban RPC payload has no result")
        result = parsed["result"]
        if not isinstance(result, dict):
            raise SorobanRpcUnavailableError("Soroban RPC result is not an object")
        return result

    def _read_response_body(self, response, url: str) -> bytes:
        """Read a bounded response body, enforcing the size cap."""
        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_content(4096):
            size += len(chunk)
            if size > self._max_bytes:
                raise SorobanRpcUnavailableError("Soroban RPC response exceeded size limit")
            chunks.append(chunk)
        return b"".join(chunks)

    def _http_error(self, status_code: int, payload: bytes) -> SorobanRpcUnavailableError:
        """Build a fail-closed error for a non-2xx HTTP response."""
        message = f"Soroban RPC returned HTTP {status_code}"
        if status_code == 404:
            return SorobanRpcResponseError("Soroban RPC endpoint not found (404)", code=-32601)
        try:
            data = json.loads(payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            data = None
        if isinstance(data, dict) and isinstance(data.get("error"), dict):
            error = data["error"]
            if error.get("message"):
                message = f"Soroban RPC error: {error['message']}"
            return SorobanRpcResponseError(message, code=error.get("code"))
        return SorobanRpcUnavailableError(message)

    def _check_url(self, url: str) -> None:
        """Fail closed unless ``url`` is the validated, configured RPC base."""
        allowed_base = self._config.rpc_url.rstrip("/")
        if not url.startswith(allowed_base + "/") and url != allowed_base:
            raise NetworkError(f"Refusing out-of-base Soroban RPC request: {url}")
        if not validate_endpoint_url(url, is_public=self._config.is_public):
            raise NetworkError(f"Refusing unsafe Soroban RPC endpoint: {url}")
        self._assert_public_host_resolution(url)

    def _assert_public_host_resolution(self, url: str) -> None:
        """Reject a public-network host that resolves to a private address.

        Defense-in-depth against SSRF through a misconfigured endpoint; only
        runs for public networks, and DNS failures are tolerated because the
        scheme/literal/base-URL guards still apply.
        """
        if not self._config.is_public or not self._strict_host_validation:
            return
        host = _parse_host(url)[1] if _parse_host(url) else ""
        if not host or _host_is_private_literal(host) or _hostname_is_obviously_private(host):
            return
        try:
            resolved = self._resolver(host)
        except Exception:  # DNS unavailable — the structural guards still apply
            return
        for info in resolved:
            sockaddr = info[4] if isinstance(info, tuple) and len(info) > 4 else info
            ip = sockaddr[0] if isinstance(sockaddr, (tuple, list)) else None
            if ip and _host_is_private_literal(ip):
                raise NetworkError(f"Refusing private address for public network: {ip}")

    # ------------------------------------------------------------------
    # Parameter validation (fail closed before any request)
    # ------------------------------------------------------------------

    def _validate_keys(self, keys: list[str]) -> list[str]:
        if not isinstance(keys, list) or not keys:
            raise SorobanRpcInvalidParamsError("At least one ledger key is required.")
        if len(keys) > self._max_ledger_keys:
            raise SorobanRpcInvalidParamsError(
                f"At most {self._max_ledger_keys} ledger keys are allowed."
            )
        from app.services.stellar_xdr import validate_ledger_key_base64

        validated: list[str] = []
        for key in keys:
            ok, reason = validate_ledger_key_base64(key)
            if not ok:
                raise SorobanRpcInvalidParamsError(f"Invalid ledger key: {reason}")
            validated.append(key.strip())
        return validated

    def _validate_transaction_hash(self, value: str) -> str:
        if not isinstance(value, str):
            raise SorobanRpcInvalidParamsError("A transaction hash is required.")
        text = value.strip().lower()
        if len(text) != 64 or any(c not in "0123456789abcdef" for c in text):
            raise SorobanRpcInvalidParamsError("A transaction hash must be 64 hex characters.")
        return text

    @staticmethod
    def _positive_int(value: int, name: str) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise SorobanRpcInvalidParamsError(f"{name} must be an integer.") from exc
        if number <= 0:
            raise SorobanRpcInvalidParamsError(f"{name} must be a positive integer.")
        return number

    @staticmethod
    def _bounded_pagination(limit: int | None) -> dict:
        if limit is None:
            return {"limit": MAX_RESULTS}
        try:
            number = int(limit)
        except (TypeError, ValueError) as exc:
            raise SorobanRpcInvalidParamsError("limit must be an integer.") from exc
        return {"limit": max(1, min(number, MAX_RESULTS))}

    def _validate_event_filters(self, contract_ids: list[str]) -> list[dict]:
        if not isinstance(contract_ids, list) or not contract_ids:
            raise SorobanRpcInvalidParamsError("At least one contract id is required.")
        if len(contract_ids) > 5:
            raise SorobanRpcInvalidParamsError("At most 5 contract ids are allowed.")
        from app.services.stellar_xdr import validate_contract_address

        for cid in contract_ids:
            if not isinstance(cid, str) or not validate_contract_address(cid):
                raise SorobanRpcInvalidParamsError(f"Invalid contract id: {cid}")
        return [{"contractIds": [cid.strip() for cid in contract_ids]}]

    def _bound_events(self, events: list[dict]) -> list[dict]:
        out: list[dict] = []
        for event in events[:MAX_RESULTS]:
            out.append(
                {
                    "type": event.get("type"),
                    "ledger": event.get("ledger"),
                    "ledgerClosedAt": event.get("ledgerClosedAt"),
                    "contractId": event.get("contractId"),
                    "id": event.get("id"),
                    "txHash": event.get("txHash"),
                    "topic": [_clip_xdr(topic) for topic in (event.get("topic") or [])[:8]],
                    "value": _clip_xdr(event.get("value")),
                }
            )
        return out

    def _bounded_transaction(self, result: dict) -> dict:
        return {
            "status": result.get("status"),
            "txHash": result.get("txHash"),
            "ledger": result.get("ledger"),
            "createdAt": result.get("createdAt"),
            "applicationOrder": result.get("applicationOrder"),
            "feeBump": result.get("feeBump"),
            "latestLedger": result.get("latestLedger"),
            "latestLedgerCloseTime": result.get("latestLedgerCloseTime"),
            "envelopeXdr": _clip_xdr(result.get("envelopeXdr")),
            "resultXdr": _clip_xdr(result.get("resultXdr")),
        }


def get_soroban_rpc_client() -> SorobanRpcClient:
    """Return a :class:`SorobanRpcClient` bound to the current app configuration."""
    return SorobanRpcClient()
