"""Static WCAG 2.1 AA audit for templates and theme tokens (#712).

This is a deterministic, offline complement to manual review. It checks the
landmarks, labels, focus styling, modal semantics, and text contrast that the
main flows rely on. It is not a substitute for a real accessibility audit.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = REPO_ROOT / "app" / "templates"
STYLE = REPO_ROOT / "app" / "static" / "css" / "style.css"

MIN_CONTRAST = 4.5
CONTRAST_PAIRS = (
    ("--text", "--bg"),
    ("--text-muted", "--bg"),
    ("--accent", "--bg"),
)


def _hex_to_rgb(value: str) -> tuple[float, float, float]:
    raw = value.strip().lstrip("#")
    if len(raw) == 3:
        raw = "".join(ch * 2 for ch in raw)
    return tuple(int(raw[index : index + 2], 16) / 255 for index in (0, 2, 4))


def _luminance(rgb: tuple[float, float, float]) -> float:
    def channel(value: float) -> float:
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4

    red, green, blue = (channel(value) for value in rgb)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast_ratio(foreground: str, background: str) -> float:
    """Return the WCAG contrast ratio of two hex colours."""
    first = _luminance(_hex_to_rgb(foreground))
    second = _luminance(_hex_to_rgb(background))
    lighter, darker = max(first, second), min(first, second)
    return (lighter + 0.05) / (darker + 0.05)


def _css_variables(css: str) -> dict[str, str]:
    return {
        f"--{name}": value
        for name, value in re.findall(r"--([a-z0-9-]+)\s*:\s*(#[0-9a-fA-F]{3,6})", css)
    }


def find_violations(root: Path = REPO_ROOT) -> list[str]:
    """Return human-readable accessibility violations."""
    problems: list[str] = []
    templates = root / "app" / "templates"
    style = root / "app" / "static" / "css" / "style.css"
    base = templates / "base.html"

    base_text = base.read_text(encoding="utf-8")
    required_base = {
        'class="skip-link"': "skip link",
        'id="main-content"': "main landmark",
        'aria-label="Primary"': "labelled navigation",
        'dir="{{ text_direction }}"': "document direction",
        'lang="{{ current_locale }}"': "document language",
        'id="i18n-catalog"': "shared translation catalog",
    }
    for snippet, label in required_base.items():
        if snippet not in base_text:
            problems.append(f"base.html: missing {label} ({snippet})")

    for template in templates.rglob("*.html"):
        text = template.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), 1):
            if re.search(r'tabindex="[1-9]', line):
                problems.append(f"{template.relative_to(root)}:{number}: positive tabindex")
            if re.search(r"<img\b(?![^>]*\balt=)", line):
                problems.append(f"{template.relative_to(root)}:{number}: image without alt")
        if 'role="dialog"' in text:
            if 'aria-modal="true"' not in text:
                problems.append(f"{template.relative_to(root)}: dialog without aria-modal")
            if "aria-labelledby=" not in text:
                problems.append(f"{template.relative_to(root)}: dialog without aria-labelledby")

    css = style.read_text(encoding="utf-8")
    if ":focus-visible" not in css:
        problems.append("style.css: missing :focus-visible styling")
    if "outline: 2px solid var(--accent)" not in css:
        problems.append("style.css: focus outline is not the themed 2px outline")

    variables = _css_variables(css)
    for foreground, background in CONTRAST_PAIRS:
        if foreground not in variables or background not in variables:
            problems.append(f"style.css: missing {foreground} or {background} token")
            continue
        ratio = contrast_ratio(variables[foreground], variables[background])
        if ratio < MIN_CONTRAST:
            problems.append(
                f"style.css: {foreground} on {background} contrast "
                f"{ratio:.2f} is below {MIN_CONTRAST}"
            )

    return problems


def main() -> int:
    problems = find_violations(REPO_ROOT)
    if problems:
        print("Accessibility audit found issues:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("Accessibility audit passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
