"""In-process cancellation registry (issue #101).

A client that starts a long project chat stream or analysis can ask to stop it
by ``request_id``. The route calls :func:`is_cancelled` between chunks (stream)
or before side effects (analysis) and aborts cleanly, so a cancelled request
never persists a partial assistant message.

This is deliberately a small, bounded, thread-safe set: the process keeps at
most ``MAX_TRACKED`` cancelled ids (oldest evicted first) and each route calls
:func:`finish` to drop its id when the request ends, so it cannot grow without
bound.
"""

from __future__ import annotations

import threading
from collections import OrderedDict

#: Upper bound on remembered cancelled ids (LRU-evicted).
MAX_TRACKED = 1024


class CancellationRegistry:
    """Thread-safe set of cancelled request ids."""

    def __init__(self, max_tracked: int = MAX_TRACKED) -> None:
        self._cancelled: OrderedDict[str, None] = OrderedDict()
        self._lock = threading.Lock()
        self._max = max_tracked

    def cancel(self, request_id: str | None) -> None:
        if not request_id:
            return
        with self._lock:
            self._cancelled[request_id] = None
            self._cancelled.move_to_end(request_id)
            while len(self._cancelled) > self._max:
                self._cancelled.popitem(last=False)

    def is_cancelled(self, request_id: str | None) -> bool:
        if not request_id:
            return False
        with self._lock:
            return request_id in self._cancelled

    def finish(self, request_id: str | None) -> None:
        if not request_id:
            return
        with self._lock:
            self._cancelled.pop(request_id, None)

    def reset(self) -> None:
        with self._lock:
            self._cancelled.clear()


_registry = CancellationRegistry()


def cancel(request_id: str | None) -> None:
    """Mark ``request_id`` as cancelled."""
    _registry.cancel(request_id)


def is_cancelled(request_id: str | None) -> bool:
    """Return ``True`` when ``request_id`` has been cancelled."""
    return _registry.is_cancelled(request_id)


def finish(request_id: str | None) -> None:
    """Forget ``request_id`` once its request has ended."""
    _registry.finish(request_id)


def reset() -> None:
    """Clear all tracked ids (used by tests)."""
    _registry.reset()
