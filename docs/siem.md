# SIEM webhook and log forwarding

Each workspace can forward structured security events to one SIEM endpoint.
Forwarding reuses the webhook signing contract and bounded retry policy.

## Configuration

All routes require login and are owner-scoped; a non-owner receives a structured
`404 not_found`.

| Method | Path | Purpose |
| ------ | ---- | ------- |
| `GET` | `/webhooks/api/workspaces/<workspace_id>/siem` | Read the endpoint |
| `PUT` | `/webhooks/api/workspaces/<workspace_id>/siem` | Create or update |
| `DELETE` | `/webhooks/api/workspaces/<workspace_id>/siem` | Remove it |

`PUT` body:

```json
{"url": "https://siem.example/hook", "secret": "optional-signing-secret", "is_active": true}
```

- The URL must pass the job queue's SSRF guard (public HTTP(S), no credentials).
- A signing secret is generated when omitted. It is returned in the `PUT`
  response **once** and never again; `GET` reports only `has_secret`.
- Invalid configuration returns `400 invalid_input`.

## Event payload

`app/services/siem.py` forwards a bounded JSON object:

```json
{
  "schema_version": 1,
  "kind": "security-event",
  "event_type": "security.login_failure",
  "workspace_id": 7,
  "occurred_at": "2026-01-01T12:00:00+00:00",
  "actor_id": 3,
  "target_type": "user",
  "target_id": 3,
  "details": {"action": "login_failure", "count": 2}
}
```

`event_type` must match `security.<name>`. Free-form `details` are projected
through `app/services/classification.py` at the `internal` level: confidential
or PII keys (for example `secret`, `prompt`, `token`, `email`) are replaced with
a redaction marker, and nested objects are replaced with a marker rather than
forwarded. The payload size cap from `app/services/webhook_payloads.py` applies.

## Signing and retries

- Signature: HMAC-SHA256 over `<unix timestamp>.<raw body>` with the per-org
  secret, sent as `X-AICA-Signature: v1=<hex>` plus `X-AICA-Timestamp`.
- Retries: bounded exponential backoff via `app/services/webhook_retry.py`
  (5 attempts, capped delay, total delay cap). Redirects are not followed.
- The result reports `delivered`, `response_code`, a short `error` label, and
  per-attempt status. The response body, payload, and secret are never included.
- An unconfigured or inactive endpoint performs no network I/O and returns
  `reason: "not_configured"` or `"inactive"`.

Tests use injected HTTP callables; no live endpoint is contacted.
