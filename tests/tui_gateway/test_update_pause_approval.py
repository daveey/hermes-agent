"""Approval preparation can block before a tool is registered in flight."""
import threading

import pytest

from agent import update_pause_cancel
from tests.tui_gateway import test_update_pause_clarify as helpers
from tools import approval
from tools.approval_gateway_wait import _await_gateway_decision
from tui_gateway import update_pause as up
from tui_gateway.turn_marker import record_turn_start

env = helpers.env


def test_real_desktop_batch_pauses_without_executing_unapproved_commands(tmp_path, monkeypatch):
    from tests.tui_gateway import test_update_pause_mid_tool as mid
    from tests.agent import test_terminal_batch_timeout_continues as batch
    from gateway.session_context import clear_session_vars
    from tools.terminal_scope import reset_terminal_scope, set_terminal_scope
    from tools.terminal_tool_lifecycle import cleanup_vm
    from tui_gateway import server

    key = "session-key"
    monkeypatch.setattr(server, "_hermes_home", tmp_path)
    monkeypatch.setattr(server, "_load_cfg", lambda: helpers.CFG)
    monkeypatch.setattr(up, "MID_TOOL_POLL_S", 0.05)
    monkeypatch.setattr("agent.title_generator.maybe_auto_title", lambda *a, **k: None)
    monkeypatch.setattr("tools.approval_context._get_approval_mode", lambda: "manual")
    monkeypatch.setattr("tools.approval_context._get_approval_timeout", lambda: 5)
    monkeypatch.delenv("HERMES_DESKTOP", raising=False)
    monkeypatch.delenv("HERMES_SINGLE_QUERY", raising=False)
    monkeypatch.setenv("HERMES_GATEWAY_SESSION", "1")
    monkeypatch.setenv("HERMES_EXEC_ASK", "1")
    monkeypatch.setattr(approval, "_yolo_active", lambda: False)
    monkeypatch.setattr(approval, "_command_findings", lambda command: [("probe", "test consent")])
    agent = mid._make_agent(tmp_path, "terminal")
    session = mid._session(agent, tmp_path)
    session["source"] = "desktop"
    monkeypatch.setattr(server, "_sessions", {key: session})
    tokens = server._set_session_context(key)
    from tools.approval_context import set_current_session_key, reset_current_session_key
    approval_token = set_current_session_key(key)
    scope = set_terminal_scope({"TERMINAL_ENV": "local", "TERMINAL_CWD": str(tmp_path)})
    seen = []

    def notify(data):
        seen.append(update_pause_cancel.in_flight(agent))
        helpers._request_pause(tmp_path)

    approval.register_gateway_notify(key, notify)
    record_turn_start(tmp_path, key, "probe")
    paths = [tmp_path / "first", tmp_path / "second"]
    # A harmless command whose chmod shape requires consent; no command may run.
    calls = [batch._call(f"c{i}", f"touch '{p}'; chmod 777 '{p}'") for i, p in enumerate(paths)]
    try:
        result = mid._run_turn(agent, session, [mid._response(*calls), mid._response()])
        assert seen and seen[0] == {}, "must exercise preparation before in-flight registration"
        assert result.get("paused_for_update") == up.PAUSE_REASON
        assert not any(p.exists() for p in paths)
        rows = mid._durable_tools(tmp_path)
        assert set(rows) == {"c0", "c1"}
        assert all("update" in row for row in rows.values())
        assert approval.list_gateway_approvals(key) == []
        assert agent.client.chat.completions.create.call_count == 1
    finally:
        approval.unregister_gateway_notify(key)
        reset_terminal_scope(scope)
        cleanup_vm(key)
        reset_current_session_key(approval_token)
        clear_session_vars(tokens)
        agent.close()


def _pending(monkeypatch, key):
    monkeypatch.setattr("tools.approval_context._get_approval_timeout", lambda: 30)
    published = threading.Event()
    box = {}
    settled = []

    def notify(data):
        box["request_id"] = data["request_id"]
        approval.register_gateway_settle(key, data["request_id"], settled.append)
        published.set()

    def wait():
        box["result"] = _await_gateway_decision(key, notify, {"command": "probe", "pattern_key": "probe"})

    thread = threading.Thread(target=wait)
    thread.start()
    assert published.wait(10)
    return thread, box, settled


def _cleanup(thread, box, key):
    approval.withdraw_gateway_approval(key, box["request_id"], "test cleanup")
    thread.join(10)
    assert not thread.is_alive()


def test_pause_withdraws_approval_even_before_execution_starts(env, monkeypatch):
    key = "session-key"
    thread, box, settled = _pending(monkeypatch, key)
    try:
        assert update_pause_cancel.in_flight(env.session["agent"]) == {}
        helpers._request_pause(env.home)
        up.mid_tool_cancel_step(env.session, key, env.session["agent"], {})
        thread.join(2)
        assert not thread.is_alive(), "pre-execution approval still blocks update pause"
        result = box["result"]
        assert result["choice"] is None
        assert "update" in result["cancelled"] and "again" in result["cancelled"]
        assert settled == ["session_closed"]
        assert approval.list_gateway_approvals(key) == []
        assert approval.resolve_gateway_approval(key, "once", request_id=box["request_id"]) == 0
        # A continuation must publish a new, unanswered request, not inherit consent.
        from hermes_cli import admission_fence
        admission_fence.clear_pause_request(env.home)
        again, new_box, new_settled = _pending(monkeypatch, key)
        try:
            up.mid_tool_cancel_step(env.session, key, env.session["agent"], {})
            assert new_box["request_id"] != box["request_id"]
            assert approval.has_blocking_approval(key) and new_settled == []
        finally:
            _cleanup(again, new_box, key)
    finally:
        _cleanup(thread, box, key)


@pytest.mark.parametrize("case", ["no_pause", "no_hook", "opted_out", "other_session", "stop"])
def test_nonpausing_turn_keeps_pending_approval(env, monkeypatch, case):
    key = "other-session" if case == "other_session" else "session-key"
    thread, box, settled = _pending(monkeypatch, key)
    try:
        if case != "no_pause":
            helpers._request_pause(env.home)
        if case == "no_hook":
            env.session["agent"].step_boundary_pause = None
        if case == "opted_out":
            record_turn_start(env.home, "session-key", "prompt", auto_continue=False)
        if case == "stop":
            env.session["agent"]._interrupt_requested = True
        up.mid_tool_cancel_step(env.session, "session-key", env.session["agent"], {})
        assert approval.has_blocking_approval(key)
        assert settled == []
    finally:
        _cleanup(thread, box, key)
