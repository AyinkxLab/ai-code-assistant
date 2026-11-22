# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Organization query performance budgets for the workspace dashboard and the
  membership listing, with a seeded 50-member organization (#638).
- Per-token and per-organization API quotas on public workspace endpoints,
  including `X-RateLimit-*` headers and a stable `rate_limited` 429 body
  (#654).
- OpenAPI 3.1 document generation (`scripts/generate_openapi.py`) with a CI
  validation check and a published `openapi.json` artifact (#652).
- An upgrade guide covering schema changes, new settings, and operator actions
  (#644).

### Changed

- The workspace membership model now records `status`, `joined_at`,
  `removed_at`, and `last_seen_at`, so removed members are retained as history
  without granting access.
- Public workspace endpoints apply quotas before returning data.

### Documentation

- Added [`docs/upgrade-guide.md`](docs/upgrade-guide.md) and this changelog.

## [0.1.0]

Initial development line: workspaces, projects, collaboration, plugin system,
billing and usage ledgers, observability, and the public API error catalog.
