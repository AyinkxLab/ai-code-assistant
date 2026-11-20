# Isolated plugin runner

`app/services/plugin_runner.py` executes one plugin entry point in a separate
Python process. It is the stronger isolation option next to the in-process,
measure-only `plugin_execution` budgets.

## What it does

- Builds the child environment from an **explicit allowlist**
  (`DEFAULT_ENV_ALLOWLIST`): `PATH`, `PYTHONPATH`, `PYTHONHOME`,
  `PYTHONIOENCODING`, `SYSTEMROOT`, `WINDIR`, `TEMP`, `TMP`, `LANG`, `LC_ALL`.
  `os.environ` is otherwise not inherited, so `SECRET_KEY`, provider API keys,
  OAuth secrets, and tokens never reach the child.
- Drains stdout and stderr through bounded readers, keeping at most
  `max_output_bytes` (64 KiB by default) of each.
- Enforces a wall-clock `timeout` (10 seconds by default) and kills the child on
  expiry.
- Passes a JSON payload in and a JSON result out. Nothing but plain data crosses
  the process boundary.

The entry point is `module:Attribute`. A class is instantiated with no
arguments; a callable is used as-is. The object must expose `run(payload)` or be
callable itself.

```python
from app.services.plugin_runner import run_plugin

result = run_plugin("my_plugin.plugin:MyPlugin", {"event": "project.created"})
print(result.result)
```

Failures are structured (`PluginRunnerError` subclasses with a stable `code`):
`invalid_input`, `timeout`, `output_limit`, and `crash`.

## Isolation model and its honest limits

This is **process isolation, not a security sandbox**.

- The child runs as the same OS user, with the same filesystem and network
  access. It can read files the application can read and open sockets.
- Python-level interpreter state is fresh, but installed site-packages and the
  interpreter itself are shared.
- Background processes started by the plugin are outside this module's bounds.
- The environment allowlist protects against accidental secret inheritance; it
  does not stop code from reading a secret file on disk.

Use OS-level isolation (container, dedicated user, seccomp/AppArmor, network
policy) when plugin code is untrusted. `plugin_network` remains the policy guard
for outbound HTTP made through the plugin transport.

## Tests

`tests/test_plugin_runner.py` uses the deterministic plugin in
`tests/fixtures/plugin_runner/sample_plugin.py` and covers success, timeout,
oversized output, crash, invalid input, and environment isolation (a planted
secret in the parent environment is not visible to the child).
