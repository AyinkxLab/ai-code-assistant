---
name: Feature request
about: Suggest a new capability
title: "[feat] "
labels: ["enhancement"]
assignees: ""
---

## Problem / motivation

What is missing and why does it matter to users or developers?

## Proposed behavior

What "done" looks like from the outside.

## Scope

- In scope (and any explicitly out of scope, e.g. wallet/signing features for
  Stellar, remote plugin installs).
- Estimated complexity: Trivial (100) / Medium (150) / High (200) with a short
  justification, matching the Drips Wave points model.

## Implementation guidance

- Key files/modules likely involved (`app/services/...`, `app/...`).
- Design references or existing patterns to follow.
- Constraints (security, read-only, SSRF, workspace isolation, fail-closed).

## Validation

- Tests to add/update and where they live.
- How a reviewer should verify the change (commands, manual steps).

## Definition of done

- [ ] Feature works as described
- [ ] Tests added and passing (`pytest`)
- [ ] `ruff check .` and `black --check .` pass
- [ ] Documentation updated if behavior is user-visible
