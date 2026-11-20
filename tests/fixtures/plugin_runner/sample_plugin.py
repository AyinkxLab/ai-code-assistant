"""Deterministic plugin used by the isolated-runner tests (#687)."""

import os
import sys
import time


class SamplePlugin:
    """Test plugin covering the success and failure modes of the runner."""

    def run(self, payload):
        mode = payload.get("mode", "echo")
        if mode == "timeout":
            time.sleep(30)
            return {"unreachable": True}
        if mode == "crash":
            raise RuntimeError("deterministic crash")
        if mode == "big_output":
            for _ in range(2000):
                sys.stdout.write("x" * 1024)
            return {}
        if mode == "env":
            probe = payload.get("probe", "")
            return {
                "keys": sorted(os.environ),
                "probe": os.environ.get(probe) if probe else None,
            }
        return {"echo": payload.get("value")}
