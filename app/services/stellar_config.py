"""Read-only Stellar/Soroban network configuration generation (issue #183).

Generates the project's supported Soroban CLI network configuration document
(``.soroban/config.toml``) from the app's validated network presets. The helper
is a pure function of the configured networks: it makes no network calls, writes
no files, and never emits secrets.

Format
------
The repository already uses ``.soroban/config.toml`` with a ``[network.<name>]``
table (``rpc_url`` + ``network_passphrase``): see
``app/services/soroban_scaffold.py`` and the ``.soroban`` signal in
``app/services/stellar_detection.py``. Generating that same format keeps the
output reviewable and lets it round-trip through the detector. No undocumented
``.soroban`` layout is assumed.

Safety
------
* No secrets: only the network name/passphrase and the validated RPC endpoint
  are emitted; the generated output is checked for secret-looking keys/values.
* No arbitrary URLs: networks are resolved through ``resolve_network_config``,
  which only accepts the built-in presets (and validated operator endpoints).
* No silent overwrite: ``generate_config_file`` refuses to replace an existing
  target path unless an explicit ``overwrite`` is requested.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from types import SimpleNamespace
from typing import Any

from app.services.stellar import (
    StellarError,
    normalize_network_selection,
    resolve_network_config,
)
from app.services.stellar_detection import detect_stellar_project

#: Target path for the generated Soroban CLI network config.
CONFIG_PATH = ".soroban/config.toml"

#: Canonical network value -> config section name.
_SECTION_BY_NETWORK = {
    "mainnet": "mainnet",
    "testnet": "testnet",
    "futurenet": "futurenet",
    "custom": "local",
}

#: Network value -> the detector's network-hint label.
_DETECTION_HINT = {
    "mainnet": "mainnet",
    "testnet": "testnet",
    "futurenet": "futurenet",
    "custom": "standalone",
}

_SECRET_KEY_HINTS = ("secret", "private", "seed", "mnemonic", "password", "token")
_STELLAR_SECRET_RE = re.compile(r"\bS[A-Z2-7]{55}\b")


class StellarConfigError(ValueError):
    """Raised when Stellar/Soroban configuration cannot be generated safely."""


def supported_config_networks() -> list[str]:
    """Return the network values that can be generated (stable order)."""
    return list(_SECTION_BY_NETWORK)


def _resolve(network: str | None):
    """Return ``(network_value, NetworkConfig)`` for a requested network."""
    if network is None or not str(network).strip():
        config = resolve_network_config(None)
        return config.network.value, config
    value = normalize_network_selection(network)
    return value, resolve_network_config(value)


def generate_soroban_config(network: str | None = None) -> str:
    """Return a ``.soroban/config.toml`` document for ``network``.

    ``None`` uses the configured/selected network. Only the built-in networks
    (resolved by :func:`resolve_network_config`) are accepted; arbitrary URLs are
    never accepted. Raises :class:`StellarConfigError` for unsupported input.
    """
    try:
        value, config = _resolve(network)
    except StellarError as exc:
        raise StellarConfigError(str(exc)) from exc
    if value not in _SECTION_BY_NETWORK:
        raise StellarConfigError(f"Unsupported network for config generation: {network!r}.")
    section = _SECTION_BY_NETWORK[value]
    lines = [
        "# Generated Stellar/Soroban network configuration (read-only reference).",
        "# Contains no credentials. Review before committing to your project.",
        f"[network.{section}]",
    ]
    if config.rpc_url:
        lines.append(f'rpc_url = "{config.rpc_url}"')
    lines.append(f'network_passphrase = "{config.network_passphrase}"')
    return "\n".join(lines) + "\n"


def _has_secret_key(data: Any, *, depth: int = 0) -> bool:
    """Return ``True`` when a parsed document contains a secret-looking key."""
    if depth > 5:
        return False
    if isinstance(data, dict):
        for key, value in data.items():
            if any(hint in str(key).lower() for hint in _SECRET_KEY_HINTS):
                return True
            if _has_secret_key(value, depth=depth + 1):
                return True
    elif isinstance(data, list):
        return any(_has_secret_key(item, depth=depth + 1) for item in data)
    return False


def validate_generated_config(text: str, network: str | None = None) -> dict:
    """Validate a generated config document and return its network table.

    Checks that the document parses as TOML, has the expected
    ``[network.<name>]`` table whose values match the resolved network, contains
    no secret-looking keys or Stellar secret seeds, and is recognized by the
    project's own Stellar detector (structure + network-hint round trip).

    Raises :class:`StellarConfigError` on any failure.
    """
    import tomllib

    try:
        value, config = _resolve(network)
    except StellarError as exc:
        raise StellarConfigError(str(exc)) from exc
    if value not in _SECTION_BY_NETWORK:
        raise StellarConfigError(f"Unsupported network for config validation: {network!r}.")

    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise StellarConfigError(f"Generated config is not valid TOML: {exc}") from exc

    section = _SECTION_BY_NETWORK[value]
    table = (data.get("network") or {}).get(section)
    if not isinstance(table, dict):
        raise StellarConfigError(f"Generated config is missing [network.{section}].")
    if table.get("network_passphrase") != config.network_passphrase:
        raise StellarConfigError("Generated config passphrase does not match the network preset.")
    if config.rpc_url and table.get("rpc_url") != config.rpc_url:
        raise StellarConfigError("Generated config rpc_url does not match the network preset.")
    if _has_secret_key(data) or _STELLAR_SECRET_RE.search(text):
        raise StellarConfigError("Generated config must not contain secrets.")

    signals = detect_stellar_project([SimpleNamespace(path=CONFIG_PATH, content=text)])
    if not (signals.stellar_config_file or signals.soroban_config_dir):
        raise StellarConfigError("Generated config was not recognized by Stellar detection.")
    expected_hint = _DETECTION_HINT[value]
    if expected_hint not in signals.network_hints:
        raise StellarConfigError(
            f"Generated config did not yield the expected '{expected_hint}' network hint."
        )
    return table


def generate_config_file(
    network: str | None = None,
    *,
    existing_paths: Iterable[Any] = (),
    target_path: str = CONFIG_PATH,
    overwrite: bool = False,
) -> dict:
    """Return an import-ready file row for the generated network config.

    ``existing_paths`` (path strings or objects exposing ``.path``) represents
    the project's current files; when ``target_path`` is among them and
    ``overwrite`` is not set, generation is refused. No file is written to disk.
    """
    text = generate_soroban_config(network)
    validate_generated_config(text, network)
    existing = {
        item if isinstance(item, str) else getattr(item, "path", "") for item in existing_paths
    }
    if target_path in existing and not overwrite:
        raise StellarConfigError(
            f"{target_path} already exists; pass overwrite=True to replace it."
        )
    raw = text.encode("utf-8")
    return {
        "path": target_path,
        "size": len(raw),
        "is_binary": False,
        "language": "toml",
        "content": text,
    }
