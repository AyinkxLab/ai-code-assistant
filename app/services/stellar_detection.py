"""Heuristic, safe detection of Stellar/Soroban projects from indexed files.

Detection is intentionally conservative: a project is only classified as a
Stellar/Soroban project when there is concrete, file-level evidence (a Soroban
crate dependency, a ``#[contractimpl]``/``#[contract]`` attribute, an SDK
dependency, or Stellar configuration). A plain Rust crate with no such signals
is never classified as Soroban.

The module only reads data the caller already holds (``ProjectFile`` rows); it
never makes network calls and never guesses.
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
_CONTRACT_DIR_PREFIXES = ("contracts/", "src/contracts/", "contract/")
_MANIFEST_NAMES = {
    "cargo.toml",
    "package.json",
    "requirements.txt",
    "go.mod",
    "pyproject.toml",
    "gemfile",
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
    evidence: list[str] = field(default_factory=list)

    @property
    def confidence(self) -> str:
        """One of ``none``, ``possible``, or ``likely``.

        ``likely`` requires strong Soroban evidence (crate dependency or Rust
        contract attributes/imports). ``possible`` covers SDK usage, Stellar
        config files, or a ``contracts/`` layout without Soroban markers.
        """
        if self.soroban_cargo_dependency or self.soroban_attribute or self.soroban_import:
            return "likely"
        if (
            self.stellar_sdk_dependency
            or self.stellar_config_file
            or self.soroban_config_dir
            or self.contract_directory
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
            },
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
        if ".soroban" in lower_path or ".soroban/" in lower_path:
            signals.soroban_config_dir = True
            signals.evidence.append(f".soroban config dir: {path}")
        if _is_config_file(path):
            signals.stellar_config_file = True
            signals.evidence.append(f"stellar config file: {path}")

        if content is None:
            continue

        if _is_manifest(path):
            for dep in _manifest_dependency_names(path, content):
                if dep in SOROBAN_CRATES:
                    signals.soroban_cargo_dependency = True
                    signals.evidence.append(f"{path}: soroban crate {dep}")
                elif dep in STELLAR_SDK_DEPENDENCIES:
                    signals.stellar_sdk_dependency = True
                    signals.evidence.append(f"{path}: stellar sdk {dep}")
            continue

        if _is_rust_source(path):
            if any(attr in content for attr in _SOROBAN_ATTRIBUTES):
                signals.soroban_attribute = True
                signals.evidence.append(f"{path}: soroban contract attribute")
            if any(imp in content for imp in _SOROBAN_IMPORTS):
                signals.soroban_import = True
                signals.evidence.append(f"{path}: soroban import")

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
            if (getattr(f, "content", None) is not None or True)
            and (
                _is_config_file(getattr(f, "path", ""))
                or ".soroban" in (getattr(f, "path", "") or "").lower()
            )
        ][:20]
    return metadata
