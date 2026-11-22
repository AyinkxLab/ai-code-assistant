# Upgrade guide

This guide describes the schema changes, new settings, and required operator
actions for the current release. It is referenced from
[`CHANGELOG.md`](../CHANGELOG.md) and assumes the standard deployment described
in `docs/migration-rollout.md`.

## Before you upgrade

1. Take a database backup (`python scripts/db_backup.py`) and confirm it can be
   restored in staging (`python scripts/db_restore.py`).
2. Run the migration preflight against a copy of production:
   `python scripts/migration_preflight.py`.
3. Read the new settings below and decide values before restarting workers.

## Schema changes

### Organization / workspace membership

The membership table now carries the full collaboration lifecycle:

| Column | Type | Notes |
| ------ | ---- | ----- |
| `status` | `varchar(20)` | `active` or `removed`; defaults to `active`. |
| `joined_at` | `timestamptz` | When the member joined. |
| `removed_at` | `timestamptz` | Set when a membership is soft-removed. |
| `last_seen_at` | `timestamptz` | Presence heartbeat; `NULL` until the first heartbeat. |

Existing rows are backfilled to `status = 'active'`. Removed members keep their
history but never grant access; listings filter on `status = 'active'`.

No column is dropped or renamed, so a rollback does not lose member history.

### API quotas and OpenAPI

No database schema changes. The OpenAPI document is generated from the Flask
routing table and is not stored in the database.

## New settings

All settings are environment variables with safe defaults; see
[`.env.example`](../.env.example).

| Setting | Default | Purpose |
| ------- | ------- | ------- |
| `API_QUOTA_TOKEN_MAX` | `600` | Requests per token per window on public endpoints. |
| `API_QUOTA_TOKEN_WINDOW` | `60` | Token quota window in seconds. |
| `API_QUOTA_ORG_MAX` | `1200` | Requests per organization per window. |
| `API_QUOTA_ORG_WINDOW` | `60` | Organization quota window in seconds. |

The existing `RATE_LIMIT_*` settings are unchanged. Set a quota value to `0` to
disable that dimension.

## Required operator actions

1. Apply migrations: `flask db upgrade` (or let the deploy job run them).
2. Restart every web and worker process so the new quota decorators and headers
   take effect.
3. If you publish an OpenAPI document, regenerate it with
   `python scripts/generate_openapi.py --output openapi.json` and update your
   API gateway from that artifact.
4. If you run multiple workers, back the quota limiter with a shared store
   before scaling further; the default limiter is process-local.

## Rollback notes for the organization migration

The migration is additive and backward compatible:

1. Revert the application to the previous image. Older code ignores the new
   membership columns.
2. If you must reverse the migration, run `flask db downgrade -1` **after**
   taking a fresh backup. The downgrade drops only the additive columns;
   soft-removed members are not restored to active automatically.
3. Restore the pre-upgrade backup if you need the exact previous membership
   state.
4. Remove the `API_QUOTA_*` environment variables if the older image does not
   understand them.

## Verifying the upgrade

- `python scripts/migration_preflight.py` reports a single migration head.
- `pytest`, `ruff check .`, and `black --check .` pass.
- `python scripts/generate_openapi.py --check` reports no drift.
- A smoke request to `GET /workspaces/api/workspaces` returns
  `X-RateLimit-Limit`, `X-RateLimit-Remaining`, and `X-RateLimit-Reset`.
