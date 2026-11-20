"""Isolated execution of plugin entry points in a child process (#687).

Plugin code is third-party code. In-process execution (see
:mod:`app.services.plugin_execution`) bounds time and resources but shares the
application's memory, environment, and interpreter. This module offers the
stronger option of running one entry point in a fresh Python subprocess:

* the child receives an **explicit environment allowlist** — never
  ``os.environ`` — so application secrets (``SECRET_KEY``, API keys, provider
  credentials) are not inherited;
* stdout and stderr are drained with a hard byte cap, so a chatty plugin cannot
  exhaust memory;
* the whole run has a wall-clock timeout, after which the child is killed;
* the payload and the returned result are JSON, so nothing but plain data
  crosses the process boundary.

Isolation limits (read this before relying on it for security):

* This is **process isolation, not a security sandbox**. The child runs as the
  same OS user, with the same filesystem and network access. It can read files
  the application can read and open sockets. Use OS-level sandboxing
  (containers, seccomp, a dedicated user) when untrusted code must be contained.
* Python-level interpreter state is fresh, but site-packages are shared.
* A plugin that starts background processes of its own is outside this bound.

The child protocol is small: ``module:Attribute`` names a class (instantiated
with no arguments) or a callable. The instance/callable must expose ``run`` (or
be callable itself) and accept one JSON payload argument.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

#: Environment variables passed to the child when a caller does not supply one.
#: Only interpreter/runtime basics are included; nothing secret is on this list.
DEFAULT_ENV_ALLOWLIST = (
    "PATH",
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONIOENCODING",
    "SYSTEMROOT",
    "WINDIR",
    "TEMP",
    "TMP",
    "LANG",
    "LC_ALL",
)

#: Default wall-clock budget for one run, in seconds.
DEFAULT_TIMEOUT = 10.0

#: Default cap on each of stdout and stderr, in bytes.
DEFAULT_MAX_OUTPUT_BYTES = 64 * 1024

#: Default cap on the JSON payload sent to the child, in bytes.
DEFAULT_MAX_PAYLOAD_BYTES = 1024 * 1024

#: Read size used by the bounded output drainers.
_READ_SIZE = 8192

_CHILD_SCRIPT = r"""
import importlib
import json
import sys

entry_point, payload_text = sys.argv[1], sys.stdin.read()
module_name, _, attribute = entry_point.partition(":")
if not module_name or not attribute:
    raise SystemExit("entry point must be 'module:Attribute'")

payload = json.loads(payload_text or "{}")
module = importlib.import_module(module_name)
target = getattr(module, attribute)
instance = target() if isinstance(target, type) else target
run = getattr(instance, "run", instance)
result = run(payload)
sys.stdout.write(json.dumps({"ok": True, "result": result}, default=repr))
"""


class PluginRunnerError(RuntimeError):
    """Base class for isolated-runner failures. ``code`` is stable for callers."""

    code = "runner_error"


class PluginRunnerInvalidInput(PluginRunnerError):
    """The entry point, payload, or a bound argument is invalid."""

    code = "invalid_input"


class PluginRunnerTimeout(PluginRunnerError):
    """The child did not finish within the wall-clock budget."""

    code = "timeout"


class PluginRunnerOutputLimit(PluginRunnerError):
    """The child wrote more than the allowed stdout/stderr bytes."""

    code = "output_limit"


class PluginRunnerCrash(PluginRunnerError):
    """The child exited non-zero or returned an unreadable result."""

    code = "crash"


@dataclass(frozen=True)
class PluginRunResult:
    """A successful isolated run."""

    entry_point: str
    result: Any
    stdout: str
    stderr: str
    returncode: int

    def to_dict(self) -> dict[str, Any]:
        """Return the result as JSON-serializable data."""
        return {
            "entry_point": self.entry_point,
            "result": self.result,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "returncode": self.returncode,
        }


def build_runner_env(
    environ: Mapping[str, str] | None = None,
    *,
    allowlist: tuple[str, ...] | list[str] | None = None,
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build the child environment from an explicit allowlist.

    ``environ`` defaults to ``os.environ`` but only the names in ``allowlist``
    are copied. ``extra`` values are added deliberately by the caller (for
    example a fixture directory on ``PYTHONPATH``) and are never inherited.
    """
    source = os.environ if environ is None else environ
    allowed = DEFAULT_ENV_ALLOWLIST if allowlist is None else tuple(allowlist)
    env = {name: str(source[name]) for name in allowed if name in source}
    if extra:
        env.update({str(name): str(value) for name, value in extra.items()})
    return env


def _check_entry_point(entry_point: Any) -> str:
    if not isinstance(entry_point, str) or ":" not in entry_point:
        raise PluginRunnerInvalidInput("Plugin entry point must be 'module:Attribute'")
    module_name, _, attribute = entry_point.partition(":")
    if not module_name.strip() or not attribute.strip():
        raise PluginRunnerInvalidInput("Plugin entry point must be 'module:Attribute'")
    return entry_point


def _validate_bound(name: str, value: Any) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise PluginRunnerInvalidInput(f"{name} must be a positive number")
    return value


def _drain(stream: Any, limit: int, state: dict[str, Any], key: str) -> None:
    """Read ``stream`` to EOF, keeping at most ``limit`` bytes."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = stream.read(_READ_SIZE)
        if not chunk:
            break
        total += len(chunk)
        if total <= limit:
            chunks.append(chunk)
        else:
            state["overflow"] = True
    state[key] = b"".join(chunks)


def run_plugin(
    entry_point: str,
    payload: Mapping[str, Any] | None = None,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
    env: Mapping[str, str] | None = None,
    env_allowlist: tuple[str, ...] | list[str] | None = None,
    cwd: str | os.PathLike[str] | None = None,
    python: str | None = None,
) -> PluginRunResult:
    """Run ``entry_point`` in a fresh Python process and return its result.

    Args:
        entry_point: ``module:Attribute``; a class is instantiated with no
            arguments, a callable is used as-is. The target must expose
            ``run(payload)`` or be callable itself.
        payload: JSON-serializable mapping passed as the single argument.
        timeout: Wall-clock budget in seconds; the child is killed when it
            expires.
        max_output_bytes: Cap applied separately to stdout and stderr.
        max_payload_bytes: Cap on the serialized payload.
        env: Explicit child environment. When ``None``, one is built from
            ``os.environ`` using ``env_allowlist`` (no secrets are inherited).
        env_allowlist: Override :data:`DEFAULT_ENV_ALLOWLIST`.
        cwd: Working directory for the child.
        python: Interpreter path; defaults to ``sys.executable``.

    Raises:
        PluginRunnerInvalidInput: For an invalid entry point, payload, or bound.
        PluginRunnerTimeout: When the child exceeds ``timeout``.
        PluginRunnerOutputLimit: When stdout or stderr exceeds the cap.
        PluginRunnerCrash: When the child exits non-zero or returns bad JSON.
    """
    entry = _check_entry_point(entry_point)
    _validate_bound("timeout", timeout)
    _validate_bound("max_output_bytes", max_output_bytes)
    _validate_bound("max_payload_bytes", max_payload_bytes)

    data = dict(payload or {})
    try:
        payload_bytes = json.dumps(data, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise PluginRunnerInvalidInput(f"Plugin payload is not JSON-serializable: {exc}") from exc
    if len(payload_bytes) > max_payload_bytes:
        raise PluginRunnerInvalidInput(f"Plugin payload exceeds the {max_payload_bytes} byte cap")

    child_env = dict(env) if env is not None else build_runner_env(allowlist=env_allowlist)

    process = subprocess.Popen(
        [python or sys.executable, "-c", _CHILD_SCRIPT, entry],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(cwd) if cwd is not None else None,
        env=child_env,
    )

    state: dict[str, Any] = {}
    readers = [
        threading.Thread(
            target=_drain,
            args=(process.stdout, max_output_bytes, state, "stdout"),
            daemon=True,
        ),
        threading.Thread(
            target=_drain,
            args=(process.stderr, max_output_bytes, state, "stderr"),
            daemon=True,
        ),
    ]
    for reader in readers:
        reader.start()

    try:
        assert process.stdin is not None
        process.stdin.write(payload_bytes)
        process.stdin.close()
    except (BrokenPipeError, OSError):
        # The child may have crashed before reading its payload; its exit code
        # and stderr below describe the failure more usefully.
        pass

    try:
        returncode = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        process.wait()
        for reader in readers:
            reader.join(timeout=1)
        raise PluginRunnerTimeout(f"Plugin {entry!r} exceeded its {timeout:g}s timeout") from exc

    for reader in readers:
        reader.join(timeout=1)

    stdout = state.get("stdout", b"").decode("utf-8", errors="replace")
    stderr = state.get("stderr", b"").decode("utf-8", errors="replace")

    if state.get("overflow"):
        raise PluginRunnerOutputLimit(
            f"Plugin {entry!r} exceeded the {max_output_bytes} byte output cap"
        )
    if returncode != 0:
        detail = stderr.strip()[:500] or "no stderr"
        raise PluginRunnerCrash(f"Plugin {entry!r} exited with {returncode}: {detail}")

    try:
        envelope = json.loads(stdout)
    except ValueError as exc:
        raise PluginRunnerCrash(f"Plugin {entry!r} returned unreadable output") from exc
    if not isinstance(envelope, dict) or envelope.get("ok") is not True:
        raise PluginRunnerCrash(f"Plugin {entry!r} returned an invalid result envelope")

    return PluginRunResult(
        entry_point=entry,
        result=envelope.get("result"),
        stdout=stdout,
        stderr=stderr,
        returncode=returncode,
    )


__all__ = [
    "DEFAULT_ENV_ALLOWLIST",
    "DEFAULT_MAX_OUTPUT_BYTES",
    "DEFAULT_MAX_PAYLOAD_BYTES",
    "DEFAULT_TIMEOUT",
    "PluginRunResult",
    "PluginRunnerCrash",
    "PluginRunnerError",
    "PluginRunnerInvalidInput",
    "PluginRunnerOutputLimit",
    "PluginRunnerTimeout",
    "build_runner_env",
    "run_plugin",
]
