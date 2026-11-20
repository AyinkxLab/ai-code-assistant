# Distributed tracing and request correlation (#703)

Every request already receives a correlation id from the chat-audit middleware
(`X-Request-ID`: an inbound value is accepted after sanitising, otherwise a new
id is generated, and it is echoed on the response). Tracing reuses that id as
the **trace id**, so request logs, slow-query lines and spans can be joined
without a separate correlation scheme.

The implementation is [`app/observability/tracing.py`](../app/observability/tracing.py).
Spans are recorded only inside a request. There is no external tracing backend
and no new dependency: spans are JSON log records that any log pipeline can
render.

## What is traced

| Span | Where | Attributes |
| --- | --- | --- |
| `llm.provider.chat` | `RetryingProvider.chat` | `provider` |
| `llm.provider.stream` | `RetryingProvider.stream` | `provider` |
| `plugin.handler` | `EventDispatcher.dispatch` before a plugin callback runs | `plugin_id`, `event_type`, `capability` |

A span inherits `parent_span_id` from the span that is open when it starts, so
nested service and plugin work forms a tree. The request's spans share the
`X-Request-ID` value as `trace_id`. The response carries it in `X-Trace-ID` as
well.

## Log format

One line per span on the `app.observability.trace` logger at `INFO`:

```json
{"attributes":{"provider":"mock"},"duration_ms":1.234,"event":"span","name":"llm.provider.chat","parent_span_id":null,"span_id":"9f0c2e1a7b3d4c56","trace_id":"3f1c9a2b7d4e"}
```

Fields are stable:

| Field | Meaning |
| --- | --- |
| `event` | Always `span`. |
| `trace_id` | The request id (`X-Request-ID`). |
| `span_id` | Random 16-hex-character id, unique within the trace. |
| `parent_span_id` | Parent span id, or `null` for a root span. |
| `name` | Bounded operation name (≤ 64 chars). |
| `duration_ms` | Wall-clock duration, rounded to 3 decimals. |
| `attributes` | Safe, bounded key/value strings (see below). |

Configure your logging stack to keep the `app.observability.trace` logger. A
representative line can be rendered as a tree with standard tools, for example:

```bash
# Sort a log file's spans by trace and show name + duration
jq -r 'select(.event=="span") | [.trace_id, .name, .duration_ms] | @tsv' trace.log
```

The process also keeps the **last 100 finished traces** in a bounded in-process
buffer (`recent_traces()`); tests and a local debugger can inspect the same
records without parsing logs. The buffer is not an API and is not persisted.

## Bounds and privacy

- At most **32 spans** per trace. Extra `trace_span` blocks still execute, but
  are not recorded.
- At most **8 attributes** per span; keys are capped at 32 characters and values
  at 64.
- Attribute names matching
  `prompt|content|message|body|query|secret|token|password|authorization|api[_-]?key`
  are dropped entirely.
- Callers pass identifiers, provider/model names, counts and durations only.
  Prompt text, completions, request bodies and credentials are never passed to
  the instrumentation.

The filter is defence in depth, not permission to log content: a new span must
not add content-bearing attributes just because they might be truncated.

## Rendering locally

1. Run the app with `TRACING_ENABLED=1` (the default) and make a request.
2. Read the JSON lines from the `app.observability.trace` logger, or call
   `recent_traces()` from a debug shell with an app context.
3. Group by `trace_id` and order by `parent_span_id` to reconstruct the tree.

`TRACING_ENABLED=0` removes the hooks (no header, no records). Tracing is
request-scoped and per process; CLI commands and background workers are not
traced, and spans are not shipped anywhere by the application.

## What is not covered

- No W3C `traceparent` propagation to third-party HTTP services; provider calls
  are traced locally only.
- No sampling: every traced call inside a request is recorded up to the span
  cap. The cap is the bound.
- No log shipping, storage or UI. Use the JSON format above with the log
  pipeline you already run.
- No spans for SQL statements; query counts and slow statements are covered by
  [`observability.md`](observability.md#query-profiling-and-the-slow-query-log).

## Implementation and tests

- `app/observability/tracing.py`, registered in `app/__init__.py`.
- `tests/test_tracing.py`: two provider calls plus a plugin dispatch share the
  request id; attributes and span counts are bounded; sensitive names are
  dropped; `X-Trace-ID`/`X-Request-ID` match; the log record has a stable shape.
