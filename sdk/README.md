# SDKs

Typed client libraries for the AI Code Assistant public API, introduced in
Phase 12 (issues #657, #658, #659 and #661).

| SDK | Source | Install |
| --- | ------ | ------- |
| Python | [`python/`](python/README.md) | `pip install -e sdk/python` |
| TypeScript | [`typescript/`](typescript/README.md) | `npm install @ai-code-assistant/sdk` |
| Rust | [`rust/`](rust/README.md) | `cargo add ai-code-assistant-sdk` |

All SDKs:

- authenticate with an API token (`Authorization: Bearer`);
- expose configurable timeouts and bounded retries;
- retry only idempotent requests and transient status codes;
- raise typed errors and preserve the raw `code` and request id;
- validate responses into typed models (projects, reviews, findings, usage);
- verify webhook deliveries with the shared `v1=<hex HMAC-SHA256>` contract.

The shared error contract lives in
[`fixtures/error-mapping.json`](fixtures/error-mapping.json) and is asserted by
both test suites. See [`docs/sdk.md`](../docs/sdk.md) for the full reference.

## Quickstart examples

Both examples are runnable against a bundled local mock API, so they need no
credentials and no network access:

```bash
# Start the mock API and run the Python quickstart
python sdk/python/examples/mock_server.py

# Or point the quickstart at your own API
AICA_BASE_URL=http://127.0.0.1:8000 AICA_API_TOKEN=demo-token \
  python sdk/python/examples/quickstart.py
```

The TypeScript example mirrors it; start the same mock server, then run
[`typescript/examples/quickstart.ts`](typescript/examples/quickstart.ts) with
your preferred TypeScript runner.

```bash
npx tsx sdk/typescript/examples/quickstart.ts
```

The examples show authentication, listing projects with pagination, structured
errors, and webhook delivery logs.

## Tests

```bash
pytest tests/test_sdk_python.py tests/test_sdk_quickstart.py   # Python
npm test                                                       # TypeScript (Vitest)
cd sdk/rust && cargo test                                      # Rust
```

See [`docs/sdk-release.md`](../docs/sdk-release.md) for the tag-driven release
and publish process.
