# Webhooks

Webhook subscriptions let a workspace receive signed notifications for analysis,
review, and deployment events. This page documents the subscription model and
the delivery signature contract.

## Subscription model

`WebhookSubscription` (`app/models/webhook.py`) stores:

| Field | Meaning |
| ----- | ------- |
| `workspace_id` | Workspace the subscription belongs to. |
| `url` | Absolute HTTPS endpoint; embedded credentials are rejected. |
| `event_type` | Legacy single event name (`*` matches every event). |
| `secret` | Signing secret used by the delivery worker. |
| `secret_hash` | SHA-256 of the signing secret for rotation/audit checks. |
| `is_active` | Whether deliveries are currently sent. |

Event filters live in `webhook_subscription_events` (one row per event type) and
are indexed by event type so a worker can find active subscriptions for one
event without parsing a string. When no explicit filters exist, the legacy
`event_type` column is used. A weak-network target is additionally checked at
delivery time by `app.services.job_queue.validate_webhook_url`, which rejects
private, loopback, link-local, and otherwise non-public addresses.

The extension migration is
`migrations/versions/f3a4b5c6d7e8_add_webhook_subscriptions.py`; it also merges
the two pre-existing migration heads.

## Signing

Each delivery is signed over the exact bytes sent:

    <timestamp>.<raw body bytes>

- Algorithm: HMAC-SHA256 with the per-subscription secret.
- Signature header: `X-AICA-Signature: v1=<lowercase hex>`.
- Timestamp header: `X-AICA-Timestamp: <unix seconds>`.
- Replay protection: reject timestamps outside the tolerance window (300
  seconds by default).

`app/services/webhook_signing.py` implements signing and constant-time
verification on the server. The Python, TypeScript, and Rust SDKs expose matching
helpers; see [SDKs](sdk.md#webhook-signatures).

> Verify the raw request body before parsing or re-encoding JSON. Any
> transformation changes the bytes and invalidates the signature.
