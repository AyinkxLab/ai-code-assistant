# AI Code Assistant TypeScript SDK

Typed client for the AI Code Assistant public API. Ships ESM and CommonJS builds
from the same TypeScript source.

## Install

```bash
npm install @ai-code-assistant/sdk
```

For local development in this repository:

```bash
cd sdk/typescript
npm install
npm run build
```

## Quick start

```ts
import { Client, NotFoundError } from "@ai-code-assistant/sdk";

const client = new Client({
  baseUrl: "https://api.example.com",
  apiToken: "token",
  timeoutMs: 15_000,
  maxRetries: 2,
});

try {
  const project = await client.getProject(42);
  console.log(project.name);
} catch (error) {
  if (error instanceof NotFoundError) console.log("No such project");
  throw error;
}
```

## Behavior

- The token is sent as `Authorization: Bearer <token>`.
- `timeoutMs` is per attempt.
- `maxRetries` is bounded. Retries only happen for idempotent requests (`GET`,
  `HEAD`, `OPTIONS`, `PUT`, `DELETE`) and for transient status codes
  (`429`, `500`, `502`, `503`, `504`) or transport failures. `POST` and `PATCH`
  are never retried automatically.
- `Retry-After` is honored (capped by `maxBackoffMs`) and exponential backoff is
  used otherwise.
- Errors are typed and keep the raw `code`, `statusCode`, `requestId` and
  `payload`.

The mapping is shared with the Python SDK through
[`sdk/fixtures/error-mapping.json`](../fixtures/error-mapping.json).

## Webhook verification

```ts
import { verifySignature } from "@ai-code-assistant/sdk";

const valid = await verifySignature({
  secret,
  body: rawBody, // exact bytes, before JSON parsing
  signature: request.headers["x-aica-signature"],
  timestamp: Number(request.headers["x-aica-timestamp"]),
});
if (!valid) throw new Error("invalid webhook signature");
```

The signature covers `<timestamp>.<raw body>`. Verification uses the Web Crypto
API and rejects timestamps outside the 300-second tolerance.

## Tests

The suite lives in `tests/js/sdk_typescript.test.js` at the repository root and
runs on Vitest against a local mock HTTP server:

```bash
npm test
```
