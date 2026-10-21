# Structured usage logging

Usage events are emitted as JSON with stable keys (`event`, `request_id`, `actor`, `duration_ms`, and `outcome`). Secrets, prompts, and source contents are excluded. Set `USAGE_LOG_LEVEL=info` to enable production-safe summaries; invalid values fall back to `warn`.
