"""A desktop-only package (desktop/plugin.js, no __init__.py) loads cleanly instead of failing import."""

import logging

from hermes_cli.plugins import PluginManager


def _manager(tmp_path, monkeypatch, files):
    home = tmp_path / "home"
    plugin = home / "plugins" / "panel_only"
    for name, text in files.items():
        (plugin / name).parent.mkdir(parents=True, exist_ok=True)
        (plugin / name).write_text(text)
    (home / "config.yaml").write_text("plugins:\n  enabled: [panel_only]\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    manager = PluginManager()
    manager.discover_and_load()
    return manager._plugins["panel_only"]


def test_desktop_only_package_loads_without_python(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    loaded = _manager(tmp_path, monkeypatch, {
        "plugin.yaml": "name: panel_only\nversion: 0.1.0\nmanifest_version: 1\nkind: standalone\n",
        "desktop/plugin.js": "export default {}\n"})
    assert loaded.error is None
    assert loaded.enabled
    assert "No __init__.py" not in caplog.text


def test_package_without_python_or_desktop_half_still_fails(tmp_path, monkeypatch):
    loaded = _manager(tmp_path, monkeypatch, {"plugin.yaml": "name: panel_only\nversion: 0.1.0\n"})
    assert loaded.error and "__init__.py" in loaded.error


def test_provides_middleware_is_a_known_manifest_field(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    _manager(tmp_path, monkeypatch, {
        "plugin.yaml": "name: panel_only\nversion: 0.1.0\nmanifest_version: 2\nprovides_middleware: []\n",
        "__init__.py": "def register(ctx):\n    pass\n"})
    assert "unknown manifest field" not in caplog.text
