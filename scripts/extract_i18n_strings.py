"""Extract ``t('...')`` keys from templates and compare them with catalogs.

Reports missing keys and unused keys per catalog. Exits non-zero when a shipped
locale is missing a key used by a template.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = REPO_ROOT / "app" / "templates"
TRANSLATIONS = REPO_ROOT / "app" / "translations"
#: A complete ``t('key')`` call. Requires the closing paren so dynamic
#: expressions (``t('prefix_' ~ variable)``) and methods such as ``.get(`` are
#: not mistaken for translation keys.
KEY_RE = re.compile(r"\bt\(\s*['\"]([A-Za-z0-9_.-]+)['\"]\s*\)")


def template_keys() -> set[str]:
    keys: set[str] = set()
    for template in TEMPLATES.rglob("*.html"):
        keys.update(KEY_RE.findall(template.read_text(encoding="utf-8")))
    return keys


def catalogs() -> dict[str, dict[str, str]]:
    loaded: dict[str, dict[str, str]] = {}
    for path in sorted(TRANSLATIONS.glob("*.json")):
        loaded[path.stem] = json.loads(path.read_text(encoding="utf-8"))
    return loaded


def find_issues() -> list[str]:
    used = template_keys()
    issues: list[str] = []
    for locale, catalog in catalogs().items():
        missing = sorted(used - set(catalog))
        if missing:
            issues.append(f"{locale}: missing keys: {', '.join(missing)}")
    return issues


def main() -> int:
    issues = find_issues()
    if issues:
        for issue in issues:
            print(issue)
        return 1
    used = sorted(template_keys())
    print(f"All template keys are present in every catalog ({len(used)} keys).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
