# Webhooks and Event Delivery Architecture

This document describes the webhook payload size limits, oversized payload policies, pre-buffering defenses, and error response conventions for AI Code Assistant (Phase 12, #674).

---

## 1. Overview

AI Code Assistant delivers and ingests real-time events across projects, reviews, workspaces, and integrations. To prevent memory exhaustion attacks, unbounded buffering, and denial-of-service conditions, all webhook payloads are subject to strict byte-level size limits and pre-buffering enforcement.

---

## 2. Configuration Settings

Webhook payload bounds are environment-driven and can be tuned in `.env` or container settings:

| Setting | Default | Description |
|---|---|---|
| `WEBHOOK_MAX_PAYLOAD_BYTES` | `262144` (256 KB) | Maximum allowed size of an event payload in bytes. |
| `WEBHOOK_OVERSIZED_POLICY` | `reject` | Handling policy when a payload exceeds the byte ceiling (`reject` or `truncate`). |
| `WEBHOOK_INGESTION_SECRET` | *(empty)* | Optional shared secret required via `X-Webhook-Secret` or `Authorization: Bearer <secret>`. |

---

## 3. Oversized Payload Policies

### 3.1 Rejection Policy (`reject`, Default)
The rejection policy is the recommended, fail-closed default. When any payload's serialized byte length exceeds `WEBHOOK_MAX_PAYLOAD_BYTES`:
- The request or delivery is immediately rejected.
- The HTTP server responds with status **413 Request Entity Too Large**.
- A structured error response is returned:
  ```json
  {
    "error": "Webhook payload size 350000 bytes exceeds maximum limit of 262144 bytes.",
    "code": "payload_too_large",
    "limit_bytes": 262144
  }
  ```

### 3.2 Safe Truncation Policy (`truncate`)
When the operator explicitly enables `WEBHOOK_OVERSIZED_POLICY=truncate`:
- **Core Envelope Invariance**: Structural and identity fields are **NEVER** silently truncated or dropped:
  - `id`
  - `event`
  - `event_type`
  - `timestamp`
  - `workspace_id`
  - `user_id`
  - `version`
  - `delivery_id`
  - `signature`
- **Safe Truncation Targets**: Voluminous optional fields (such as `data.diff`, `data.raw`, `data.findings`, `data.output`, `data.patch`, `data.content`) are truncated to bring the total serialized size within the configured ceiling.
- **Explicit Truncation Markers**: Truncated payloads explicitly advertise metadata to ensure downstream consumers are aware of truncated fields:
  ```json
  {
    "event_type": "project.updated",
    "id": 42,
    "timestamp": "2026-10-28T12:00:00Z",
    "data": {
      "diff": "diff --git a/... [truncated]"
    },
    "_truncated": true,
    "_original_size": 420000,
    "_truncated_fields": ["data.diff"]
  }
  ```
- **Fail-Closed Floor**: If the immutable required fields alone exceed `WEBHOOK_MAX_PAYLOAD_BYTES`, the system refuses to corrupt the event and fails closed with HTTP 413.

---

## 4. Pre-Buffering Protection

To prevent attackers from sending multi-gigabyte streams that exhaust server memory before validation occurs:
1. **Header Evaluation**: If the incoming HTTP request specifies a `Content-Length` header exceeding the permitted ceiling, the request is aborted immediately before reading bytes from the socket stream.
2. **Chunked Stream Bounds**: If `Content-Length` is omitted or chunked transfer encoding is used, the stream is read incrementally in 8 KB chunks. As soon as cumulative bytes exceed the maximum threshold, stream reading is halted and an HTTP 413 error is returned.

---

## 5. Structured Error Responses

All error responses adhere to the repository's API error convention:

| Status Code | Error Code | Condition |
|---|---|---|
| `400 Bad Request` | `malformed_payload` | The request body is not valid JSON or is not a JSON object. |
| `400 Bad Request` | `empty_payload` | The request body is empty. |
| `401 Unauthorized` | `unauthorized` | Missing or invalid webhook secret or signature. |
| `403 Forbidden` | `forbidden` | Subscriber lacks capability or grant to receive the specified event. |
| `413 Payload Too Large` | `payload_too_large` | Payload exceeds `WEBHOOK_MAX_PAYLOAD_BYTES` under the `reject` policy. |
| `422 Unprocessable` | `invalid_payload` | Payload is valid JSON but lacks required fields (e.g. `event_type`). |

---

## 6. Privacy and Sanitization

Webhook logs and audit trails strictly sanitize all request contents:
- Raw request bodies and full payloads are never written to disk or logs.
- Webhook secrets, bearer tokens, and HMAC signatures are scrubbed.
- Logs capture only safe metadata: `event_type`, byte count, delivery outcome, and truncation status.
