# Notification integrations (Slack, Teams, email, health)

This guide covers the workspace notification integrations that deliver review,
budget, and security events outside the app:

| Issue | Integration | Code |
| --- | --- | --- |
| #678 | Slack incoming webhook | `app/services/integrations/slack.py` |
| #679 | Microsoft Teams incoming webhook | `app/services/integrations/teams.py` |
| #682 | Inbound/outbound email | `app/services/integrations/email.py` |
| #684 | Health checks and test connection | `app/services/integrations/health.py` |

All four share `app/services/integrations/common.py`: bounded secret-free event
summaries, timeouts, bounded exponential-backoff retries, structured error
codes, and sanitized error text. Secrets live in the encrypted
[integration credentials](integration-credentials.md) store; nothing is
stored in plaintext.

## Event types

The connectors render only these event types:

| Event | Source |
| --- | --- |
| `review.completed` | `review.completed` application event |
| `budget.alert` | `app/services/budget_alerts.py` threshold alerts |
| `security.alert` | `stellar.analysis.completed` application event |
| `test` | Manual test-send from the API |

Only known scalar fields are copied into the payload and every string is
length-bounded, so an event can never leak an arbitrary secret into Slack,
Teams, or email.

## Slack (#678)

The incoming-webhook URL is validated with the same SSRF guard as the webhook
job queue (`validate_webhook_url`), then stored encrypted as
`integration=slack, name=webhook_url`. Delivery posts a small `{"text": ...}`
message with a request timeout and bounded retries on network errors, HTTP 429,
and HTTP 5xx.

## Microsoft Teams (#679)

Teams uses an incoming webhook and a bounded MessageCard. The webhook URL is
validated with `validate_webhook_url` and stored encrypted as
`integration=teams, name=webhook_url`. The same retry policy applies.

## Email (#682)

Outbound email is sent through the existing SMTP service. The integration adds:

* a configurable `Reply-To` credential (`integration=email, name=reply_to`);
* bounded plain-text and HTML templates under `app/templates/integrations/`;
* a signed `List-Unsubscribe` link and a persisted suppression list
  (`email_suppressions`).

Inbound email is accepted at `POST /integrations/email/<workspace_id>/inbound`.
The relay must send:

* `X-Timestamp`: Unix seconds;
* `X-Signature`: lowercase hex HMAC-SHA256 of `<timestamp>.<raw body>` using the
  workspace's `integration=email, name=inbound_secret` value.

Timestamps outside `INTEGRATION_INBOUND_SIGNATURE_TOLERANCE` seconds are
rejected. The parser caps body size, attachment count, attachment size, and text
length; attachment names with path separators or executable extensions are
rejected. A valid message creates an in-app notification for the workspace
owner.

Unsubscribe links call `GET /integrations/email/unsubscribe?token=...`. The token
is an HMAC over the workspace and recipient, so it cannot be forged.

## Health checks (#684)

Every delivery records a non-secret snapshot in `integration_health`:

* `last_success_at`, `last_status_code`, `last_checked_at`;
* `last_error_at`, `last_error`, `last_error_code`.

Errors are sanitized before storage (`sanitize_error`): URLs are redacted
because Slack/Teams webhook URLs contain a token, and the text is truncated.
The API never returns credentials.

## API

All configuration and test routes require the `manage_integrations`
capability, which only workspace owners hold. Non-members receive 404 and
members receive 403.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/integrations/api/workspaces/<id>/integrations` | Configuration metadata + health |
| `GET` | `/integrations/api/workspaces/<id>/integrations/health` | Health only |
| `PUT` | `/integrations/api/workspaces/<id>/integrations/<integration>` | Store/rotate credentials |
| `DELETE` | `/integrations/api/workspaces/<id>/integrations/<integration>` | Remove credentials |
| `POST` | `/integrations/api/workspaces/<id>/integrations/<integration>/test` | Manual test connection/send |
| `POST` | `/integrations/email/<id>/inbound` | Signed inbound email |
| `GET`/`POST` | `/integrations/email/unsubscribe` | Signed unsubscribe |

Structured errors always include `code` and `error`: `invalid_input`,
`not_configured`, `permission_denied`, `not_found`, `delivery_failed`,
`invalid_signature`, `expired_signature`, `unsafe_attachment`, and
`payload_too_large`.

## Configuration

```dotenv
INTEGRATION_REQUEST_TIMEOUT=10
INTEGRATION_MAX_RETRIES=3
INTEGRATION_RETRY_BASE_SECONDS=0.5
INTEGRATION_RETRY_MAX_SECONDS=8
INTEGRATION_INBOUND_SIGNATURE_TOLERANCE=300
INTEGRATION_INBOUND_MAX_BYTES=524288
INTEGRATION_EMAIL_MAX_ATTACHMENTS=5
INTEGRATION_EMAIL_MAX_ATTACHMENT_BYTES=10485760
INTEGRATION_EMAIL_MAX_BODY_CHARS=20000
PUBLIC_BASE_URL=https://app.example.com
```

`PUBLIC_BASE_URL` makes unsubscribe links absolute; when unset the link is
relative.

## Tests

* `tests/test_slack_integration.py`
* `tests/test_teams_integration.py`
* `tests/test_email_integration.py`
* `tests/test_integration_health.py`
* `tests/test_integration_migration.py`

All outbound network calls are injected or mocked; inbound verification uses a
fixed timestamp and a local secret.
