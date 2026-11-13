"""[daveey] The ``*_nowait`` plugin probes never wait on a sweep another thread holds.

A force reload (hermes-sync's mods-reload) unloads first, so ``_discovered`` is False while the sweep
thread holds ``_discovery_lock``. The probe used to fall through to a blocking ``discover_plugins()``,
which parked the gateway event loop (``kanban_run -> build_policy -> get_plugin_toolset_keys_nowait``)
for 36 s. It now serves the last live set, else last launch's persisted set; a stale set self-heals
on the next probe after the sweep. With neither, the event loop gets ``set()``: callers use these keys
to INCLUDE default-on plugin toolsets too (``tools_config._get_platform_tools``), so that window drops
them from a session built in it, and the fallback is logged once.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time

import pytest

from hermes_cli import plugins as plugins_mod


@pytest.fixture
def manager(monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / "cache").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    mgr = plugins_mod.PluginManager()
    monkeypatch.setattr(plugins_mod, "get_plugin_manager", lambda: mgr)
    monkeypatch.setattr(plugins_mod, "_background_discovery_thread", None)
    return mgr


def _hold_lock(mgr):
    """Hold ``mgr._discovery_lock`` on another thread (a force reload mid-sweep) until released."""
    held, release = threading.Event(), threading.Event()

    def _sweep():
        with mgr._discovery_lock:
            mgr._discovered = False  # unload() reset it; re-registration has not finished
            held.set()
            release.wait(10)

    t = threading.Thread(target=_sweep, name="force-reload", daemon=True)
    t.start()
    assert held.wait(5)
    return t, release


async def _timed(fn):
    start = time.monotonic()
    value = fn()
    return value, time.monotonic() - start


def test_nowait_returns_last_live_set_while_reload_holds_lock(manager, monkeypatch):
    manager._discovered = True
    monkeypatch.setattr(plugins_mod, "get_plugin_toolsets", lambda: [("coach", "Coach", "")])
    assert plugins_mod.get_plugin_toolset_keys_nowait() == {"coach"}  # live answer is remembered

    t, release = _hold_lock(manager)
    try:
        keys, elapsed = asyncio.run(_timed(plugins_mod.get_plugin_toolset_keys_nowait))
    finally:
        release.set()
        t.join(5)
    assert elapsed < 0.5, f"nowait probe blocked {elapsed:.2f}s on _discovery_lock"
    assert keys == {"coach"}


def test_nowait_serves_persisted_set_when_never_answered_live(manager):
    (plugins_mod.get_hermes_home() / "cache" / "plugin_toolset_keys.json").write_text(
        json.dumps({"toolset_keys": ["a2a"], "portable_mcp": ["srv"]}))
    t, release = _hold_lock(manager)
    try:
        keys, elapsed = asyncio.run(_timed(plugins_mod.get_plugin_toolset_keys_nowait))
        mcp = plugins_mod.get_portable_mcp_server_names_nowait()
    finally:
        release.set()
        t.join(5)
    assert elapsed < 0.5
    assert keys == {"a2a"} and mcp == {"srv"}


def test_nowait_never_blocks_event_loop_without_any_known_set(manager, monkeypatch, caplog):
    """The empty on-loop fallback is served without blocking and logged once per field."""
    monkeypatch.setattr(plugins_mod, "_nowait_empty_logged", set())
    t, release = _hold_lock(manager)
    try:
        with caplog.at_level(logging.WARNING, logger=plugins_mod.logger.name):
            keys, elapsed = asyncio.run(_timed(plugins_mod.get_plugin_toolset_keys_nowait))
            again, _ = asyncio.run(_timed(plugins_mod.get_plugin_toolset_keys_nowait))
            mcp, _ = asyncio.run(_timed(plugins_mod.get_portable_mcp_server_names_nowait))
    finally:
        release.set()
        t.join(5)
    assert elapsed < 0.5
    assert keys == set() and again == set() and mcp == set()
    logged = [r.getMessage() for r in caplog.records if "serving an empty set" in r.getMessage()]
    assert len(logged) == 2, logged  # once per field, not once per probe
    assert "toolset_keys" in logged[0] and "plugin_toolset_keys.json" in logged[0]
    assert "portable_mcp" in logged[1]


def test_nowait_empty_fallback_not_logged_when_persisted_set_served(manager, monkeypatch, caplog):
    monkeypatch.setattr(plugins_mod, "_nowait_empty_logged", set())
    (plugins_mod.get_hermes_home() / "cache" / "plugin_toolset_keys.json").write_text(
        json.dumps({"toolset_keys": ["a2a"], "portable_mcp": []}))
    t, release = _hold_lock(manager)
    try:
        with caplog.at_level(logging.WARNING, logger=plugins_mod.logger.name):
            keys, _ = asyncio.run(_timed(plugins_mod.get_plugin_toolset_keys_nowait))
    finally:
        release.set()
        t.join(5)
    assert keys == {"a2a"}
    assert not [r for r in caplog.records if "serving an empty set" in r.getMessage()]


def test_nowait_blocks_off_loop_for_never_discovered_process(manager, monkeypatch):
    """Off the event loop, a process with no known set still waits for the sweep (old contract)."""
    calls = []
    monkeypatch.setattr(plugins_mod, "discover_plugins", lambda force=False: calls.append(force))
    monkeypatch.setattr(plugins_mod, "get_plugin_toolsets", lambda: [("x", "X", "")])
    assert plugins_mod.get_plugin_toolset_keys_nowait() == {"x"}
    assert calls == [False]


def test_nowait_idle_never_discovered_sweeps_inline_on_loop(manager, monkeypatch):
    """Documented contract: with the lock free and nothing remembered, the first sweep runs inline even
    on the event loop, and the persisted cache is not consulted."""
    (plugins_mod.get_hermes_home() / "cache" / "plugin_toolset_keys.json").write_text(
        json.dumps({"toolset_keys": ["stale"], "portable_mcp": []}))
    calls = []
    monkeypatch.setattr(plugins_mod, "discover_plugins", lambda force=False: calls.append(force))
    monkeypatch.setattr(plugins_mod, "get_plugin_toolsets", lambda: [("x", "X", "")])
    keys, _ = asyncio.run(_timed(plugins_mod.get_plugin_toolset_keys_nowait))
    assert keys == {"x"} and calls == [False]


def test_nowait_on_loop_skips_retry_after_failed_sweep(manager, monkeypatch):
    """A force reload that failed resets ``_discovered``; the loop serves the last live set instead of
    re-running the sweep inline (off the loop the retry still runs, as before)."""
    manager._discovered = True
    monkeypatch.setattr(plugins_mod, "get_plugin_toolsets", lambda: [("coach", "Coach", "")])
    assert plugins_mod.get_plugin_toolset_keys_nowait() == {"coach"}
    manager._discovered = False
    calls = []
    monkeypatch.setattr(plugins_mod, "discover_plugins", lambda force=False: calls.append(force))
    keys, _ = asyncio.run(_timed(plugins_mod.get_plugin_toolset_keys_nowait))
    assert keys == {"coach"} and calls == []
    plugins_mod.get_plugin_toolset_keys_nowait()
    assert calls == [False]
