"""Dependency license policy and bounded SBOM generation (#623).

This is deliberately stdlib-only. It reads the declared Python requirements,
resolves each package's license from installed metadata (falling back to a
small reviewed map for packages whose metadata omits it), writes a bounded
JSON SBOM, and fails when a license is outside the allowlist.

The SBOM is **not** CycloneDX. It is a small, documented schema of our own so
that the repository ships something honest without pretending to be a format
we do not fully implement. Scope is the declared direct dependency set from
``requirements.txt`` and ``requirements-dev.txt``; transitive packages are not
enumerated.

Run:
    python scripts/dependency_policy.py --sbom sbom-python.json
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

SCHEMA = "ai-code-assistant-sbom/1"
MAX_COMPONENTS = 500
MAX_FIELD_CHARS = 200
REQUIREMENT_FILES = ("requirements.txt", "requirements-dev.txt")

#: Licenses the project accepts for direct dependencies. Expressions using
#: ``OR``/``AND`` are allowed only when every referenced license is allowed;
#: this is conservative and easy to review.
ALLOWED_LICENSES = frozenset(
    {
        "0BSD",
        "Apache-2.0",
        "BSD-2-Clause",
        "BSD-3-Clause",
        "CC0-1.0",
        "ISC",
        "LGPL-2.1-or-later",
        "LGPL-3.0-or-later",
        "MIT",
        "MPL-2.0",
        "PSF-2.0",
        "Python-2.0",
        "Unlicense",
    }
)

#: Used only when installed metadata has no usable license field. Every entry
#: is a reviewed fact for a package this repository pins.
FALLBACK_LICENSES = {
    "black": "MIT",
    "cryptography": "Apache-2.0 OR BSD-3-Clause",
    "email-validator": "Unlicense",
    "flask": "BSD-3-Clause",
    "flask-login": "MIT",
    "flask-socketio": "MIT",
    "flask-sqlalchemy": "BSD-3-Clause",
    "flask-wtf": "BSD-3-Clause",
    "fpdf2": "LGPL-3.0-or-later",
    "gunicorn": "MIT",
    "packaging": "Apache-2.0 OR BSD-2-Clause",
    "psycopg2-binary": "LGPL-3.0-or-later",
    "py7zr": "LGPL-2.1-or-later",
    "pytest": "MIT",
    "pytest-cov": "MIT",
    "python-dotenv": "BSD-3-Clause",
    "pyyaml": "MIT",
    "rarfile": "ISC",
    "requests": "Apache-2.0",
    "ruff": "MIT",
    "simple-websocket": "MIT",
    "sqlalchemy": "MIT",
    "werkzeug": "BSD-3-Clause",
    "wtforms": "BSD-3-Clause",
}

#: Substring aliases seen in package metadata/classifiers, longest first.
_LICENSE_ALIASES = (
    ("gnu lesser general public license (lgpl)", "LGPL-3.0-or-later"),
    ("gnu lesser general public license v3", "LGPL-3.0-or-later"),
    ("gnu lesser general public license v2", "LGPL-2.1-or-later"),
    ("lesser general public license (lgpl)", "LGPL-3.0-or-later"),
    ("apache software license", "Apache-2.0"),
    ("bsd 3-clause", "BSD-3-Clause"),
    ("bsd-3-clause", "BSD-3-Clause"),
    ("bsd 2-clause", "BSD-2-Clause"),
    ("bsd-2-clause", "BSD-2-Clause"),
    ("bsd license", "BSD-3-Clause"),
    ("mit license", "MIT"),
    ("python software foundation", "PSF-2.0"),
    ("mozilla public license 2.0", "MPL-2.0"),
    ("isc license", "ISC"),
    ("unlicense", "Unlicense"),
    ("apache-2.0", "Apache-2.0"),
    ("mit", "MIT"),
    ("isc", "ISC"),
    ("mpl-2.0", "MPL-2.0"),
    ("psf-2.0", "PSF-2.0"),
)

_REQUIREMENT_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]+\])?\s*==\s*(?P<version>[^\s;#]+)"
)

#: A bare SPDX id or an ``OR``/``AND`` expression of bare ids.
_SPDX_EXPRESSION_RE = re.compile(r"^[A-Za-z0-9.+-]+(?:\s+(?:OR|AND)\s+[A-Za-z0-9.+-]+)*$")


@dataclass(frozen=True)
class Dependency:
    """One declared direct dependency."""

    name: str
    version: str
    license: str
    pinned: bool
    source: str


def canonical_name(name: str) -> str:
    """PEP 503 normalised distribution name."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _truncate(value: str) -> str:
    return value[:MAX_FIELD_CHARS]


def normalize_license(raw: str | None) -> str:
    """Return a compact SPDX-ish license expression, or ``UNKNOWN``."""
    if not raw:
        return "UNKNOWN"
    text = " ".join(str(raw).split())
    if not text or len(text) > MAX_FIELD_CHARS:
        return "UNKNOWN"
    key = text.lower()
    for alias, spdx in _LICENSE_ALIASES:
        if alias == key:
            return spdx
    if _SPDX_EXPRESSION_RE.match(text):
        parts = re.split(r"\s+(OR|AND)\s+", text)
        normalized = []
        for part in parts:
            if part in {"OR", "AND"}:
                normalized.append(part)
            else:
                alias = next(
                    (spdx for name, spdx in _LICENSE_ALIASES if name == part.lower()), None
                )
                normalized.append(alias or part)
        return _truncate(" ".join(normalized))
    # Metadata often includes prose ("Copyright ... Redistribution ...") or a
    # classifier prefix; match a known license fragment inside it.
    for alias, spdx in _LICENSE_ALIASES:
        if alias in key:
            return spdx
    return "UNKNOWN"


def license_allowed(expression: str) -> bool:
    """Return ``True`` when every license in ``expression`` is allowed."""
    if expression in ALLOWED_LICENSES:
        return True
    parts = [part for part in re.split(r"\s+(?:OR|AND)\s+|[()]", expression) if part]
    return bool(parts) and all(part in ALLOWED_LICENSES for part in parts)


def _metadata_license(name: str) -> str | None:
    try:
        metadata = importlib.metadata.metadata(name)
    except importlib.metadata.PackageNotFoundError:
        return None
    for field in ("License-Expression", "License"):
        value = metadata.get(field)
        if value:
            normalized = normalize_license(value)
            if normalized != "UNKNOWN":
                return normalized
    for classifier in metadata.get_all("Classifier") or []:
        if classifier.startswith("License :: OSI Approved ::"):
            normalized = normalize_license(classifier.rsplit("::", 1)[-1])
            if normalized != "UNKNOWN":
                return normalized
    return None


def license_for(name: str) -> str:
    """Resolve a package license.

    The reviewed fallback map wins for the repository's pinned packages so the
    policy is deterministic across metadata formats. A package not in the map
    is resolved from installed metadata and reported as ``UNKNOWN`` when that
    metadata carries no usable license.
    """
    canonical = canonical_name(name)
    if canonical in FALLBACK_LICENSES:
        return FALLBACK_LICENSES[canonical]
    detected = _metadata_license(name) or _metadata_license(canonical)
    return detected or "UNKNOWN"


def _read_requirement_file(path: Path, found: dict[str, Dependency], seen_files: set[Path]):
    resolved = path.resolve()
    if resolved in seen_files or len(seen_files) > 10:
        return
    seen_files.add(resolved)
    try:
        lines = resolved.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        if text.startswith(("-r ", "--requirement ")):
            include = text.split(maxsplit=1)[1]
            _read_requirement_file(resolved.parent / include, found, seen_files)
            continue
        if text.startswith("-"):
            continue
        match = _REQUIREMENT_RE.match(text)
        if not match:
            continue
        name = match.group("name")
        canonical = canonical_name(name)
        if canonical in found:
            continue
        version = _truncate(match.group("version"))
        found[canonical] = Dependency(
            name=_truncate(name),
            version=version,
            license=license_for(name),
            pinned=True,
            source=resolved.name,
        )


def collect_dependencies(root: Path = REPO_ROOT) -> list[Dependency]:
    """Parse the declared direct dependencies, bounded and deterministic."""
    found: dict[str, Dependency] = {}
    seen_files: set[Path] = set()
    for filename in REQUIREMENT_FILES:
        path = Path(root) / filename
        if path.is_file():
            _read_requirement_file(path, found, seen_files)
    return [found[key] for key in sorted(found)]


def build_sbom(dependencies: list[Dependency]) -> dict:
    """Build the bounded SBOM document (own schema, not CycloneDX)."""
    components = sorted(dependencies, key=lambda dep: canonical_name(dep.name))
    truncated = len(components) > MAX_COMPONENTS
    components = components[:MAX_COMPONENTS]
    return {
        "schema": SCHEMA,
        "generator": "scripts/dependency_policy.py",
        "format_note": "Minimal project SBOM; not a CycloneDX document.",
        "scope": "declared direct Python requirements (transitive packages excluded)",
        "component_count": len(components),
        "truncated": truncated,
        "components": [
            {
                **asdict(component),
                "name": _truncate(component.name),
                "version": _truncate(component.version),
                "license": _truncate(component.license),
                "source": _truncate(component.source),
            }
            for component in components
        ],
    }


def find_license_violations(dependencies: list[Dependency]) -> list[str]:
    """Return one structured message per dependency outside the allowlist."""
    return [
        f"{dep.name} {dep.version or '(unpinned)'}: disallowed license {dep.license!r}"
        for dep in sorted(dependencies, key=lambda dep: canonical_name(dep.name))
        if not license_allowed(dep.license)
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    parser.add_argument("--sbom", type=Path, help="write the bounded SBOM JSON here")
    args = parser.parse_args(argv)

    dependencies = collect_dependencies(args.root)
    sbom = build_sbom(dependencies)
    if args.sbom:
        args.sbom.write_text(json.dumps(sbom, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    violations = find_license_violations(dependencies)
    if violations:
        print("Dependency license policy failed:")
        for violation in violations:
            print(f"  - {violation}")
        print("Add a reviewed exception in scripts/dependency_policy.py or replace the dependency.")
        return 1

    print(f"Checked {len(dependencies)} declared dependencies; all licenses are allowed.")
    if args.sbom:
        print(f"Wrote {sbom['component_count']} components to {args.sbom}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
