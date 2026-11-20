# SDKs

Typed client libraries for the AI Code Assistant public API, introduced in
Phase 12 (issues #657, #658, #659 and #661).

| SDK | Source | Install |
| --- | ------ | ------- |
| Python | [`python/`](python/README.md) | `pip install -e sdk/python` |
| TypeScript | [`typescript/`](typescript/README.md) | `npm install @ai-code-assistant/sdk` |

Both SDKs:

- authenticate with an API token (`Authorization: Bearer`);
- expose configurable timeouts and bounded retries;
- retry only idempotent requests and transient status codes;
- raise typed errors and preserve the raw `code` and request id;
- validate responses into typed models (projects, reviews, findings, usage).

The shared error contract lives in
[`fixtures/error-mapping.json`](fixtures/error-mapping.json) and is asserted by
both test suites. See [`docs/sdk.md`](../docs/sdk.md) for the full reference.

## Tests

```bash
pytest tests/test_sdk_python.py   # Python
npm test                          # TypeScript (Vitest)
```
