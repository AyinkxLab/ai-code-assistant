"""Validate the documented multi-region production configuration (#711).

The guide shows the environment variables an operator sets. This loads the same
values through the real production config class in a subprocess, so the example
cannot drift from the code without a failing test.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_documented_production_environment_loads():
    env = {
        **os.environ,
        "APP_ENV": "production",
        "SECRET_KEY": "a" * 64,
        "DATABASE_URL": "postgresql://aica:secret@primary.db.example.net:5432/aica",
        "SOCKETIO_CORS_ALLOWED_ORIGINS": "https://app.example.com",
    }
    script = (
        "import json\n"
        "from app.config import ProductionConfig\n"
        "config = ProductionConfig()\n"
        "print(json.dumps({\n"
        "    'uri': config.SQLALCHEMY_DATABASE_URI,\n"
        "    'secure': config.SESSION_COOKIE_SECURE,\n"
        "    'httponly': config.SESSION_COOKIE_HTTPONLY,\n"
        "    'samesite': config.SESSION_COOKIE_SAMESITE,\n"
        "}))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    loaded = json.loads(result.stdout.strip().splitlines()[-1])

    assert loaded["uri"].startswith("postgresql://")
    assert loaded["secure"] is True
    assert loaded["httponly"] is True
    assert loaded["samesite"] == "Lax"
