# Data classification

Stored records are classified so exports and logs can apply the right audience
without re-deciding field by field. The mapping lives in
`app/services/classification.py`.

| Level | Meaning | Examples |
| ----- | ------- | -------- |
| `public` | Safe for an unauthenticated/public audience | row ids, action names, target type |
| `internal` | Authenticated workspace members | names, roles, timestamps, status |
| `confidential` | Workspace content | message bodies, file contents, metadata blobs, config |
| `pii` | Personal data and secret material | email, username, tokens, encrypted key material |

Unknown field names default to `internal`. A public export therefore never
includes a field just because nobody remembered to classify it.

## Exports

Use the helpers rather than an ad-hoc allowlist:

```python
from app.services.classification import export_record, public_export

public_row = public_export(audit_entry.to_dict())
internal_row = export_record(audit_entry.to_dict(), max_level="internal")
```

`public_export` keeps `public` fields only. `export_record` keeps every field at
or below the requested maximum. These are deny-by-default projections: a field
classified `confidential` or `pii` cannot appear in a lower-level export.

The conversation Markdown export accepts the same maximum:

```python
render_conversation_markdown(conversation)                       # owner download
render_conversation_markdown(conversation, max_level="public")   # redacts title/body
```

The admin action audit JSON export (`/admin/actions?format=json`) is projected
at `internal`, and is already whitelisted to non-metadata columns.

## Redaction helper

`app.services.classification.redact` (also exported as `redact_for_log`) is the
single redaction helper for logs and forwarding exports (#613). It walks nested
dictionaries and lists at any depth and applies two rules:

1. A value under a documented PII field is replaced with `[redacted:pii]`. The
   list is the frozen set `classification.REDACT_FIELDS`, derived from every
   `FIELD_LEVELS` entry classified `pii` (email, username, password, token,
   access/refresh token, api_key, secret, …). Add a key to `FIELD_LEVELS` with
   level `pii` to redact it everywhere.
2. Email addresses embedded in otherwise allowed strings are replaced with
   `[redacted:email]` (`redact_text` applies just this rule to one string).

`redact` and `redact_for_log` both default to the `internal` ceiling, so
confidential content and PII are hidden together. The admin audit export and
SIEM forwarding both pass their rows through this helper.

## Logs

`app.services.chat_audit.log_event` runs every structured field through
`redact_for_log`. Fields classified `confidential` or `pii` are replaced with
`[redacted:<level>]` before the record reaches a handler, including fields
nested inside dictionaries and lists. Callers should still avoid passing message
content or secrets; this is a backstop, not a licence to log them.

## Known limits

- Classification is by field **name**. A confidential value stored under an
  unclassified key is treated as `internal`; add the key to `FIELD_LEVELS`.
- The owner's full project/conversation export is intentionally `confidential`
  and is access-controlled by ownership, not by redaction.
