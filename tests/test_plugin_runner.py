"""Tests for the isolated plugin runner (#687).

The deterministic plugin lives in ``tests/fixtures/plugin_runner``. Every test
that starts a child passes an explicit environment built from the allowlist, so
nothing from the developer's shell can influence the outcome.
"""

import os
from pathlib import Path

import pytest

from app.services.plugin_runner import (
    DEFAULT_ENV_ALLOWLIST,
    PluginRunnerCrash,
    PluginRunnerInvalidInput,
    PluginRunnerOutputLimit,
    PluginRunnerTimeout,
    build_runner_env,
    run_plugin,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "plugin_runner"
ENTRY_POINT = "sample_plugin:SamplePlugin"


def _env(**extra):
    values = {"PYTHONPATH": str(FIXTURE_DIR)}
    values.update(extra)
    return build_runner_env(extra=values)


def _run(payload, **kwargs):
    kwargs.setdefault("env", _env())
    kwargs.setdefault("timeout", 10)
    return run_plugin(ENTRY_POINT, payload, **kwargs)


class TestRunnerEnvironment:
    def test_allowlist_excludes_app_secrets(self, monkeypatch):
        monkeypatch.setenv("AICA_TEST_PLANTED_SECRET", "must-not-leak")
        monkeypatch.setenv("SECRET_KEY", "also-must-not-leak")

        result = _run({"mode": "env", "probe": "AICA_TEST_PLANTED_SECRET"})

        assert result.result["probe"] is None
        assert "AICA_TEST_PLANTED_SECRET" not in result.result["keys"]
        assert "SECRET_KEY" not in result.result["keys"]
        secretish = {"key", "secret", "token", "password", "credential"}
        assert not any(token in key.lower() for key in result.result["keys"] for token in secretish)

    def test_build_env_ignores_unlisted_variables(self):
        env = build_runner_env(
            {"PATH": "/bin", "OPENAI_API_KEY": "sk-nope", "PLUGIN_EXTRA": "ok"},
            extra={"PLUGIN_EXTRA": "ok"},
        )
        assert env["PATH"] == "/bin"
        assert "OPENAI_API_KEY" not in env
        assert env["PLUGIN_EXTRA"] == "ok"

    def test_empty_allowlist_means_empty_environment(self):
        assert build_runner_env({"PATH": "/bin"}, allowlist=[]) == {}


class TestRunnerSuccess:
    def test_returns_the_plugin_result(self):
        result = _run({"value": "hello"})

        assert result.result == {"echo": "hello"}
        assert result.returncode == 0
        assert result.entry_point == ENTRY_POINT


class TestRunnerFailures:
    def test_timeout_kills_the_child(self):
        with pytest.raises(PluginRunnerTimeout) as exc:
            _run({"mode": "timeout"}, timeout=0.3)
        assert exc.value.code == "timeout"

    def test_oversized_output_is_rejected(self):
        with pytest.raises(PluginRunnerOutputLimit) as exc:
            _run({"mode": "big_output"}, max_output_bytes=1024)
        assert exc.value.code == "output_limit"

    def test_crash_surfaces_the_exit_code(self):
        with pytest.raises(PluginRunnerCrash) as exc:
            _run({"mode": "crash"})
        assert exc.value.code == "crash"
        assert "deterministic crash" in str(exc.value)

    @pytest.mark.parametrize("entry_point", ["", "no-colon", ":missing", "module:"])
    def test_invalid_entry_point_is_structured(self, entry_point):
        with pytest.raises(PluginRunnerInvalidInput):
            run_plugin(entry_point, {}, env=_env())

    def test_invalid_bounds_are_structured(self):
        with pytest.raises(PluginRunnerInvalidInput):
            run_plugin(ENTRY_POINT, {}, timeout=0, env=_env())
        with pytest.raises(PluginRunnerInvalidInput):
            run_plugin(ENTRY_POINT, {}, max_output_bytes=-1, env=_env())

    def test_non_json_payload_is_rejected(self):
        with pytest.raises(PluginRunnerInvalidInput):
            run_plugin(ENTRY_POINT, {"bad": object()}, env=_env())

    def test_oversized_payload_is_rejected(self):
        with pytest.raises(PluginRunnerInvalidInput, match="payload"):
            run_plugin(
                ENTRY_POINT,
                {"value": "x" * 100},
                max_payload_bytes=10,
                env=_env(),
            )


def test_default_env_allowlist_has_no_secret_names():
    lowered = {name.lower() for name in DEFAULT_ENV_ALLOWLIST}
    assert not any("key" in name or "secret" in name or "token" in name for name in lowered)
    # Windows needs SYSTEMROOT/WINDIR for the interpreter; POSIX ignores them.
    assert os.sep  # exercise the import on the current platform
