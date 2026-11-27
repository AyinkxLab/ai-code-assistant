"""CLI entry point for the Phase 11 role migration (#605).

All behavior lives in :mod:`app.services.role_migration` so tests and operators
use the same bounded, reversible code path. Structured errors are printed as
one JSON document on stderr and exit with status ``2``.
"""

from __future__ import annotations

import sys

from app.services.role_migration import main

if __name__ == "__main__":  # pragma: no cover - CLI entry point
    sys.exit(main())
