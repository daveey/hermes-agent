"""A pick whose unlocked ``running`` check lost to a turn admission is queued, not dropped or applied.

``config.set model`` and the ``/model`` slash mirror read ``session["running"]`` without a lock. The
agent's switch refuses to publish onto a turn admitted after its snapshot (``TurnAdmittedDuringSwitch``);
the gateway then stashes the pick for the next turn start through the busy path's own stash.
"""

import threading
import types

import pytest

from agent.agent_runtime_switch import TurnAdmittedDuringSwitch
from tui_gateway import server

TARGET = "claude-target"


def _refuse(*_args, **_kwargs):
    raise TurnAdmittedDuringSwitch("a turn started while the model switch resolved")


@pytest.fixture
def idle_session(monkeypatch):
    """An idle-looking session whose agent's switch loses the race to a turn."""
    result = types.SimpleNamespace(
        success=True, new_model=TARGET, target_provider="anthropic", base_url="", api_key="", api_mode="",
        warning_message="", error_message="", runtime_capabilities=None, usage_notice="")
    guard = types.SimpleNamespace(warning_message="")
    monkeypatch.setattr(server, "_current_model_runtime", lambda *_a, **_k: ("anthropic", "old", "", ""))
    monkeypatch.setattr(server, "_preflight_model_switch", lambda *_a, **_k: (result, guard, {}, {}))
    monkeypatch.setattr(server, "_merge_preflight_warning", lambda *_a, **_k: None)
    monkeypatch.setattr(server, "_expensive_model_confirm", lambda *_a, **_k: None)
    monkeypatch.setattr(server, "_restart_slash_worker", lambda *_a, **_k: pytest.fail("restarted after refusal"))
    agent = types.SimpleNamespace(
        model="old", provider="anthropic", session_id="s", _one_turn_runtime=None, switch_model=_refuse)
    session = {"agent": agent, "session_key": "k", "history": [], "history_lock": threading.Lock(),
               "running": False, "slash_worker": None}
    server._sessions["sid"] = session
    try:
        yield session
    finally:
        server._sessions.pop("sid", None)


def test_config_set_queues_the_pick(idle_session):
    resp = server.handle_request({"id": "1", "method": "config.set",
                                  "params": {"session_id": "sid", "key": "model", "value": TARGET}})
    assert not resp.get("error"), resp
    assert resp["result"]["deferred"] is True and resp["result"]["value"] == TARGET
    assert idle_session["pending_model_switch"]["raw"] == TARGET
    assert "model_override" not in idle_session  # nothing was recorded as applied


def test_slash_mirror_queues_the_pick(idle_session):
    note = server._mirror_slash_side_effects("sid", idle_session, f"/model {TARGET}")
    assert "next turn" in note
    assert idle_session["pending_model_switch"]["raw"] == TARGET
    assert "model_override" not in idle_session
