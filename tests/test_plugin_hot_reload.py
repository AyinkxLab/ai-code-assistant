"""Development-only plugin hot reload (#411)."""

import pytest

from app.services import plugin_ops


def test_hot_reload_is_disabled_in_production(app):
    app.config.update(DEBUG=False, APP_ENV="production")

    assert plugin_ops.hot_reload_enabled(app.config) is False
    with pytest.raises(plugin_ops.PluginReloadDisabledError):
        plugin_ops.reload_plugin("example", config=app.config)


def test_hot_reload_proceeds_in_development(app):
    app.config.update(DEBUG=False, APP_ENV="development")

    assert plugin_ops.hot_reload_enabled(app.config) is True
    with pytest.raises(plugin_ops.PluginNotFoundError):
        plugin_ops.reload_plugin("example", config=app.config)
