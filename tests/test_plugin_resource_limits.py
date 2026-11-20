"""Tests for plugin callback resource limits (#689).

The wall-clock timeout, CPU time, memory, descriptor growth, and output size
are all bounded. A callback that exceeds a bound fails cleanly (and dispatch
keeps running) instead of crashing the worker.
"""

import time

import pytest

from app.extensions import db
from app.models import ActivityEvent
from app.models import Plugin as PluginRow
from app.models.activity_event import EVENT_PLUGIN_EXECUTED
from app.services.events import Event, EventDispatcher
from app.services.plugin_execution import (
    PluginCPUTimeLimitError,
    PluginFileDescriptorLimitError,
    PluginMemoryLimitError,
    PluginOutputLimitError,
    PluginTimeoutError,
    run_with_timeout,
)
from app.services.plugin_limits import (
    DEFAULT_CPU_TIME_SECONDS,
    DEFAULT_MAX_FILE_DESCRIPTORS,
    DEFAULT_MAX_MEMORY_BYTES,
    DEFAULT_MAX_OUTPUT_BYTES,
    get_plugin_limits,
)

NEW_LIMIT_SETTINGS = (
    "PLUGIN_CPU_TIME_SECONDS",
    "PLUGIN_MAX_MEMORY_BYTES",
    "PLUGIN_MAX_OUTPUT_BYTES",
    "PLUGIN_MAX_FILE_DESCRIPTORS",
)


class TestLimitResolution:
    def test_safe_defaults_when_settings_are_missing(self, app):
        for name in NEW_LIMIT_SETTINGS:
            app.config.pop(name, None)

        limits = get_plugin_limits()

        assert limits.cpu_time_seconds == DEFAULT_CPU_TIME_SECONDS
        assert limits.max_memory_bytes == DEFAULT_MAX_MEMORY_BYTES
        assert limits.max_output_bytes == DEFAULT_MAX_OUTPUT_BYTES
        assert limits.max_file_descriptors == DEFAULT_MAX_FILE_DESCRIPTORS

    def test_configuration_overrides_are_read(self, app):
        app.config.update(
            PLUGIN_CPU_TIME_SECONDS=1.5,
            PLUGIN_MAX_MEMORY_BYTES=2048,
            PLUGIN_MAX_OUTPUT_BYTES=512,
            PLUGIN_MAX_FILE_DESCRIPTORS=7,
        )

        limits = get_plugin_limits()

        assert limits.cpu_time_seconds == pytest.approx(1.5)
        assert limits.max_memory_bytes == 2048
        assert limits.max_output_bytes == 512
        assert limits.max_file_descriptors == 7

    def test_invalid_values_fall_back_to_defaults(self, app):
        app.config.update(
            PLUGIN_MAX_MEMORY_BYTES="nope",
            PLUGIN_MAX_OUTPUT_BYTES=0,
            PLUGIN_MAX_FILE_DESCRIPTORS=-1,
        )

        limits = get_plugin_limits()

        assert limits.max_memory_bytes == DEFAULT_MAX_MEMORY_BYTES
        assert limits.max_output_bytes == DEFAULT_MAX_OUTPUT_BYTES
        assert limits.max_file_descriptors == DEFAULT_MAX_FILE_DESCRIPTORS


class TestExecutionBounds:
    def test_wall_clock_timeout_is_raised(self):
        with pytest.raises(PluginTimeoutError):
            run_with_timeout(lambda: time.sleep(1), operation="timeout", timeout=0.02)

    def test_cpu_budget_fails_cleanly(self):
        with pytest.raises(PluginCPUTimeLimitError):
            run_with_timeout(
                lambda: sum(range(200_000)),
                operation="cpu",
                timeout=5,
                cpu_time=1e-9,
            )

    def test_memory_budget_fails_cleanly(self):
        with pytest.raises(PluginMemoryLimitError):
            run_with_timeout(
                lambda: bytearray(1024 * 1024),
                operation="memory",
                timeout=5,
                max_memory_bytes=1024,
            )

    def test_output_budget_fails_cleanly(self):
        with pytest.raises(PluginOutputLimitError) as excinfo:
            run_with_timeout(
                lambda: "x" * 100,
                operation="output",
                timeout=5,
                max_output_bytes=10,
            )

        assert excinfo.value.size_bytes > excinfo.value.limit
        assert excinfo.value.limit == 10

    def test_descriptor_growth_fails_cleanly(self, monkeypatch):
        counts = iter((0, 5))
        monkeypatch.setattr("app.services.plugin_execution._open_fd_count", lambda: next(counts))

        with pytest.raises(PluginFileDescriptorLimitError):
            run_with_timeout(
                lambda: None,
                operation="descriptors",
                timeout=5,
                max_file_descriptors=1,
            )

    def test_small_result_stays_within_all_defaults(self):
        assert run_with_timeout(lambda: {"ok": True}, operation="ok", timeout=1) == {"ok": True}


class TestDispatchFailureIsolation:
    def test_oversized_output_fails_the_handler_not_the_worker(self, app):
        app.config["PLUGIN_MAX_OUTPUT_BYTES"] = 10
        app.config["PLUGIN_EXECUTION_TIMEOUT"] = 1
        db.session.add(
            PluginRow(
                id="noisy-plugin",
                name="Noisy plugin",
                version="1.2.3",
                entry_point="noisy:Noisy",
                capabilities=["GITHUB_READ"],
                enabled=True,
            )
        )
        db.session.commit()

        dispatcher = EventDispatcher()
        dispatcher.subscribe("github.connected", lambda event: "x" * 100, plugin_id="noisy-plugin")

        result = dispatcher.dispatch(Event(event_type="github.connected", data={}))

        assert result["failed"] == 1
        assert isinstance(result["errors"][0][1], PluginOutputLimitError)
        db.session.commit()

        audit = ActivityEvent.query.filter_by(
            workspace_id=None, event_type=EVENT_PLUGIN_EXECUTED
        ).first()
        # Global events have no workspace, so no workspace-scoped audit row is
        # written; the failure is still isolated and dispatch returned normally.
        assert audit is None
