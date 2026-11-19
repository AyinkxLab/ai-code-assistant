"""Bounded execution for plugin callbacks (issues #408, #689).

A plugin handler or lifecycle hook is third-party code: it may block on I/O,
loop forever, allocate without bound, or return an enormous result. This module
runs such callbacks in a short-lived daemon thread and applies the resource
budgets configured in :mod:`app.services.plugin_limits`:

* wall-clock time (``PLUGIN_EXECUTION_TIMEOUT``) — enforced by abandoning the
  worker thread;
* CPU time (``PLUGIN_CPU_TIME_SECONDS``) — measured with
  :func:`time.thread_time` and rejected after the callback returns;
* peak memory (``PLUGIN_MAX_MEMORY_BYTES``) — measured with :mod:`tracemalloc`;
* open file-descriptor growth (``PLUGIN_MAX_FILE_DESCRIPTORS``) — measured on
  POSIX; unsupported platforms return ``None`` and skip this check;
* serialized output size (``PLUGIN_MAX_OUTPUT_BYTES``).

The CPU, memory, and descriptor bounds are *post-hoc* guards: Python cannot
preempt a running thread, so a callback that exceeds them is failed cleanly
after it returns rather than being killed mid-flight. The wall-clock bound is
the only preemptive guard. Callbacks must therefore be idempotent and must not
be relied on to have stopped.

Limitations (Python cannot kill threads):

* A callback that exceeds the wall-clock budget is **abandoned**, not
  interrupted. It is a daemon thread, so it never blocks interpreter shutdown,
  but it may still be running (and finish) in the background.
* The current Flask application context is copied into the worker thread when
  one is active, so plugins can still use ``current_app``. The calling thread
  blocks until the callback finishes or the budget expires.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import tracemalloc
from collections.abc import Callable
from typing import Any

from app.services.plugin_limits import (
    DEFAULT_CPU_TIME_SECONDS,
    DEFAULT_EXECUTION_TIMEOUT,
    DEFAULT_MAX_FILE_DESCRIPTORS,
    DEFAULT_MAX_MEMORY_BYTES,
    DEFAULT_MAX_OUTPUT_BYTES,
    get_plugin_limits,
)

logger = logging.getLogger(__name__)

#: Fallback budget used when the application configuration is unavailable.
DEFAULT_TIMEOUT_SECONDS = DEFAULT_EXECUTION_TIMEOUT

#: Upper bound applied to the operation label stored in error reports.
_MAX_OPERATION_CHARS = 160


class PluginTimeoutError(TimeoutError):
    """Raised when a plugin callback exceeds its execution budget."""

    def __init__(self, operation: str, timeout: float) -> None:
        self.operation = str(operation)[:_MAX_OPERATION_CHARS]
        self.timeout = timeout
        super().__init__(f"Plugin operation '{self.operation}' timed out after {timeout:g}s")


class PluginResourceLimitError(RuntimeError):
    """Base class for a callback that exceeded a resource budget."""

    code = "limit_exceeded"

    def __init__(self, operation: str, message: str) -> None:
        self.operation = str(operation)[:_MAX_OPERATION_CHARS]
        super().__init__(message)


class PluginCPUTimeLimitError(PluginResourceLimitError):
    """Raised when a callback used more CPU time than allowed."""

    def __init__(self, operation: str, cpu_seconds: float, limit: float) -> None:
        self.cpu_seconds = cpu_seconds
        self.limit = limit
        label = str(operation)[:_MAX_OPERATION_CHARS]
        super().__init__(
            operation,
            f"Plugin operation '{label}' used {cpu_seconds:.3f}s CPU " f"(limit {limit:g}s)",
        )


class PluginMemoryLimitError(PluginResourceLimitError):
    """Raised when a callback allocated more peak memory than allowed."""

    def __init__(self, operation: str, peak_bytes: int, limit: int) -> None:
        self.peak_bytes = peak_bytes
        self.limit = limit
        label = str(operation)[:_MAX_OPERATION_CHARS]
        super().__init__(
            operation,
            f"Plugin operation '{label}' used {peak_bytes} bytes of memory " f"(limit {limit})",
        )


class PluginFileDescriptorLimitError(PluginResourceLimitError):
    """Raised when a callback leaked more file descriptors than allowed."""

    def __init__(self, operation: str, growth: int, limit: int) -> None:
        self.growth = growth
        self.limit = limit
        label = str(operation)[:_MAX_OPERATION_CHARS]
        super().__init__(
            operation,
            f"Plugin operation '{label}' opened {growth} file descriptors " f"(limit {limit})",
        )


class PluginOutputLimitError(PluginResourceLimitError):
    """Raised when a callback returned a result larger than allowed."""

    def __init__(self, operation: str, size_bytes: int, limit: int) -> None:
        self.size_bytes = size_bytes
        self.limit = limit
        label = str(operation)[:_MAX_OPERATION_CHARS]
        super().__init__(
            operation,
            f"Plugin operation '{label}' produced {size_bytes} bytes of output " f"(limit {limit})",
        )


def configured_timeout() -> float:
    """Return the configured callback budget in seconds (``<= 0`` disables it)."""
    return get_plugin_limits().execution_timeout


def _preserve_app_context(func: Callable[[], Any]) -> Callable[[], Any]:
    """Return ``func`` wrapped so an active Flask app context is available."""
    try:
        from flask import copy_current_app_context, has_app_context

        if not has_app_context():
            return func
        return copy_current_app_context(func)
    except Exception:  # pragma: no cover - context copying is best effort
        return func


def _open_fd_count() -> int | None:
    """Return the process's open descriptor count, or ``None`` when unsupported."""
    for path in ("/proc/self/fd", "/dev/fd"):
        try:
            return len(os.listdir(path))
        except OSError:
            continue
    return None


def _output_size(value: Any) -> int:
    """Return a stable byte size for a callback result (JSON, else ``repr``)."""
    try:
        payload = json.dumps(value, default=repr, separators=(",", ":"))
    except (TypeError, ValueError, OverflowError):
        payload = repr(value)
    return len(payload.encode("utf-8", errors="replace"))


def _run_measured(
    call: Callable[[], Any],
    *,
    operation: str,
    cpu_limit: float,
    memory_limit: int,
    fd_limit: int,
) -> Any:
    """Run ``call`` and raise a resource error when a post-hoc bound is exceeded."""
    fd_before = _open_fd_count()
    cpu_start = time.thread_time()
    was_tracing = tracemalloc.is_tracing()
    if not was_tracing:
        tracemalloc.start()
    try:
        result = call()
        peak_bytes = tracemalloc.get_traced_memory()[1]
    finally:
        if not was_tracing:
            tracemalloc.stop()

    cpu_used = time.thread_time() - cpu_start
    fd_after = _open_fd_count()

    if cpu_limit > 0 and cpu_used > cpu_limit:
        raise PluginCPUTimeLimitError(operation, cpu_used, cpu_limit)
    if memory_limit > 0 and peak_bytes > memory_limit:
        raise PluginMemoryLimitError(operation, peak_bytes, memory_limit)
    if (
        fd_limit > 0
        and fd_before is not None
        and fd_after is not None
        and fd_after - fd_before > fd_limit
    ):
        raise PluginFileDescriptorLimitError(operation, fd_after - fd_before, fd_limit)
    return result


def run_with_timeout(
    func: Callable[[], Any],
    *,
    operation: str,
    timeout: float | None = None,
    cpu_time: float | None = None,
    max_memory_bytes: int | None = None,
    max_output_bytes: int | None = None,
    max_file_descriptors: int | None = None,
) -> Any:
    """Call ``func`` under the configured resource budgets.

    Args:
        func: Zero-argument callable to execute.
        operation: Short label (for example ``dispatch:project.created``) used
            in the timeout/resource error and the recorded error report.
        timeout: Wall-clock budget in seconds; defaults to
            :func:`configured_timeout`. A value ``<= 0`` disables the wall-clock
            bound but the other budgets still apply.
        cpu_time: CPU-time budget in seconds (default from configuration).
        max_memory_bytes: Peak-memory budget (default from configuration).
        max_output_bytes: Serialized-result budget (default from configuration).
        max_file_descriptors: Descriptor-growth budget (default from
            configuration; POSIX only).

    Returns:
        Whatever ``func`` returns.

    Raises:
        PluginTimeoutError: If ``func`` does not finish within the budget.
        PluginResourceLimitError: If a CPU, memory, descriptor, or output bound
            is exceeded.
        Exception: Any exception raised by ``func`` is re-raised unchanged.
    """
    limits = get_plugin_limits()
    limit = limits.execution_timeout if timeout is None else float(timeout)
    cpu_limit = limits.cpu_time_seconds if cpu_time is None else float(cpu_time)
    memory_limit = limits.max_memory_bytes if max_memory_bytes is None else int(max_memory_bytes)
    output_limit = limits.max_output_bytes if max_output_bytes is None else int(max_output_bytes)
    fd_limit = (
        limits.max_file_descriptors if max_file_descriptors is None else int(max_file_descriptors)
    )

    call = _preserve_app_context(func)

    def _measured() -> Any:
        return _run_measured(
            call,
            operation=operation,
            cpu_limit=cpu_limit,
            memory_limit=memory_limit,
            fd_limit=fd_limit,
        )

    if limit <= 0:
        result = _measured()
    else:
        outcome: dict[str, Any] = {}
        finished = threading.Event()

        def _target() -> None:
            try:
                outcome["result"] = _measured()
            except BaseException as exc:  # re-raised in the caller thread below
                outcome["error"] = exc
            finally:
                finished.set()

        worker = threading.Thread(
            target=_target,
            name=f"plugin-callback:{str(operation)[:_MAX_OPERATION_CHARS]}",
            daemon=True,
        )
        worker.start()

        if not finished.wait(limit):
            logger.warning(
                "Plugin operation %s exceeded its %.3fs budget; abandoning the callback",
                operation,
                limit,
            )
            raise PluginTimeoutError(operation, limit)

        if "error" in outcome:
            raise outcome["error"]
        result = outcome.get("result")

    if output_limit > 0:
        size = _output_size(result)
        if size > output_limit:
            raise PluginOutputLimitError(operation, size, output_limit)
    return result


__all__ = [
    "DEFAULT_CPU_TIME_SECONDS",
    "DEFAULT_MAX_FILE_DESCRIPTORS",
    "DEFAULT_MAX_MEMORY_BYTES",
    "DEFAULT_MAX_OUTPUT_BYTES",
    "DEFAULT_TIMEOUT_SECONDS",
    "PluginCPUTimeLimitError",
    "PluginFileDescriptorLimitError",
    "PluginMemoryLimitError",
    "PluginOutputLimitError",
    "PluginResourceLimitError",
    "PluginTimeoutError",
    "configured_timeout",
    "run_with_timeout",
]
