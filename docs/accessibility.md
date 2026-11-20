# Accessibility: keyboard navigation and focus

This page records the keyboard contract for the server-rendered UI. It covers
what the application guarantees today, how to verify it, and what is not yet
covered.

## Skip link and landmarks

Every page extends `app/templates/base.html` and starts with a visible-on-focus
skip link:

```html
<a class="skip-link" href="#main-content">Skip to main content</a>
...
<main id="main-content" class="container main-content" tabindex="-1">
```

The link is the first focusable element on the page. Activating it (Enter)
moves focus to the `main` landmark, so keyboard users can bypass the header.
The header navigation is a single `<nav aria-label="Primary">` landmark.

## Focus visibility

`app/static/css/style.css` restores an `outline: 2px solid var(--accent)` for
`:focus-visible` on links, buttons, form controls, and anything with a
`tabindex`. Several controls still clear the default outline on mouse focus
(`:focus`) for the dark theme; the `:focus-visible` rules at the end of the
stylesheet take precedence for keyboard focus only.

Do not remove the `:focus-visible` block. If a new component sets
`outline: none` on `:focus`, add its selector to the keyboard-focus group.

## Dialogs

The two modal surfaces (`#workspace-modal` on the workspaces page and
`#file-modal` on the GitHub repository page) follow one contract:

| Requirement | Implementation |
| ----------- | -------------- |
| Semantics | `role="dialog"`, `aria-modal="true"`, `aria-labelledby` pointing at the dialog heading |
| Hidden when closed | `hidden` attribute; `.modal[hidden] { display: none; }` keeps the flex layout from showing it |
| Focus on open | `app/static/js/a11y.js` focuses the first focusable element inside the dialog |
| Tab / Shift+Tab | Cycles inside the dialog and wraps at both ends |
| Escape | Closes the dialog |
| Focus on close | Returns to the control that opened the dialog |
| No keyboard trap | Escape always closes; focus never moves to content behind an open dialog |

Feature scripts keep toggling `dialog.hidden`. `a11y.js` observes that
attribute, so existing open/close code does not need to duplicate focus
handling. The module is dependency-free and loaded from `base.html` before the
page-specific scripts.

### Adding a dialog

1. Put `class="modal"` on the dialog container and give it `role="dialog"`,
   `aria-modal="true"`, and `aria-labelledby="<heading id>"`.
2. Give the heading a matching `id`.
3. Toggle the container's `hidden` attribute to open and close it.
4. Keep at least one focusable control inside (a close button is enough).

`a11y.js` discovers `.modal` and `[role="dialog"]` elements at page load.

## Forms and menus

- Form controls have a visible `<label>` or an `aria-label`; the workspace
  dialog labels both fields with `for`/`id` pairs.
- The application has no custom JavaScript menus. Navigation is ordinary links,
  so browser keyboard behaviour (Tab, Enter, Shift+Tab) applies.
- Flash messages are announced through `role="status" aria-live="polite"`.

## Verification

Automated checks:

```bash
pytest tests/test_accessibility.py
node --test tests/js/a11y.test.js   # if the JS toolchain is installed
```

The Python tests assert the server-rendered contract: skip link and its target,
the navigation landmark, dialog roles and labels, no positive `tabindex`, and
the presence of the focus-visible CSS. The JavaScript test asserts focus
movement, Tab wrapping, Escape handling, and focus return using jsdom.

**Not performed in CI:** real-browser keyboard testing (NVDA/JAWS/VoiceOver,
Safari, Firefox) and automated axe scans. jsdom does not lay out the page, so
`getClientRects()`-based visibility heuristics are not exercised. Run the
manual checklist below before a release that changes dialogs or navigation.

## Manual checklist

1. Tab from the top of a page: the skip link appears first and jumps to main.
2. Open the workspace dialog with the keyboard: focus lands inside it.
3. Tab through every control and past the last one: focus wraps to the first.
4. Shift+Tab from the first control: focus wraps to the last.
5. Press Escape: the dialog closes and focus returns to "New workspace".
6. Confirm focus never reaches the page background while the dialog is open.
7. Repeat with a screen reader and at 200% zoom.
