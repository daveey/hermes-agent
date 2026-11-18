"""Foreground launch custody and no-retry boundaries."""
import json
import sys
import shlex
import os
from types import SimpleNamespace

import pytest

from tests.hermes_cli import test_origin_native_admission as admission_fixtures

turn = admission_fixtures.turn


@pytest.mark.parametrize("missing", ["platform", "seqpacket", "cmsg", "poll"])
def test_supported_requires_each_linux_primitive(monkeypatch, missing):
    from tools import origin_cli_terminal as boundary
    monkeypatch.setattr(boundary, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(boundary, "socket", SimpleNamespace(SOCK_SEQPACKET=5, MSG_CMSG_CLOEXEC=1))
    monkeypatch.setattr(boundary, "select", SimpleNamespace(poll=object()))
    assert boundary.supported()
    if missing == "platform":
        boundary.sys.platform = "unsupported"
    elif missing == "poll":
        del boundary.select.poll
    else:
        delattr(boundary.socket, "SOCK_SEQPACKET" if missing == "seqpacket" else "MSG_CMSG_CLOEXEC")
    assert not boundary.supported()


@pytest.mark.parametrize("missing", ["policy", "delegated"])
def test_offer_does_not_inherit_absent_or_delegated_policy(turn, monkeypatch, missing):
    from tools import origin_cli_terminal as boundary
    from tools.environments.local import LocalEnvironment
    monkeypatch.setattr(boundary, "supported", lambda: True)
    monkeypatch.setattr(boundary, "current_policy", lambda: None if missing == "policy" else object())
    monkeypatch.setattr("agent.delegation_context.is_delegated_child_context", lambda: missing == "delegated")
    def forbidden(*args):
        pytest.fail("ineligible caller reached Launch")
    monkeypatch.setattr(boundary, "Launch", forbidden)
    assert boundary.offer(object.__new__(LocalEnvironment), "local", None, "x") is None


def test_offered_attempt_remains_marked_after_return(monkeypatch):
    from tools import origin_cli_terminal as boundary
    launch = SimpleNamespace(retire=list, close=lambda: None)
    monkeypatch.setattr(boundary, "offer", lambda *a, **kw: launch)
    attempt = []
    result = boundary.execute(SimpleNamespace(execute=lambda *a, **kw: {"output": "ok"}), "cmd",
                              env_type="local", task_id=None, attempt=attempt)
    assert result == {"output": "ok"}
    assert attempt == [True]


def test_launch_executes_real_local_shell(turn, monkeypatch, tmp_path):
    from tools import origin_cli_terminal as boundary
    from tools.environments.local import LocalEnvironment
    from tools.origin_cli_bridge import ENV_KEY
    from tests.tools.test_origin_cli_bridge import policy_launch
    launch = policy_launch(monkeypatch, "test")
    monkeypatch.setattr(boundary, "offer", lambda *a, **k: launch)
    env = LocalEnvironment(cwd=str(tmp_path), persistent=False)
    try:
        result = boundary.execute(env, f"printf '%s' \"${ENV_KEY}\"", env_type="local", task_id="test",
                                  timeout=5, cwd=str(tmp_path))
        assert json.loads(result["output"].splitlines()[0])["nonce"] == json.loads(launch.locator)["nonce"]
        assert launch._closed
    finally:
        env.cleanup()
        launch.close()


def test_offer_requires_live_custody_and_supported_backend(turn, monkeypatch):
    from tools import origin_cli_terminal as boundary
    from tools.environments.local import LocalEnvironment
    monkeypatch.setattr(boundary, "current_policy", lambda: object())
    env = object.__new__(LocalEnvironment)
    assert boundary.offer(env, "ssh", None, None) is None
    assert boundary.offer(object(), "local", None, None) is None
    assert boundary.offer(env, "local", object(), None) is None
    monkeypatch.setattr(boundary, "supported", lambda: False)
    assert boundary.offer(env, "local", None, None) is None
    monkeypatch.setattr(boundary, "supported", lambda: True)
    launch = boundary.offer(env, "local", None, None)
    assert launch is not None
    launch.close()
    turn.release()
    assert boundary.offer(env, "local", None, None) is None


def test_execute_disables_yield_and_retires_on_failure(monkeypatch):
    from tools import origin_cli_terminal as boundary
    calls = []
    class Launch:
        def retire(self):
            calls.append("retire")
            return [{"outcome": "outcome_unknown", "task_id": "fixture"}]
        def close(self):
            calls.append("close")
    launch = Launch()
    monkeypatch.setattr(boundary, "offer", lambda *a, **k: launch)
    def fail(*a, **kw):
        assert "yield_handler" not in kw
        assert kw["origin_cli_launch"] is launch
        raise OSError("transient")
    with pytest.raises(boundary.NoRetry) as err:
        boundary.execute(SimpleNamespace(execute=fail), "cmd", env_type="local", task_id=None,
                         yield_handler=object())
    assert calls == ["retire", "close"]
    assert "outcome_unknown" in str(err.value)


def test_terminal_does_not_retry_offered_bridge(monkeypatch):
    from tools import terminal_tool as terminal
    from tools import origin_cli_terminal as boundary
    calls = []
    monkeypatch.setattr(terminal, "_resolve_command_cwd", lambda **k: "/")
    monkeypatch.setattr(terminal, "_yield_kwargs", lambda *a, **k: {})
    monkeypatch.setattr(terminal, "_survivable_foreground", lambda *a, **k: None)
    def fail(*a, **kw):
        calls.append(1)
        raise boundary.NoRetry("unknown; do not retry")
    monkeypatch.setattr(boundary, "execute", fail)
    plan = SimpleNamespace(env_type="local", effective_task_id="test", effective_timeout=5, cwd="/", host_cwd=None)
    result = json.loads(terminal._run_foreground("true", object(), plan, task_id=None, session_id=None,
                      session_key="test", cwd_key="test", workdir=None, approval_note=None,
                      clear_interrupt=False, metered=False))
    assert calls == [1]
    assert "do not retry" in result["error"]


@pytest.mark.parametrize("revoke", [False, True])
def test_terminal_cli_real_dispatch_receipt(turn, monkeypatch, tmp_path, revoke):
    import model_tools
    from hermes_cli import lifecycle, kanban_db_connect as kbc, kanban_origin_native as native
    from tools import terminal_tool as terminal, kanban_tools
    from tools.environments.local import LocalEnvironment
    from tools.registry import registry
    from tests.hermes_cli import test_origin_native_admission as fixtures
    old = registry._tools.copy()
    events = []
    monkeypatch.setattr(lifecycle, "has_hook", lambda name: name in {"pre_tool_call", "post_tool_call"})
    monkeypatch.setattr(lifecycle, "invoke_hook", lambda name, **kw: events.append((name, kw)) or [])
    monkeypatch.setattr(model_tools, "_select_tool_names", lambda *a, **kw: {"kanban_create"})
    registry.register(name="kanban_create", toolset="kanban", schema=kanban_tools.KANBAN_CREATE_SCHEMA,
                      handler=lambda args, **kw: kanban_tools._handle_create(args))
    env = LocalEnvironment(cwd=os.getcwd(), persistent=True, env={"PYTHONPATH": os.getcwd()})
    plan = SimpleNamespace(env_type="local", effective_task_id="test", effective_timeout=20,
                           cwd=os.getcwd(), host_cwd=None)
    monkeypatch.setattr(terminal, "_survivable_foreground", lambda *a, **kw: None)
    command = shlex.join([sys.executable, "-m", "hermes_cli.main", "kanban", "create", "composed child",
                          "--assignee", "default", "--no-requires", "--json"])
    if revoke:
        from tools import origin_cli_terminal as boundary
        original_offer = boundary.offer
        def revoke_after_offer(*a, **kw):
            launch = original_offer(*a, **kw)
            assert launch is not None
            turn.release()
            return launch
        monkeypatch.setattr(boundary, "offer", revoke_after_offer)
    def run(args, **kw):
        return terminal._run_foreground(command, env, plan, task_id="test", session_id="parent",
                        session_key="test", cwd_key="test", workdir=os.getcwd(), approval_note=None,
                        clear_interrupt=False, metered=False)
    registry.register(name="origin_terminal_probe", toolset="terminal",
                      schema={"name": "origin_terminal_probe", "parameters": {}}, handler=run)
    try:
        result = json.loads(model_tools.handle_function_call("origin_terminal_probe", {},
                    enabled_tools=["terminal", "kanban_create"], session_id="parent"))
        # Revoke after offer, not before: otherwise ordinary legacy CLI is intended.
        if revoke:
            assert result["exit_code"] == 2, result
            with kbc.connect_closing() as conn:
                assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
        else:
            assert result["exit_code"] == 0, result
            value = json.loads(result["output"].splitlines()[0])
            assert value["ok"], value
            assert "Native CLI admission" in result["output"]
            with kbc.connect_closing() as conn:
                assert native._receipt(conn, value["task_id"])["origin"]["session"] == fixtures.fixtures.CHAT
            names = [(name, kw.get("tool_name")) for name, kw in events]
            assert ("pre_tool_call", "kanban_create") in names
            assert ("post_tool_call", "kanban_create") in names
        # A subsequent command must never inherit the retired endpoint via snapshot.
        next_result = env.execute("printf '%s' \"${HERMES_ORIGIN_CLI_BRIDGE-unset}\"", timeout=5)
        assert next_result["output"].strip() == "unset"
    finally:
        env.cleanup()
        registry._tools.clear()
        registry._tools.update(old)
