# Hello World plugin (example)

A minimal, copy-pasteable plugin used by
[`docs/plugin-developer-guide.md`](../../../docs/plugin-developer-guide.md).

- `manifest.json` — manifest (id, entry point, declared `PROJECT_READ` capability).
- `plugin.py` — `HelloWorldPlugin`, which subscribes to `project.created`.

Nothing here is granted automatically: the plugin must be installed, enabled in
a workspace, and explicitly granted `PROJECT_READ` before its handler runs.

## Try it

```bash
# Register the plugin (operator scope; no code is executed, no grants created)
flask plugins install examples/plugins/hello_world
flask plugins inspect hello-world
flask plugins enable hello-world
```

To have the entry point importable during local development, add the plugin's
parent directory to `PYTHONPATH`:

```bash
# POSIX
export PYTHONPATH="$PWD/examples/plugins:$PYTHONPATH"
# PowerShell
$env:PYTHONPATH = "$PWD\examples\plugins;$env:PYTHONPATH"
```

Then install it into a workspace and grant the capability through the management
API or UI (see the guide).
