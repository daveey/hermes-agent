import threading

import pytest

from hermes_cli import plugins_loader


def test_nested_plugin_load_runs_inline_on_deadline_worker(monkeypatch):
    monkeypatch.setattr(plugins_loader, "_resolve_plugin_load_timeout", lambda: 0.3)
    abandoned = []

    class Context:
        def __init__(self, name):
            self.name = name

        def _abandon_load(self):
            abandoned.append(self.name)

    observed_threads = []
    release = threading.Event()

    def outer_load():
        observed_threads.append(threading.current_thread())
        plugins_loader.run_with_load_deadline(
            "done", Context("done"), lambda: observed_threads.append(threading.current_thread()),
        )
        plugins_loader.run_with_load_deadline("hung", Context("hung"), release.wait)

    with pytest.raises(plugins_loader.PluginLoadTimeout):
        plugins_loader.run_with_load_deadline("outer", Context("outer"), outer_load)
    release.set()

    assert len(observed_threads) == 2
    assert observed_threads[0] is observed_threads[1]
    # The outer timeout abandons only contexts still loading, not a nested load that already finished.
    assert abandoned == ["outer", "hung"]


# ── sys.modules eviction vs. concurrent imports ─────────────────────────────
# On hermes-hub, "Failed to load plugin '<x>': dictionary changed size during iteration" dropped a random
# plugin every 10-40 min: _evict_modules walked the live sys.modules while another thread imported.
# The trap key below makes that interleaving deterministic: the first time the eviction scan compares
# against it, a *separate thread* imports (inserts into sys.modules) and finishes before the scan resumes.

import sys
import types


class _ImportsOnCompare(str):
    __hash__ = str.__hash__

    def __new__(cls, value):
        obj = super().__new__(cls, value)
        obj.armed = False
        obj.fired = False
        obj.inserted = f"{value}__concurrent_import"
        return obj

    def __eq__(self, other):
        if self.armed and not self.fired:
            self.fired = True
            worker = threading.Thread(
                target=sys.modules.__setitem__, args=(self.inserted, types.ModuleType(self.inserted)))
            worker.start()
            worker.join()
        return str.__eq__(self, other)


@pytest.fixture
def concurrent_import_trap():
    trap = _ImportsOnCompare("_hermes_test_concurrent_import_trap")
    sys.modules[trap] = types.ModuleType(str(trap))
    try:
        yield trap
    finally:
        trap.armed = False
        sys.modules.pop(trap, None)
        sys.modules.pop(trap.inserted, None)


def test_evict_modules_tolerates_concurrent_import(concurrent_import_trap):
    sys.modules["_hermes_test_evict_me"] = types.ModuleType("_hermes_test_evict_me")
    sys.modules["_hermes_test_evict_me.sub"] = types.ModuleType("_hermes_test_evict_me.sub")
    concurrent_import_trap.armed = True

    plugins_loader._evict_modules("_hermes_test_evict_me")

    assert concurrent_import_trap.fired, "the trap never fired, so the race was not exercised"
    assert "_hermes_test_evict_me" not in sys.modules
    assert "_hermes_test_evict_me.sub" not in sys.modules


def test_plugin_host_child_evict_tolerates_concurrent_import(concurrent_import_trap):
    from hermes_cli import plugin_host_child

    sys.modules["_hermes_test_child_evict"] = types.ModuleType("_hermes_test_child_evict")
    concurrent_import_trap.armed = True

    plugin_host_child._evict_modules("_hermes_test_child_evict")

    assert concurrent_import_trap.fired
    assert "_hermes_test_child_evict" not in sys.modules


def test_directory_plugin_loads_while_another_thread_imports(tmp_path, monkeypatch, concurrent_import_trap):
    """End to end: the real load path (_load_plugin -> deadline worker -> _load_directory_module) must
    register the plugin, not log "Failed to load plugin", when an import lands mid-eviction."""
    from hermes_cli.plugins import PluginManager, PluginManifest

    plugin_dir = tmp_path / "race_probe"
    plugin_dir.mkdir()
    (plugin_dir / "__init__.py").write_text("LOADED = []\ndef register(ctx):\n    LOADED.append(1)\n")
    manifest = PluginManifest(name="race_probe", source="user", path=str(plugin_dir))
    manager = PluginManager()
    monkeypatch.setattr(manager, "_runs_in_plugin_host", lambda _m: False)
    concurrent_import_trap.armed = True

    manager._load_plugin(manifest)

    loaded = manager._plugins["race_probe"]
    assert concurrent_import_trap.fired
    assert loaded.error is None, loaded.error
    assert loaded.enabled
    assert loaded.module.LOADED == [1]


def test_load_failure_warning_names_where_it_raised(tmp_path, monkeypatch, caplog):
    """Without HERMES_PLUGINS_DEBUG the warning carries no traceback; it must still name the raising frame."""
    import logging

    from hermes_cli.plugins import PluginManager, PluginManifest

    plugin_dir = tmp_path / "raises_probe"
    plugin_dir.mkdir()
    (plugin_dir / "__init__.py").write_text("def register(ctx):\n    raise RuntimeError('boom')\n")
    manager = PluginManager()
    monkeypatch.setattr(manager, "_runs_in_plugin_host", lambda _m: False)

    with caplog.at_level(logging.WARNING, logger="hermes_cli.plugins"):
        manager._load_plugin(PluginManifest(name="raises_probe", source="user", path=str(plugin_dir)))

    assert manager._plugins["raises_probe"].error == "boom"
    [line] = [r.getMessage() for r in caplog.records if "Failed to load plugin 'raises_probe'" in r.getMessage()]
    assert f"raised at {plugin_dir / '__init__.py'}:2 in register" in line
