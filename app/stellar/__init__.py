"""Stellar blueprint: read-only Stellar/Soroban developer tools.

Provides a standalone developer page plus read-only APIs for inspecting the
configured Stellar network, accounts, contracts, and raw ledger entries. All
endpoints are login-required, read-only, and bound to the validated network
configuration — there is no way for a caller to supply an endpoint URL, and
nothing ever signs or submits transactions. See ``app/services/stellar.py``,
``app/services/soroban_rpc.py``, and ``app/services/stellar_inspection.py``.
"""

from flask import Blueprint

bp = Blueprint("stellar", __name__, url_prefix="/stellar")

from app.stellar import routes  # noqa: E402,F401
