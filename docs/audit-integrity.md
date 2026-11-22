# Audit log integrity

The audit log (`audit_logs`) is append-only from the application's point of
view: routes add entries through `app.services.audit.record` and nothing in the
app updates or deletes them. Phase 11 adds a hash chain so a row changed
directly in the database can be detected.

## How the chain works

Every entry stores:

| Column | Meaning |
| ------ | ------- |
| `workspace_id` | The workspace ("organization") whose chain the row belongs to. Global events use the `NULL` chain. |
| `prev_hash` | The `record_hash` of the previous row in the same chain, or 64 zeroes for the first hashed row. |
| `record_hash` | SHA-256 of the row's canonical fields plus `prev_hash`. |

Hashed fields: `workspace_id`, `action`, `target_type`, `target_id`, `metadata`,
`created_at` and `prev_hash`. `user_id` is deliberately excluded: deleting a
user sets it to `NULL`, and hashing it would make every historical row look
tampered after a legitimate account deletion. Consequently, changing only
`user_id` is not detected by the chain; actor changes are visible in backups
and access logs instead.

Timestamps are normalized to UTC before hashing, so SQLite's naive datetimes
and PostgreSQL's timezone-aware datetimes verify identically.

## Verifying

```bash
flask audit verify                 # every chain, including the NULL chain
flask audit verify --workspace-id 7 --json
```

Exit status is `0` when all chains are intact, `1` when a modified or missing
row is found, and `2` for invalid input (for example an unknown workspace).
The JSON form is:

```json
{
  "ok": false,
  "chains": [
    {
      "workspace_id": 7,
      "ok": false,
      "checked": 12,
      "unhashed": 0,
      "errors": [{"id": 42, "problem": "missing_or_reordered"}]
    }
  ]
}
```

`problem` is `modified` when the recomputed row hash differs from the stored
hash and `missing_or_reordered` when a row does not link to its predecessor.

## Known limits

- **Legacy rows.** Entries written before this migration have `NULL` hashes.
  They are counted as `unhashed` and skipped, never reported as tampered.
- **Purged prefixes.** Retention (#608) removes the oldest rows. The first
  surviving hashed row is therefore treated as an anchor even though its
  `prev_hash` points at a deleted row. Deleting a middle row is still detected.
- **Tail truncation.** Deleting the newest rows leaves a valid-looking prefix.
  The chain alone cannot prove a chain was not truncated; keep database
  backups or publish the latest `record_hash` to an external system if that
  matters for your threat model.
- **Concurrent writers.** Appends take a row lock on PostgreSQL. SQLite ignores
  `FOR UPDATE`, so a shared SQLite writer could race; deployment uses
  PostgreSQL.
