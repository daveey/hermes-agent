"""A durable start may pin a closed toolset allowlist that every later agent build keeps."""
import hashlib
import json
import types

import pytest

from hermes_state import SessionDB
from tui_gateway import server
from tui_gateway.session_start_requests import StartIntent, StartOwner, StartStore

OWNER = StartOwner("example-gateway", "default", "example:user")
ALLOW = ["example_ops", "clarify", "todo"]


@pytest.fixture(autouse=True)
def _plugin_toolset(monkeypatch):
    """Stands in for a plugin-registered toolset such as runners."""
    import toolsets
    monkeypatch.setitem(toolsets.TOOLSETS, "example_ops", toolsets._ts("Example plugin tools", [], []))


@pytest.fixture
def store(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    yield StartStore(db)
    db.close()


def accept(store, toolsets=None, request_id="request"):
    return store.accept(owner=OWNER, namespace="example", request_id=request_id,
                        intent=StartIntent("Title", "Inspect only", toolsets=toolsets))


# ── validation ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", [[], "terminal", ["nope-not-a-toolset"], ["all"], ["*"], ["clarify", 3],
                                 ["clarify", "clarify"], [""], ["x"] * 65, {"clarify": True}])
def test_toolsets_validation_rejects_unknown_or_unclosed(bad):
    with pytest.raises(ValueError, match="toolsets"):
        StartIntent("Title", "Inspect", toolsets=bad)


def test_toolsets_are_normalized_immutable_and_independent_of_caller():
    supplied = ["todo", "clarify"]
    intent = StartIntent("Title", "Inspect", toolsets=supplied)
    supplied.append("terminal")
    assert intent.toolsets == ("clarify", "todo")


def test_absent_toolsets_keep_old_digest_and_behaviour(store):
    row = accept(store)
    old = hashlib.sha256(json.dumps(dict(title="Title", text="Inspect only", source="desktop"),
                                    sort_keys=True, ensure_ascii=True).encode()).hexdigest()
    assert row["intent_digest"] == old
    assert StartIntent("t", "x").toolsets is None
    assert store.session_toolsets(row["stored_session_id"]) is None
    assert store.status(OWNER, "example", "request")["toolsets"] == []


# ── persistence ─────────────────────────────────────────────────────────────

def test_toolsets_durable_idempotent_and_conflict(store):
    row = accept(store, ALLOW)
    assert accept(store, list(reversed(ALLOW))) == row
    with pytest.raises(ValueError, match="conflict"):
        accept(store, ["clarify"])
    with pytest.raises(ValueError, match="conflict"):
        accept(store, None)
    assert store.status(OWNER, "example", "request")["toolsets"] == sorted(ALLOW)


def test_pin_follows_compression_branch_and_reopen(store):
    sid = accept(store, ALLOW)["stored_session_id"]
    assert store.session_toolsets(sid) == tuple(sorted(ALLOW))
    store.db.end_session(sid, "compression")
    store.db.create_session("child", source="desktop", parent_session_id=sid)
    store.db.create_session("grandchild", source="desktop", parent_session_id="child")
    with SessionDB(store.db.db_path) as reopened:
        fresh = StartStore(reopened)
        assert fresh.session_toolsets("grandchild") == tuple(sorted(ALLOW))
        assert fresh.session_toolsets("child") == tuple(sorted(ALLOW))


def test_unrelated_session_and_old_ledger_have_no_pin(store):
    accept(store, ALLOW)
    store.db.create_session("other", source="desktop")
    assert store.session_toolsets("other") is None
    assert store.session_toolsets("missing") is None


def test_pin_lookup_without_ledger_table_is_unpinned(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    try:
        db.create_session("plain", source="desktop")
        assert StartStore.session_toolsets_for(db, "plain") is None
    finally:
        db.close()


def test_upgrade_existing_ledger_adds_column(store):
    row = accept(store)
    store.db._execute_write(lambda conn: conn.execute(
        "ALTER TABLE session_start_requests DROP COLUMN toolsets_json"))
    upgraded = StartStore(store.db)
    assert accept(upgraded)["stored_session_id"] == row["stored_session_id"]
    assert upgraded.session_toolsets(row["stored_session_id"]) is None


# ── enforcement at agent build ──────────────────────────────────────────────

def _pinned_session(store):
    sid = accept(store, ALLOW)["stored_session_id"]
    store.db.end_session(sid, "compression")
    store.db.create_session("compressed-tip", source="desktop", parent_session_id=sid)
    return sid


def test_pinned_tool_surface_has_no_terminal_execute_code_or_delegate(store):
    import model_tools
    from tools import tool_search
    pin = list(store.session_toolsets(_pinned_session(store)))
    listed = {t["function"]["name"] for t in model_tools.get_tool_definitions(enabled_toolsets=pin, quiet_mode=True)}
    scoped = model_tools.get_tool_definitions(enabled_toolsets=pin, quiet_mode=True, skip_tool_search_assembly=True)
    surface = {t["function"]["name"] for t in scoped}
    assert "clarify" in listed and surface == {"clarify", "todo_list"}
    for forbidden in ("terminal", "execute_code", "delegate_task", "read_file", "write_file", "patch",
                      "kanban_create", "cronjob_manage", "cross_session", "browser_exec"):
        assert forbidden not in listed | surface
        assert forbidden not in tool_search.scoped_deferrable_names(scoped)
        # The tool_search bridge is scoped to the same set: nothing outside it is re-dispatched.
        _, redispatch = model_tools._dispatch_bridge_tool(
            "tool_call", {"name": forbidden, "arguments": {}}, pin, None)
        assert redispatch is None


def test_agent_loop_rejects_names_outside_the_pinned_surface():
    from agent.agent_runtime_helpers import repair_tool_call
    agent = types.SimpleNamespace(valid_tool_names={"clarify", "tool_search", "tool_call", "tool_describe"})
    for name in ("terminal", "execute_code", "delegate_task", "Terminal", "run_terminal"):
        assert repair_tool_call(agent, name) is None


class _FakeAgent:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.enabled_toolsets = kwargs["enabled_toolsets"]
        self.disabled_toolsets = kwargs["disabled_toolsets"]
        self.platform = kwargs["platform"]


def _build(monkeypatch, store, key, wide=("terminal", "file", "code_execution")):
    import run_agent
    monkeypatch.setattr(run_agent, "AIAgent", _FakeAgent)
    monkeypatch.setattr(server, "_load_enabled_toolsets", lambda platform=None: list(wide))
    monkeypatch.setattr(server, "_agent_cbs", lambda sid: {})
    monkeypatch.setattr(server, "_startup_system_prompt", lambda cfg, key: "")
    monkeypatch.setattr(server, "_resolve_agent_model_runtime", lambda m, p, session=None: ("model", {}))
    monkeypatch.setattr(server, "_load_cfg", dict)
    monkeypatch.setattr(server, "_resolve_agent_platform", lambda p=None: "desktop")
    return server._make_agent("sid", key, session_id=key, session_db=store.db)


def test_make_agent_uses_only_the_pin_for_original_and_compressed_sessions(monkeypatch, store):
    sid = _pinned_session(store)
    for key in (sid, "compressed-tip"):
        agent = _build(monkeypatch, store, key)
        assert list(agent.kwargs["enabled_toolsets"]) == sorted(ALLOW)
        assert agent._toolset_pin == tuple(sorted(ALLOW))


def test_make_agent_without_pin_is_unchanged(monkeypatch, store):
    store.db.create_session("normal", source="desktop")
    agent = _build(monkeypatch, store, "normal")
    assert agent.kwargs["enabled_toolsets"] == ["terminal", "file", "code_execution"]
    assert getattr(agent, "_toolset_pin", None) is None


def test_no_session_store_means_no_pin_and_the_build_proceeds(monkeypatch, store):
    # _get_db() returns None when state.db cannot open; the gateway keeps running without
    # state.db features, so _make_agent must not crash on the pin lookup.
    assert StartStore.session_toolsets_for(None, "any") is None
    import run_agent
    monkeypatch.setattr(run_agent, "AIAgent", _FakeAgent)
    monkeypatch.setattr(server, "_get_db", lambda: None)
    monkeypatch.setattr(server, "_load_enabled_toolsets", lambda platform=None: ["terminal"])
    monkeypatch.setattr(server, "_agent_cbs", lambda sid: {})
    monkeypatch.setattr(server, "_startup_system_prompt", lambda cfg, key: "")
    monkeypatch.setattr(server, "_resolve_agent_model_runtime", lambda m, p, session=None: ("model", {}))
    monkeypatch.setattr(server, "_load_cfg", dict)
    monkeypatch.setattr(server, "_resolve_agent_platform", lambda p=None: "desktop")
    agent = server._make_agent("sid", "no-db")
    assert agent.kwargs["enabled_toolsets"] == ["terminal"]
    assert getattr(agent, "_toolset_pin", None) is None


def test_pin_lookup_failure_refuses_the_build(monkeypatch, store):
    sid = _pinned_session(store)
    def boom(*a, **kw):
        raise RuntimeError("db gone")
    monkeypatch.setattr(StartStore, "session_toolsets_for", staticmethod(boom))
    with pytest.raises(RuntimeError):
        _build(monkeypatch, store, sid)


def test_refresh_cannot_widen_a_pinned_agent():
    from tools.mcp_tool_agent import _resolve_refresh_toolsets
    agent = types.SimpleNamespace(enabled_toolsets=["clarify", "todo"], disabled_toolsets=None, platform="desktop",
                                  _toolset_pin=("clarify", "todo"))
    enabled, _ = _resolve_refresh_toolsets(agent, ["terminal", "file"], [])
    assert enabled == ["clarify", "todo"]
    assert agent.enabled_toolsets == ["clarify", "todo"]


def test_refresh_without_pin_still_takes_override():
    from tools.mcp_tool_agent import _resolve_refresh_toolsets
    agent = types.SimpleNamespace(enabled_toolsets=["clarify"], disabled_toolsets=None, platform="desktop")
    enabled, _ = _resolve_refresh_toolsets(agent, ["terminal"], [])
    assert enabled == ["terminal"]
