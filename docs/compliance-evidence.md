# Compliance evidence pack

`app.services.evidence_pack.build_evidence_pack(workspace_id)` produces a
bounded, machine-readable snapshot of the controls and configuration an
auditor or customer security review usually asks for.

> **It is evidence, not a certification.** The payload carries
> `"certification": false` and an explicit disclaimer. It is not an
> attestation, an audit opinion, or a guarantee that any framework is met.

## What it contains

| Control | Evidence |
| ------- | -------- |
| `audit_export` | Total event count, a bounded export of the newest events, and the hash-chain verification result (`flask audit verify`). |
| `access_review` | Total members, a bounded member list (id, role, status, timestamps), and role counts. |
| `retention` | Effective retention window and legal-hold flag. |
| `security_settings` | Non-secret switches: secure/HTTP-only/same-site session cookies, CSRF, chat audit logging, metrics, security headers. |

The pack also includes its own limits (`audit_events`: 50, `access_rows`: 200)
and a `truncated` flag per collection, so a reader knows when more data exists.

## Privacy

Records are projected through the data classification helper at the `internal`
level. Usernames, emails, audit metadata blobs, tokens, and other
`confidential`/`pii` fields are excluded. The `SECRET_KEY` is never read into
the pack.

## Access control

Application code should pass the acting user:

```python
from app.services.evidence_pack import build_evidence_pack

pack = build_evidence_pack(workspace_id, actor=current_user)
```

A caller without the owner-level `view_audit` capability gets
`PermissionError`. An unknown workspace raises `LookupError`. The operator path
(`actor=None`) relies on deployment/database credentials instead, the same
trust model as the other `flask audit` maintenance commands.

## Known limits

- The pack describes **current** configuration and recent events; it does not
  reconstruct historical state.
- A legal hold is reported as configuration. Enforcement is in
  `flask audit purge` (see [audit retention](audit-retention.md)).
- It does not replace evidence from the hosting platform, identity provider, or
  database backups.
