# Accessibility audit (WCAG 2.1 AA)

This page records the main-flow audit for issue #712. The automated check is
`scripts/accessibility_audit.py`; it runs in CI and in
`tests/test_accessibility_audit.py`. Screenshots for each finding are attached
to the tracking issue, because binary images are not checked into this
repository.

## Scope

- Home dashboard, authentication pages, chat, workspaces, GitHub browser,
  reviews, billing, plugins, and the shared navigation.
- Keyboard-only navigation, focus management, landmark structure, labels, form
  semantics, and text contrast on the dark theme.

## Findings and status

| Area | Finding | Status | Evidence |
| ---- | ------- | ------ | -------- |
| Document | `<html>` had no language or direction, which breaks screen-reader pronunciation and RTL. | Fixed | `base.html` now sets `lang` and `dir` from the active locale. |
| Navigation | The primary nav was not a labelled landmark. | Fixed | `aria-label="Primary"` on `<nav>` (asserted in `tests/test_accessibility.py`). |
| Keyboard | No skip link to bypass repeated navigation. | Fixed | `.skip-link` targets `#main-content`; focus styling uses `:focus-visible`. |
| Dialogs | Workspace and file dialogs lacked modal semantics. | Fixed | `role="dialog"`, `aria-modal="true"`, and `aria-labelledby` are asserted by tests. |
| Modal focus | The mutation-observer path returned before focusing because the dialog was already visible, so opening a dialog left focus behind it. | Fixed | `a11y.js` now focuses on both direct and observer opens; `tests/js/a11y.test.js` passes. |
| Focus order | Positive `tabindex` values would reorder focus unpredictably. | Fixed | The audit rejects any `tabindex="1"`-style value. |
| Contrast | Body text, muted text, and links must stay ≥ 4.5:1 on the dark background. | Verified | `scripts/accessibility_audit.py` computes the ratios from theme tokens. |
| RTL | Arabic pages need mirrored layout and left-to-right code blocks. | Fixed | `rtl.css` uses logical properties and keeps code LTR. |
| Forms | Icon-only test buttons relied on surrounding context. | Fixed | Each control has a visible label or a `visually-hidden` label. |

## Manual review checklist

- [ ] Tab through each main flow; focus is always visible and order is logical.
- [ ] Every dialog traps focus and returns focus to its trigger on close.
- [ ] All form controls have an accessible name.
- [ ] Status and error messages use `role="status"` or `role="alert"`.
- [ ] Content reflows at 320 CSS px width without horizontal scrolling.
- [ ] `prefers-reduced-motion` is respected for non-essential animation.
- [ ] Arabic (RTL) pages mirror layout while code and identifiers stay LTR.

## Running the checks

```bash
python scripts/accessibility_audit.py
pytest tests/test_accessibility.py tests/test_accessibility_audit.py -q
```
