---
name: Contributor task
about: A scoped, Wave-ready implementation task for contributors
title: ""
labels: ["help wanted"]
assignees: ""
---

> Guidance for maintainers creating Wave-ready issues (see the Drips
> "Creating Meaningful Issues" guide): fill every section, tag complexity
> honestly (Trivial/Medium/High -> points), and reference key files. Keep the
> scope completable within one Wave cycle.

## Problem / real impact

Why this task matters and what it meaningfully improves.

## Context

Background, related issues/PRs, and relevant architecture
(`app/services/...`, `app/...`, `docs/...`).

## Requirements

- Bullet list of concrete requirements.

## Implementation guidance

- Key files/modules to touch.
- Existing patterns to reuse (do not duplicate service logic).
- Edge cases and constraints (security, read-only, fail-closed, isolation).

## Acceptance criteria / definition of done

- What a reviewer checks.
- Validation to run: `pytest`, `ruff check .`, `black --check .` (plus any
  targeted suites).

## Testing requirements

- Which tests to add/update and where they live.

## Complexity

- Trivial (100) / Medium (150) / High (200)
- Difficulty label: `difficulty/easy`, `difficulty/medium`, or
  `difficulty/hard`

## Out of scope

- Explicit non-goals.
