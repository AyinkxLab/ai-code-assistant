"""Redaction-safe structured usage events (issue #479)."""
from __future__ import annotations
import json, logging, time, uuid
from typing import Any

logger = logging.getLogger("usage")
_SECRET = {"token", "secret", "password", "api_key", "authorization", "prompt", "source"}

def emit_usage_event(event: str, *, actor: str | None = None, request_id: str | None = None,
                      outcome: str = "ok", duration_ms: float | None = None, **fields: Any) -> str:
    rid = request_id or str(uuid.uuid4())
    payload = {"event": event, "request_id": rid, "actor": actor, "outcome": outcome,
               "duration_ms": round(duration_ms, 2) if duration_ms is not None else None,
               **{k: v for k, v in fields.items() if k.lower() not in _SECRET}}
    logger.info(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return rid

class UsageTimer:
    def __init__(self, event: str, **fields: Any): self.event, self.fields, self.started = event, fields, time.perf_counter()
    def __enter__(self): return self
    def __exit__(self, exc_type, exc, tb):
        emit_usage_event(self.event, outcome="error" if exc else "ok", duration_ms=(time.perf_counter()-self.started)*1000, **self.fields)
        return False
