"""[daveey] A forced plugin reload must not make a live tool call see its tools as unknown.

The reload unloads every plugin tool of the home, then registers them again. A ``tool_call`` that
resolved its name in between answered ``'tailnet_exec' is not a known tool name`` for a name the
session had just been handed by ``tool_search``. These tests hold a real ``PluginManager`` force
reload open inside the plugin's ``register()`` and drive the real dispatch entry points meanwhile.
"""

import json
import sys
import threading
import types
from pathlib import Path

import pytest

import model_tools  # imported up front: its import-time discovery must not run inside a held reload
from hermes_cli.plugins import get_plugin_manager
from hermes_constants import reset_hermes_home_override, set_hermes_home_override
from tests.hermes_cli.test_plugins import _make_plugin_dir
from tools import registry_reload
from tools.registry import registry

GATE = "_hermes_reload_gate_probe"
TOOL = "probe_reload_exec"
TOOLSET = "probe_reload"

PLUGIN_SOURCE = f'''
import json, sys

def register(ctx):
    gate = sys.modules[{GATE!r}]
    if gate.armed:
        gate.entered.set()
        gate.release.wait(30)
        if gate.fail:
            raise RuntimeError("reload failed on purpose")

    def handler(args, **kwargs):
        gate.calls.append(dict(args))
        return json.dumps({{"ok": True, "host": args.get("host")}})

    ctx.register_tool({TOOL!r}, {TOOLSET!r}, {{
        "name": {TOOL!r}, "description": "probe",
        "parameters": {{"type": "object", "properties": {{"host": {{"type": "string"}}}}}},
    }}, handler)
'''


@pytest.fixture
def gate(monkeypatch):
    module = types.ModuleType(GATE)
    module.armed = False
    module.fail = False
    module.entered = threading.Event()
    module.release = threading.Event()
    module.calls = []
    monkeypatch.setitem(sys.modules, GATE, module)
    yield module
    module.release.set()


@pytest.fixture
def manager(tmp_path, monkeypatch, gate):
    home = tmp_path / "hermes_home"
    plugin_dir = _make_plugin_dir(home / "plugins", "reload_probe", home=home)
    (plugin_dir / "__init__.py").write_text(PLUGIN_SOURCE, encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    mgr = get_plugin_manager()  # the manager the dispatch path's hooks use for this home
    mgr.discover_and_load()
    assert registry.get_entry(TOOL) is not None
    yield mgr
    gate.release.set()
    mgr.unload()


def _start(fn):
    box = {}

    def run():
        try:
            box["value"] = fn()
        except BaseException as exc:  # surfaced by the assertions below
            box["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, box


def _reload_held_open(manager, gate):
    """Start a force reload and return once it sits inside register() with the tool unloaded."""
    gate.armed = True
    thread, box = _start(lambda: manager.discover_and_load(force=True))
    assert gate.entered.wait(20), "force reload never reached the plugin's register()"
    assert registry.get_entry(TOOL) is None, "fixture: the reload should have unloaded the tool"
    return thread, box


def _tool_call():
    return json.loads(model_tools.handle_function_call(
        function_name="tool_call",
        function_args={"calls": [{"name": TOOL, "arguments": {"host": "mini"}}]},
        enabled_toolsets=[TOOLSET],
    ))


def test_tool_call_during_force_reload_waits_and_runs_once(manager, gate):
    reload_thread, reload_box = _reload_held_open(manager, gate)
    call_thread, call_box = _start(_tool_call)

    call_thread.join(0.5)
    assert call_thread.is_alive(), f"tool_call answered mid-reload: {call_box}"

    gate.release.set()
    reload_thread.join(20)
    call_thread.join(20)
    assert not reload_thread.is_alive() and "error" not in reload_box, reload_box
    assert call_box.get("value") == {"ok": True, "host": "mini"}, call_box
    assert gate.calls == [{"host": "mini"}]


def test_direct_dispatch_during_force_reload_waits(manager, gate):
    reload_thread, _ = _reload_held_open(manager, gate)
    call_thread, call_box = _start(lambda: json.loads(registry.dispatch(TOOL, {"host": "hub"})))

    call_thread.join(0.5)
    assert call_thread.is_alive(), f"dispatch answered mid-reload: {call_box}"

    gate.release.set()
    reload_thread.join(20)
    call_thread.join(20)
    assert call_box.get("value") == {"ok": True, "host": "hub"}, call_box
    assert gate.calls == [{"host": "hub"}]


def test_tool_search_during_force_reload_lists_the_tool(manager, gate):
    reload_thread, _ = _reload_held_open(manager, gate)
    call_thread, call_box = _start(lambda: model_tools.handle_function_call(
        function_name="tool_describe", function_args={"names": [TOOL]}, enabled_toolsets=[TOOLSET]))

    call_thread.join(0.5)
    assert call_thread.is_alive(), f"tool_describe answered mid-reload: {call_box}"

    gate.release.set()
    reload_thread.join(20)
    call_thread.join(20)
    assert TOOL in call_box.get("value", ""), call_box
    assert "not a known tool name" not in call_box["value"]
    assert gate.calls == []


def test_failed_reload_releases_the_call_with_the_real_refusal(manager, gate):
    """A plugin that does not come back is genuinely gone: the call is released and refused, not run."""
    gate.fail = True
    reload_thread, _ = _reload_held_open(manager, gate)
    call_thread, call_box = _start(_tool_call)

    gate.release.set()
    reload_thread.join(20)
    call_thread.join(20)
    assert not call_thread.is_alive()
    assert "error" in call_box.get("value", {}), call_box
    assert gate.calls == []
    assert not registry_reload.reload_active()


def test_reload_of_one_home_does_not_hold_another_homes_calls(manager, gate, tmp_path):
    reload_thread, _ = _reload_held_open(manager, gate)
    other = tmp_path / "other_home"
    other.mkdir()

    def from_other_home():
        token = set_hermes_home_override(Path(other))
        try:
            return registry_reload.wait_for_reload(), registry.dispatch(TOOL, {"host": "x"})
        finally:
            reset_hermes_home_override(token)

    call_thread, call_box = _start(from_other_home)
    call_thread.join(20)
    assert not call_thread.is_alive(), "another home's call waited on this home's reload"
    waited, result = call_box["value"]
    assert waited is False
    assert "Unknown tool" in result  # the other home never had the plugin
    gate.release.set()
    reload_thread.join(20)


def test_reloading_context_and_its_workers_never_wait():
    seen = {}
    with registry_reload.reload_barrier():
        assert registry_reload.reload_active()
        seen["owner"] = registry_reload.wait_for_reload(timeout=5)
        import contextvars
        worker = threading.Thread(target=contextvars.copy_context().run, args=(
            lambda: seen.setdefault("worker", registry_reload.wait_for_reload(timeout=5)),))
        worker.start()
        worker.join(10)
    assert seen == {"owner": False, "worker": False}
    assert not registry_reload.reload_active()


def test_wait_is_bounded_and_overlap_is_reported():
    epoch = registry_reload.reload_epoch()
    entered, release = threading.Event(), threading.Event()

    def hold():
        with registry_reload.reload_barrier():
            entered.set()
            release.wait(20)

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    assert entered.wait(10)
    try:
        assert registry_reload.wait_for_reload(timeout=0.05) is True  # gave up, did not hang
    finally:
        release.set()
        holder.join(10)
    assert registry_reload.settle_reload_since(epoch) is True   # a pass ran since the epoch was read
    assert registry_reload.settle_reload_since(registry_reload.reload_epoch()) is False
    with registry_reload.reload_barrier(False):
        assert not registry_reload.reload_active()
