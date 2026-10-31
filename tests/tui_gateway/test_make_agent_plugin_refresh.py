"""A plugin enabled after a long-lived process booted loads at its next NEW-session agent build. [daveey]

``PluginManager.discover_and_load`` returns early once ``_discovered`` is set, and ``hermes plugins
enable`` nudges only the host-level serve backend and the gateway socket: a per-profile ``hermes serve
--isolated`` backend that booted before the enable never registered the plugin's hooks until it
restarted (nine every-host guards stayed out of ten hub backends on 10-30). ``_make_agent`` (serve /
TUI) and the gateway's ``_build_fresh_agent`` now call ``refresh_plugins_for_new_session`` first, which
re-runs the forced pass only when the discovery inputs changed since the last sweep.
"""

from __future__ import annotations

import types
from types import SimpleNamespace

import pytest

import hermes_yaml as yaml
from hermes_cli import plugins as plugins_mod
from hermes_constants import reset_hermes_home_override, set_hermes_home_override

_GUARD = """
def _guard(tool_name=None, **_kw):
    return None

def register(ctx):
    ctx.register_hook("pre_tool_call", _guard)
"""


def _add_enabled_plugin(home, name: str) -> None:
    plugin_dir = home / "plugins" / name
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(yaml.safe_dump({"name": name, "version": "0.1.0"}), encoding="utf-8")
    (plugin_dir / "__init__.py").write_text(_GUARD, encoding="utf-8")
    cfg_path = home / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    cfg.setdefault("plugins", {}).setdefault("enabled", []).append(name)
    cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")


def _hook_owners(manager, hook: str) -> set[str]:
    return {getattr(cb, "__module__", "") for cb in manager._hooks.get(hook, [])}


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    (home / "plugins").mkdir(parents=True)
    (home / "config.yaml").write_text(yaml.safe_dump({"plugins": {"enabled": []}}), encoding="utf-8")
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(bundled))  # no bundled plugins: a fast, exact registry
    monkeypatch.delenv("HERMES_SAFE_MODE", raising=False)
    plugins_mod._reset_plugin_managers_for_tests()
    token = set_hermes_home_override(home)
    yield home
    reset_hermes_home_override(token)
    plugins_mod._reset_plugin_managers_for_tests()


@pytest.fixture
def sweeps(home, monkeypatch):
    """The discovered manager for ``home`` plus a counter of real discovery sweeps after boot."""
    manager = plugins_mod.get_plugin_manager()
    manager.discover_and_load()
    assert manager._discovered and manager._swept_fingerprint is not None
    count = {"n": 0}
    inner = manager._discover_and_load_inner

    def counted():
        count["n"] += 1
        return inner()

    monkeypatch.setattr(manager, "_discover_and_load_inner", counted)
    return manager, count


def _build_tui_agent(monkeypatch):
    from tui_gateway import server
    monkeypatch.setattr(server, "_resolve_agent_model_runtime", lambda *_a: ("test-model", {}))
    monkeypatch.setattr(server, "_load_enabled_toolsets", lambda *_a, **_kw: None)
    monkeypatch.setattr(server, "_get_db", lambda: None)
    monkeypatch.setattr(server, "_agent_cbs", lambda sid: {})
    monkeypatch.setattr("run_agent.AIAgent", lambda **kw: types.SimpleNamespace(model=kw.get("model")))
    server._make_agent("sid", "session-key")


def _build_gateway_agent():
    from gateway.session_execution import GatewaySessionAgentMixin
    turn = GatewaySessionAgentMixin.__new__(GatewaySessionAgentMixin)
    turn._runner = SimpleNamespace(session_authority=None, _prefill_messages=None, _service_tier=None,
                                   _session_db=None, _refresh_fallback_model=lambda: None, config=None)
    source = SimpleNamespace(chat_id="c", user_id="u", user_id_alt=None, user_name=None, chat_name=None,
                             chat_type="dm", thread_id=None, platform=None)
    turn._ctx = SimpleNamespace(source=source, user_config={}, enabled_toolsets=[], disabled_toolsets=None,
                                session_id="c", session_key="k", AIAgent=lambda **kw: kw)
    turn._build_fresh_agent({"model": "m", "runtime": {}}, "local", None, 1, None, {}, False)


def test_new_session_build_loads_a_plugin_enabled_after_boot(sweeps, home, monkeypatch):
    manager, count = sweeps
    assert "refresh_probe_guard" not in _hook_owners(manager, "pre_tool_call")
    _add_enabled_plugin(home, "refresh_probe_guard")

    _build_tui_agent(monkeypatch)

    assert count["n"] == 1
    assert "refresh_probe_guard" in manager._plugins
    assert any("refresh_probe_guard" in owner for owner in _hook_owners(manager, "pre_tool_call"))


def test_unchanged_inputs_run_no_forced_pass(sweeps, monkeypatch):
    _manager, count = sweeps
    for _ in range(3):
        _build_tui_agent(monkeypatch)
    assert count["n"] == 0
    assert plugins_mod.refresh_plugins_for_new_session() is False


def test_gateway_fresh_agent_build_loads_a_plugin_enabled_after_boot(sweeps, home, monkeypatch):
    from gateway import session_policy
    monkeypatch.setattr(session_policy, "policy_for_source", lambda runner, source: None)
    manager, count = sweeps
    _build_gateway_agent()
    assert count["n"] == 0  # unchanged inputs: no pass
    _add_enabled_plugin(home, "gateway_probe_guard")

    _build_gateway_agent()
    _build_gateway_agent()  # the second build sees the inputs the first one swept

    assert count["n"] == 1
    assert any("gateway_probe_guard" in owner for owner in _hook_owners(manager, "pre_tool_call"))


def test_an_undiscovered_manager_is_left_to_lazy_discovery(home):
    manager = plugins_mod.get_plugin_manager()
    assert not manager._discovered
    assert manager.refresh_if_inputs_changed() is False
    assert not manager._discovered
