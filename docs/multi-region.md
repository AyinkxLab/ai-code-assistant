# Multi-region deployment guide

This guide describes what the current codebase can and cannot do across regions.
It is intentionally conservative: every "supported" statement below was checked
against the application code, not assumed from typical Flask deployments.

## Summary

| Question | Answer today |
| --- | --- |
| Multi-region **writes** (active/active) | **Not supported.** There is one SQLAlchemy engine and one writer. |
| Read replicas used by the application | **Not supported.** No replica URL, routing, or read/write split exists. |
| Per-tenant / per-user data residency | **Not supported.** All tenants share one primary database. |
| Multiple app replicas in one region | **Supported.** The app is stateless apart from a few in-process caches. |
| App replicas in several regions, all writing to one primary | **Possible with caveats.** It works technically, but adds latency and is not a residency or failover solution. |
| Automatic regional failover | **Not implemented.** Promotion and `DATABASE_URL` changes are operator steps. |
| Background job workers in several regions | **Supported only against the same primary database** (row locks). |
| Cross-region real-time collaboration | **Not supported** without external Socket.IO message passing; rooms/presence are process-local. |
| Global rate limits | **Not supported.** Limits and caches are per process. |

If a requirement says "data must stay in region X" or "users in two regions can
write at the same time", the honest answer is: this application does not support
that today. Run a separate, complete installation per region instead (see
[Data residency](#data-residency)).

## What the code actually does

### One database engine

`app/config.py` reads a single `DATABASE_URL`. `app/extensions.py` creates one
Flask-SQLAlchemy extension, and every model, query, migration, and CLI command
uses it. There is no `DATABASE_REPLICA_URL`, no `SQLALCHEMY_BINDS`, no query
routing, and no read-after-write handling. A read replica can exist, but the app
will not use it.

Production startup refuses to run unless `DATABASE_URL` starts with
`postgresql://` (see `ProductionConfig`). SQLite is a development/test default
only.

### Client-side sessions

Sessions are Flask's signed cookies; there is no server-side session store, no
Redis, and no session table. Consequences:

- Any app replica that shares the same `SECRET_KEY` can serve the next request.
  Sticky sessions are not required for HTTP requests.
- `SECRET_KEY` is also used to encrypt stored provider API keys
  (`app/services/crypto.py`). Every region must use the **same** secret, and
  rotating it invalidates stored keys.
- A cookie issued in one region is not valid in a separately deployed stack that
  uses a different `SECRET_KEY`.
- `ProductionConfig` forces `SESSION_COOKIE_SECURE`, `SESSION_COOKIE_HTTPONLY`,
  and `SameSite=Lax`.

### Database-backed background jobs

`app/services/job_queue.py` stores jobs in the `jobs` table. Workers claim a job
with `SELECT ... FOR UPDATE SKIP LOCKED` against the primary database. Multiple
workers — including workers in other regions — can share that queue safely, but
they must all connect to the same primary. A worker pointed at a read replica
cannot claim work.

### In-process state

These are not shared between processes or regions:

- LLM response cache, read cache, GitHub ETag cache, marketplace cache
- Rate-limit counters
- Alert dedupe state
- Socket.IO rooms and collaboration presence
- Usage/price caches loaded per process

Running `N` replicas therefore gives roughly `N` times the nominal rate limit and
`N` independent caches. That is usually acceptable within one region; across
regions it becomes a correctness problem for global quotas.

### Health checks

`GET /health` returns a fixed `{"status": "ok", ...}` and does **not** query the
database. A region can report healthy while its database connection is broken.
For failover decisions, use an external check that opens a database connection
and runs `SELECT 1`, not `/health` alone.

## Data residency

There is exactly one authoritative database. PostgreSQL streaming replicas copy
that database to other regions, so **every byte is present in every replica
region** (unless the provider offers tablespace/row filtering, which this schema
does not use).

Supported residency options:

1. **Single region, multi-AZ** — all data stays in one region; availability
   comes from database Multi-AZ and multiple app replicas.
2. **Separate stack per region** — deploy the app, database, workers, and
   secrets independently in each region when regulation requires it. Users,
   workspaces, and conversations are **not** shared across those stacks; there
   is no cross-region login, sync, or migration tool.

Not supported:

- Routing one user's rows to one region while other users stay elsewhere.
- Keeping EU rows out of a US primary while serving both from one installation.
- A "region" column or tenant-to-region mapping. No such field exists.

If you need regional residency, budget for a separate product instance per
region, including separate backups, billing data, OAuth apps, and support
processes.

## Read replicas

PostgreSQL read replicas are useful for:

- external BI / analytics tools that connect directly;
- a warm standby for disaster recovery;
- backups taken from a replica to reduce primary load.

They are **not** usable by the application:

- All ORM reads go to `DATABASE_URL`.
- After a write, the next request may be served by any app replica; that replica
  still reads the primary, so read-after-write is consistent.
- No code detects replica lag or retries a stale read elsewhere.

Do not put a replica URL behind a load balancer in front of `DATABASE_URL`: the
app also writes through that URL. Any write sent to a read-only replica fails.

## Failover expectations

Failover is operator-driven. Reasonable expectations for this codebase:

| Event | Expected behaviour |
| --- | --- |
| App replica dies | Load balancer removes it; no session loss (cookie sessions). |
| Primary database fails over to a standby (same endpoint) | Existing connections break; SQLAlchemy reconnects on new requests. In-flight requests fail; clients retry. |
| Primary promoted at a **new** endpoint | Update `DATABASE_URL` and restart/redeploy app and worker processes. No runtime discovery. |
| Region loses network to the primary | The app cannot serve writes or most reads; `/health` still returns 200. Remove the region from the load balancer. |
| Replica promotion | Any writes not yet replicated are lost. Measure and document the RPO from replication lag. |
| Split brain (two primaries) | **Unsupported and unsafe.** There is no conflict resolution; do not run two writers. |

Recommended sequence for regional failover:

1. Declare the incident and stop writes in the failed region (remove it from the
   load balancer, stop workers).
2. Confirm the standby's replication position and promote it.
3. Update the secret/config store with the new `DATABASE_URL`.
4. Deploy or restart app replicas and exactly one worker fleet against the new
   primary.
5. Run a database-level smoke test (login, one read, one write) and a
   preflight/backup check before restoring traffic.
6. Rebuild replication in the old region as a standby; never let it accept
   writes before it has caught up.

RPO is bounded by replication lag; RTO by your promotion and redeploy automation.
Neither is provided by the application.

## Decision table

| Requirement | Recommended topology | Supported? | Main trade-off |
| --- | --- | --- | --- |
| High availability in one country | One region, Multi-AZ DB, 2+ app replicas | Yes | Simplest; one latency domain |
| Read scaling for dashboards | Add a replica for external tools only | Partially — app cannot use it | Extra infrastructure, no app benefit |
| Lower latency for a distant user base | App replicas near users, all against one primary | Yes, with caveats | Cross-region DB latency; per-process caches/limits |
| Disaster recovery | Cross-region streaming standby, manual promotion | Yes, manual | RPO from lag; operator runbook required |
| Data must remain in a specific country | Separate full stack per country | Yes, as separate installs | No shared users/data; duplicated cost |
| Active/active writes in two regions | Multi-master or sharded database | **No** | Would require application redesign |
| Global per-user rate limits | Central store or single-region gateway | **No** in current code | In-memory limits multiply per replica |
| Cross-region live presence | External Socket.IO broker / Redis adapter | **No** in current dependencies | Rooms are process-local |

## Configuration example

Production rejects missing secrets and non-PostgreSQL URLs. A multi-region app
fleet should set at least:

```dotenv
APP_ENV=production
# Same value in every region. Also encrypts stored provider keys.
SECRET_KEY=<64-hex-character-random-value>
# One writer endpoint. Use the provider's stable writer DNS name if it supports
# endpoint switching; otherwise change this during a failover.
DATABASE_URL=postgresql://aica:<password>@primary.db.example.net:5432/aica
# Optional: restrict real-time WebSocket origins. Same-origin deployments can
# leave this empty.
SOCKETIO_CORS_ALLOWED_ORIGINS=https://app.example.com
```

Notes:

- Do not point `DATABASE_URL` at a read replica.
- Do not use a different `SECRET_KEY` per region: users would be logged out when
  routed elsewhere, and stored provider keys would fail to decrypt.
- `SESSION_LIFETIME` is optional and defaults to 12 hours.
- `docker-compose.yml` runs `flask db upgrade` before gunicorn. With multiple
  regions, run migrations **once** from a controlled job (see
  `docs/migration-rollout.md`); concurrent upgrades are not safe.

The configuration above is exercised by `tests/test_multi_region_docs.py`, which
loads it through the real production config class in a subprocess.

## Cost and complexity checklist

Relative sizing, not a price quote. Costs depend on provider and traffic.

| Item | Cost driver | Complexity |
| --- | --- | --- |
| Second app region | Compute, load balancer, egress, observability agents | Medium |
| Cross-region database traffic | Every query and write crosses regions; latency and egress | High |
| Read replica | Storage, compute, replication bandwidth | Low–medium |
| Cross-region standby | Full duplicate storage + replication traffic | Medium |
| Failover automation (DNS, config, health checks) | Engineering time, runbooks, game days | High |
| Separate residency stacks | Duplicate everything per region, plus support | High |
| External Socket.IO broker (if needed) | Managed cache/broker, new dependency | High (not implemented) |
| Central rate limiting (if needed) | Shared store, new dependency | High (not implemented) |
| Backups per region | Storage, transfer, retention, encryption keys | Medium |
| Egress for LLM/GitHub/Stellar calls | Traffic leaves from each region | Medium |
| On-call training and failover drills | Staff time | Ongoing |

Questions to answer before committing:

- What RPO/RTO is acceptable, and does replication lag meet it?
- Is data residency a legal requirement or a latency goal? The answers differ.
- Can the product tolerate users being pinned to one region during an incident?
- Who is allowed to promote the database, and how is that audited?
- Are backups stored in the same region as the primary? (Avoid a single failure
  domain; see `docs/backup-restore.md` when the backup runbook lands.)
- Will per-process rate limits and caches be acceptable across regions?

## Verification checklist

- [ ] All regions use the same `SECRET_KEY` from a secret manager.
- [ ] `DATABASE_URL` points at the writer endpoint in every region.
- [ ] Only one worker fleet writes jobs at a time during a failover.
- [ ] `/health` is supplemented with a database `SELECT 1` probe.
- [ ] Replication lag and failover time are measured, not assumed.
- [ ] Backups are taken from the primary or a consistent standby and stored off-region.
- [ ] Migrations are run once, before app rollout.
- [ ] Rate-limit and cache multiplication is accepted or a gateway is used.
- [ ] Real-time collaboration is either single-region or documented as degraded.

## What was not tested

This guide is based on code inspection. No multi-region environment, managed
PostgreSQL failover, or cross-region latency test was executed for this change.
Provider-specific behaviour (DNS switching, promotion time, replica lag) must be
validated in your own staging environment.
