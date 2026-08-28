"""Heuristic, safe detection of Stellar/Soroban projects from indexed files.

Detection is intentionally conservative: a project is only classified as a
Stellar/Soroban project when there is concrete, file-level evidence (a Soroban
crate dependency, a ``#[contractimpl]``/``#[contract]`` attribute, an SDK
dependency, Stellar configuration, or Stellar/Soroban CLI tooling). A plain
Rust crate with no such signals is never classified as Soroban.

Confidence is reported as one of ``none`` / ``possible`` / ``likely``:

- ``likely`` — strong Soroban (smart-contract) evidence: a Soroban crate in a
  ``Cargo.toml`` or ``#[contractimpl]``/``#[contract]`` attributes /
  ``soroban_sdk::`` imports in Rust sources.
- ``possible`` — Stellar SDK usage (JS/Python/Go), a ``stellar.toml`` /
  ``soroban.toml`` / ``.soroban`` configuration, a ``contracts/`` layout, or
  Stellar/Soroban CLI tooling in build files.
- ``none`` — otherwise.

The module only reads data the caller already holds (``ProjectFile`` rows); it
never makes network calls and never guesses. Network hints (testnet/mainnet/
futurenet) are extracted from configuration files and passphrases, not from
live data.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

# Crates that indicate a Soroban (Rust) contract project.
SOROBAN_CRATES = {
    "soroban-sdk",
    "soroban-auth",
    "soroban-token-sdk",
    "soroban-spec",
    "soroban-cli",
    "soroban-env-host",
    "soroban-rpc",
    "stellar-strkey",
    "stellar-contract-sdk",
    "stellar-contract-env-host",
    "stellar-contract-env",
    "stellar-xdr",
}

# Dependency names (across ecosystems) that indicate Stellar SDK usage.
STELLAR_SDK_DEPENDENCIES = {
    "stellar-sdk",
    "js-stellar-sdk",
    "@stellar/stellar-sdk",
    "@stellar/stellar-base",
    "stellar-base",
    "py-stellar-base",
    "go-stellar-base",
    "stellar-client",
    "stellar-java-sdk",
}

# Rust contract/attr markers found in Soroban source.
_SOROBAN_ATTRIBUTES = ("#[contractimpl]", "#[contract]", "#[contracttype]", "#[contracterror]")
_SOROBAN_IMPORTS = ("soroban_sdk::", "soroban::", "soroban_sdk;")
_SOROBAN_KEYWORDS = ("soroban", "stellar-contract")

_STELLAR_CONFIG_FILES = {
    "stellar.toml",
    "soroban.toml",
    "stellar-config.toml",
    "stellar.json",
    "soroban.json",
}
# Files that commonly encode Stellar/Soroban CLI commands (build tooling).
_CLI_TOOLING_FILES = {
    "makefile",
    "justfile",
    "dockerfile",
    "build.rs",
    "xtask",
}
_CI_WORKFLOW_MARKER = ".github/workflows/"
# Command fragments that indicate real Soroban/Stellar CLI usage. Kept narrow
# to avoid false positives (e.g. a project that merely mentions the word
# "stellar").
_CLI_COMMAND_MARKERS = (
    "soroban contract",
    "soroban build",
    "soroban invoke",
    "soroban deploy",
    "soroban-install",
    "stellar contract",
    "stellar build",
    "stellar xdr",
    "stellar rpc",
    "stellar keys",
    "stellar network",
    "stellar-cli",
)
_CONTRACT_DIR_PREFIXES = ("contracts/", "src/contracts/", "contract/")
_MANIFEST_NAMES = {
    "cargo.toml",
    "package.json",
    "requirements.txt",
    "go.mod",
    "pyproject.toml",
    "gemfile",
}

# Network passphrases / markers that identify a configured Stellar network.
_NETWORK_PASSPHRASE_MARKERS = {
    "mainnet": (
        "public global stellar network",
        "stellar:pubnet",
        "public network ; september 2015",
    ),
    "testnet": ("test sdf network", "stellar:testnet", "testnet ; september 2015"),
    "futurenet": ("future network", "futurenet"),
    "standalone": ("standalone network", "local network", "stellar:standalone"),
}
_NETWORK_FILENAME_MARKERS = {
    "mainnet": ("stellar-mainnet", "soroban-mainnet", "pubnet", "mainnet"),
    "testnet": ("stellar-testnet", "soroban-testnet", "testnet"),
    "futurenet": ("stellar-futurenet", "soroban-futurenet", "futurenet"),
}


@dataclass
class StellarSignals:
    """Evidence collected about a project's Stellar/Soroban usage."""

    soroban_cargo_dependency: bool = False
    soroban_attribute: bool = False
    soroban_import: bool = False
    stellar_sdk_dependency: bool = False
    stellar_config_file: bool = False
    soroban_config_dir: bool = False
    contract_directory: bool = False
    stellar_cli_tooling: bool = False
    network_hints: list[str] = field(default_factory=list)
    relevant_files: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)

    @property
    def confidence(self) -> str:
        """One of ``none``, ``possible``, or ``likely``.

        ``likely`` requires strong Soroban evidence (crate dependency or Rust
        contract attributes/imports). ``possible`` covers SDK usage, Stellar
        config files, Soroban/Stellar CLI tooling, or a ``contracts/`` layout
        without Soroban markers.
        """
        if self.soroban_cargo_dependency or self.soroban_attribute or self.soroban_import:
            return "likely"
        if (
            self.stellar_sdk_dependency
            or self.stellar_config_file
            or self.soroban_config_dir
            or self.contract_directory
            or self.stellar_cli_tooling
        ):
            return "possible"
        return "none"

    @property
    def is_stellar(self) -> bool:
        """True when any credible Stellar/Soroban signal was found."""
        return self.confidence != "none"

    @property
    def is_soroban(self) -> bool:
        """True only for strong Soroban (smart-contract) evidence."""
        return self.soroban_cargo_dependency or self.soroban_attribute or self.soroban_import

    def to_dict(self) -> dict[str, Any]:
        """Dictionary representation for metadata and AI prompts."""
        return {
            "is_stellar": self.is_stellar,
            "is_soroban": self.is_soroban,
            "confidence": self.confidence,
            "signals": {
                "soroban_cargo_dependency": self.soroban_cargo_dependency,
                "soroban_attribute": self.soroban_attribute,
                "soroban_import": self.soroban_import,
                "stellar_sdk_dependency": self.stellar_sdk_dependency,
                "stellar_config_file": self.stellar_config_file,
                "soroban_config_dir": self.soroban_config_dir,
                "contract_directory": self.contract_directory,
                "stellar_cli_tooling": self.stellar_cli_tooling,
            },
            "network_hints": sorted(set(self.network_hints))[:10],
            "relevant_files": sorted(set(self.relevant_files))[:20],
            "evidence": self.evidence[:20],
        }


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1].lower()


def _is_cargo_manifest(path: str) -> bool:
    return _basename(path) == "cargo.toml"


def _is_manifest(path: str) -> bool:
    return _basename(path) in _MANIFEST_NAMES or (
        _basename(path).startswith("requirements") and _basename(path).endswith(".txt")
    )


def _is_rust_source(path: str) -> bool:
    return path.rsplit(".", 1)[-1].lower() == "rs"


def _is_config_file(path: str) -> bool:
    return _basename(path) in _STELLAR_CONFIG_FILES


def _is_cli_tooling(path: str) -> bool:
    lower = path.lower()
    if _basename(path) in _CLI_TOOLING_FILES:
        return True
    return lower.startswith(_CI_WORKFLOW_MARKER) and lower.endswith((".yml", ".yaml"))


def _cargo_dependencies(content: str) -> Iterable[str]:
    """Yield dependency names found in a Cargo.toml body."""
    section = None
    for raw in content.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            section = line[1:].split("]")[0].strip()
            continue
        if section not in ("dependencies", "dev-dependencies", "workspace.dependencies"):
            continue
        name = line.split("=")[0].split()[0].strip()
        if name:
            yield name


def _parse_json_dependencies(content: str) -> Iterable[str]:
    import json as _json

    try:
        data = _json.loads(content)
    except (ValueError, TypeError):
        return
    for section in ("dependencies", "devDependencies", "peerDependencies"):
        for name in data.get(section) or {}:
            if isinstance(name, str):
                yield name


def _requirements_dependencies(content: str) -> Iterable[str]:
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "-", "--")):
            continue
        for sep in ("=", "<", ">", "~", "!"):
            line = line.split(sep)[0]
        name = line.strip().split()[0]
        if name:
            yield name


def _go_dependencies(content: str) -> Iterable[str]:
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith(")"):
            continue
        if stripped.startswith("require (") or stripped == "require (":
            continue
        if stripped.startswith("require"):
            parts = stripped.split()[1:]
        elif stripped.startswith("("):
            parts = stripped[1:].split()
        else:
            parts = stripped.split()
        if parts and "/" in parts[0]:
            yield parts[0].rsplit("/", 1)[-1]


def _toml_dependencies(content: str) -> Iterable[str]:
    try:
        import tomllib
    except ImportError:  # pragma: no cover - Python <3.11
        tomllib = None
    if tomllib is None:
        return
    try:
        data = tomllib.loads(content)
    except Exception:
        return
    for section in ("dependencies", "optional-dependencies"):
        deps = data.get("project", {}).get(section) or data.get(section) or []
        if isinstance(deps, dict):
            deps = list(deps)
        for raw in deps:
            yield str(raw).split("=")[0].split("<")[0].split(">")[0].split("[")[0].strip()


def _gemfile_dependencies(content: str) -> Iterable[str]:
    import re as _re

    for match in _re.finditer(r'^\s*gem\s+["\']([^"\']+)["\']', content, _re.MULTILINE):
        yield match.group(1)


def _manifest_dependency_names(path: str, content: str) -> Iterable[str]:
    base = _basename(path)
    if base == "cargo.toml":
        yield from _cargo_dependencies(content)
    elif base in ("package.json", "package-lock.json"):
        yield from _parse_json_dependencies(content)
    elif base == "requirements.txt" or (base.startswith("requirements") and base.endswith(".txt")):
        yield from _requirements_dependencies(content)
    elif base == "go.mod":
        yield from _go_dependencies(content)
    elif base == "pyproject.toml":
        yield from _toml_dependencies(content)
    elif base == "gemfile":
        yield from _gemfile_dependencies(content)


def _is_contract_layout(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in _CONTRACT_DIR_PREFIXES)


def _network_hints_for_content(path: str, content: str) -> list[str]:
    """Return network hints from a file's path name and content passphrases."""
    hints: set[str] = set()
    lower_path = path.lower()
    lower_content = (content or "").lower()

    for network, markers in _NETWORK_FILENAME_MARKERS.items():
        if any(marker in lower_path for marker in markers):
            hints.add(network)
    for network, markers in _NETWORK_PASSPHRASE_MARKERS.items():
        if any(marker in lower_content for marker in markers):
            hints.add(network)
    return sorted(hints)


def _cli_tooling_hint(path: str, content: str) -> bool:
    """Return ``True`` when build tooling/CI invokes Stellar/Soroban CLI commands."""
    lower = (content or "").lower()
    return any(marker in lower for marker in _CLI_COMMAND_MARKERS)


def detect_stellar_project(files: Iterable[Any]) -> StellarSignals:
    """Detect Stellar/Soroban signals from an iterable of files.

    Each ``files`` item must expose ``.path`` and ``.content`` attributes
    (e.g. :class:`app.models.project_file.ProjectFile`).
    """
    signals = StellarSignals()
    for file in files:
        path = getattr(file, "path", "") or ""
        content = getattr(file, "content", None)
        lower_path = path.lower()

        # Layout signals apply to any file, content or not.
        if _is_contract_layout(path):
            signals.contract_directory = True
            signals.evidence.append(f"contract layout: {path}")
            signals.relevant_files.append(path)
        if ".soroban" in lower_path or ".soroban/" in lower_path:
            signals.soroban_config_dir = True
            signals.evidence.append(f".soroban config dir: {path}")
            signals.relevant_files.append(path)
        if _is_config_file(path):
            signals.stellar_config_file = True
            signals.evidence.append(f"stellar config file: {path}")
            signals.relevant_files.append(path)

        if content is None:
            continue

        if _is_manifest(path):
            for dep in _manifest_dependency_names(path, content):
                if dep in SOROBAN_CRATES:
                    signals.soroban_cargo_dependency = True
                    signals.evidence.append(f"{path}: soroban crate {dep}")
                    signals.relevant_files.append(path)
                elif dep in STELLAR_SDK_DEPENDENCIES:
                    signals.stellar_sdk_dependency = True
                    signals.evidence.append(f"{path}: stellar sdk {dep}")
                    signals.relevant_files.append(path)
            continue

        if _is_rust_source(path):
            if any(attr in content for attr in _SOROBAN_ATTRIBUTES):
                signals.soroban_attribute = True
                signals.evidence.append(f"{path}: soroban contract attribute")
                signals.relevant_files.append(path)
            if any(imp in content for imp in _SOROBAN_IMPORTS):
                signals.soroban_import = True
                signals.evidence.append(f"{path}: soroban import")
                signals.relevant_files.append(path)

        if _is_cli_tooling(path) and _cli_tooling_hint(path, content):
            signals.stellar_cli_tooling = True
            signals.evidence.append(f"{path}: stellar/soroban cli tooling")
            signals.relevant_files.append(path)

        if _is_config_file(path) or ".soroban" in lower_path:
            for hint in _network_hints_for_content(path, content):
                if hint not in signals.network_hints:
                    signals.network_hints.append(hint)
                    signals.evidence.append(f"{path}: network hint {hint}")
                    signals.relevant_files.append(path)

    return signals


def project_stellar_metadata(project) -> dict[str, Any]:
    """Return a dict of Stellar metadata for an indexed project (bounded)."""
    files = list(project.files.all()) if hasattr(project.files, "all") else list(project.files)
    signals = detect_stellar_project(files)
    metadata = signals.to_dict()
    if signals.is_stellar:
        metadata["network_files"] = [
            f.path
            for f in files
            if (
                _is_config_file(getattr(f, "path", ""))
                or ".soroban" in (getattr(f, "path", "") or "").lower()
            )
        ][:20]
    return metadata


def _is_network_named_file(path: str) -> bool:
    """Return ``True`` when a file's name encodes a Stellar network
    (e.g. ``stellar-testnet.toml``)."""
    lower_path = path.lower()
    return any(
        marker in lower_path for markers in _NETWORK_FILENAME_MARKERS.values() for marker in markers
    )


def detect_stellar_network(files: Iterable[Any]) -> dict[str, Any]:
    """Return a network guess from file names/config passphrases (never live data).

    Returns ``{"network": "testnet"|"mainnet"|"futurenet"|"standalone"|None,
    "evidence": [...]}``. A single authoritative network is returned only when
    the evidence is unambiguous; conflicting or absent hints yield ``None``.
    """
    hints: dict[str, list[str]] = {}
    for file in files:
        path = getattr(file, "path", "") or ""
        content = getattr(file, "content", None)
        lower_path = path.lower()
        if not (_is_config_file(path) or ".soroban" in lower_path or _is_network_named_file(path)):
            continue
        if content is None:
            continue
        for hint in _network_hints_for_content(path, content):
            hints.setdefault(hint, []).append(path)

    if not hints:
        return {"network": None, "evidence": []}
    evidence: list[str] = []
    for network, paths in hints.items():
        evidence.extend(f"{p}: {network}" for p in paths)
    # If multiple distinct networks are hinted, the project is ambiguous.
    networks = set(hints)
    if len(networks) > 1:
        return {"network": None, "evidence": evidence[:20]}
    return {"network": next(iter(networks)), "evidence": evidence[:20]}
