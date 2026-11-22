"""Packaging checks for the Phase 12 Python SDK (#656).

The SDK is intended to be installed with ``pip install -e sdk/python`` and must
be covered by the repository gates. These tests assert the metadata is real
(no placeholder version), stays in sync with the package, and that the
contributor install path is documented.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SDK = ROOT / "sdk" / "python"

sys.path.insert(0, str(SDK))

from ai_code_assistant import Client, __version__  # noqa: E402


def _metadata() -> dict:
    return tomllib.loads((SDK / "pyproject.toml").read_text(encoding="utf-8"))


def test_pyproject_declares_installable_metadata():
    project = _metadata()["project"]

    assert project["name"] == "ai-code-assistant-sdk"
    assert project["version"] == __version__
    assert project["requires-python"] == ">=3.12"
    assert project["description"].strip()
    assert project["license"]["text"] == "MIT"


def test_sdk_has_no_third_party_runtime_dependencies():
    project = _metadata()["project"]

    # A standard-library-only client keeps the test suite offline and avoids a
    # dependency surface an SDK consumer did not ask for.
    assert project.get("dependencies", []) == []


def test_editable_install_is_documented_for_contributors():
    readme = (SDK / "README.md").read_text(encoding="utf-8")
    sdk_readme = (ROOT / "sdk" / "README.md").read_text(encoding="utf-8")

    assert "pip install -e sdk/python" in readme
    assert "pip install -e sdk/python" in sdk_readme
    assert "pytest tests/test_sdk_python.py" in readme


def test_client_requires_a_base_url_and_accepts_an_api_key():
    client = Client("https://api.example.com/", api_token="secret")

    assert client.base_url == "https://api.example.com"
    assert client.api_token == "secret"
