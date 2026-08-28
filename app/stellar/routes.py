"""Read-only Stellar/Soroban developer routes (Phase 8).

Endpoints (all ``@login_required``, all read-only):

- ``GET /stellar``                       — developer page
- ``GET /stellar/api/network``           — configured network + live RPC status
- ``GET /stellar/api/account``           — read-only account inspection
- ``GET /stellar/api/contract``          — read-only contract inspection
- ``GET /stellar/api/ledger-entry``      — ledger entry lookup by base64 key

Authorization: login required. Network endpoints come exclusively from the
validated configuration; callers can never supply a URL (no SSRF). Read-only:
the service and RPC client never sign, simulate, or submit. A light per-user
rate limit keeps unbounded lookups bounded.
"""

from flask import jsonify, render_template, request
from flask_login import login_required

from app.services import ratelimit
from app.services.soroban_rpc import SorobanRpcError
from app.services.stellar import AccountError, NetworkError, StellarError
from app.services.stellar_inspection import (
    inspect_account,
    inspect_contract,
    inspect_ledger_entry,
    network_status,
)
from app.stellar import bp


def _rate_limited() -> bool:
    return not ratelimit.hit(ratelimit.client_key("stellar:"), max_hits=60)


@bp.route("/")
@login_required
def index():
    """Stellar/Soroban developer page (read-only tools)."""
    return render_template("stellar/index.html")


@bp.route("/api/network")
@login_required
def api_network():
    """Return the configured network plus best-effort live RPC status."""
    if _rate_limited():
        return jsonify({"error": "Rate limit exceeded. Try again shortly."}), 429
    return jsonify(network_status())


@bp.route("/api/account")
@login_required
def api_account():
    """Inspect a Stellar account (read-only)."""
    if _rate_limited():
        return jsonify({"error": "Rate limit exceeded. Try again shortly."}), 429
    address = (request.args.get("address") or "").strip()
    if not address:
        return jsonify({"error": "An address (G...) is required."}), 400
    try:
        return jsonify(inspect_account(address))
    except AccountError as exc:
        return jsonify({"error": str(exc)}), 400
    except (NetworkError, StellarError) as exc:
        return jsonify({"error": str(exc)}), 502


@bp.route("/api/contract")
@login_required
def api_contract():
    """Inspect a Soroban contract (read-only, via Stellar RPC)."""
    if _rate_limited():
        return jsonify({"error": "Rate limit exceeded. Try again shortly."}), 429
    contract_id = (request.args.get("address") or "").strip()
    wasm_hash = (request.args.get("wasm_hash") or "").strip() or None
    if not contract_id:
        return jsonify({"error": "A contract id (C...) is required."}), 400
    try:
        return jsonify(inspect_contract(contract_id, wasm_hash=wasm_hash))
    except AccountError as exc:
        return jsonify({"error": str(exc)}), 400
    except SorobanRpcError as exc:
        return jsonify({"error": str(exc)}), 502
    except (NetworkError, StellarError) as exc:
        return jsonify({"error": str(exc)}), 502


@bp.route("/api/ledger-entry")
@login_required
def api_ledger_entry():
    """Look up a live ledger entry by a caller-supplied base64 LedgerKey."""
    if _rate_limited():
        return jsonify({"error": "Rate limit exceeded. Try again shortly."}), 429
    key = (request.args.get("key") or "").strip()
    if not key:
        return jsonify({"error": "A base64 ledger key is required."}), 400
    try:
        return jsonify(inspect_ledger_entry(key))
    except AccountError as exc:
        return jsonify({"error": str(exc)}), 400
    except SorobanRpcError as exc:
        return jsonify({"error": str(exc)}), 502
    except (NetworkError, StellarError) as exc:
        return jsonify({"error": str(exc)}), 502
