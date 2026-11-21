# Notification channels

Issue #631 lets a workspace owner choose which channels deliver each event type.

## Model

`NotificationChannelPreference` stores one row per `(workspace, event_type)`:

- `email` — best-effort SMTP delivery.
- `chat` — an in-app notification (`Notification`).
- `webhook` — an outbound delivery queued as a `webhook.deliver` job.

Rows are sparse: an event type without a row uses the defaults (`chat` on,
`email` and `webhook` off).

## UI and API

- UI: `/workspaces/<id>/notifications` (requires the `manage_settings`
  capability, i.e. a workspace owner).
- `GET /workspaces/api/workspaces/<id>/notification-channels`
- `PUT /workspaces/api/workspaces/<id>/notification-channels`
  with `{"rows": [{"event_type": "...", "email": false, "chat": true, "webhook": false}]}`
- `POST /workspaces/api/workspaces/<id>/notification-channels/test`
  with `{"event_type": "...", "channel": "chat"}` (a webhook test also accepts
  `webhook_url`).

Invalid event types, channels, non-boolean values, or unknown keys return a
`400` with `{"error": ..., "code": "invalid_channel_config"}`.

## Testing sends

- **Chat** creates a real in-app notification for the current user.
- **Email** sends through the existing SMTP service, or reports `skipped` when
  email is not configured.
- **Webhook** validates the URL with the job queue's SSRF guard and enqueues a
  delivery, or reports `skipped` when no URL is supplied or the queue is
  disabled.

## Security

- Preferences are workspace-scoped and gated by the existing capability map.
- A non-member receives `404` (no existence oracle); a member without
  `manage_settings` receives `403`.
- Webhook URLs are validated before enqueueing; secrets are never stored in the
  preferences table.
