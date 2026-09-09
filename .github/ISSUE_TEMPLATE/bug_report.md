---
name: Bug report
about: Report a reproducible bug
title: "[bug] "
labels: ["bug"]
assignees: ""
---

## Summary

One or two sentences describing the bug and its real impact.

## Reproduction

Steps to reproduce:

1. ...
2. ...
3. ...

Expected behavior:
Actual behavior:

## Environment

- Deployment: local dev / Docker / production (if relevant)
- OS / browser (if frontend)
- Any configuration values you changed from the defaults (never include secrets)

## Suggested fix / notes (optional)

- Point to the module you suspect (`app/services/...`, `app/static/js/...`).
- Include any edge cases a fix must handle.

## Definition of done

- [ ] Bug no longer reproduces
- [ ] Regression test added (see `CONTRIBUTING.md` for test layout)
- [ ] `pytest`, `ruff check .`, and `black --check .` pass
