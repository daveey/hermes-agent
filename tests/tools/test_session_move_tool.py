"""``session_move``: the agent tool starts ``hermes sessions move ... --after-turn`` detached. [daveey]"""
from __future__ import annotations

import json
import subprocess

import pytest

from hermes_cli import session_handover as sh
from tools import session_move_tool as smt


@pytest.fixture
def hosts(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(sh, "move_hosts", lambda cfg=None: {"hub": "ubuntu@hub-host"})
    monkeypatch.setattr(smt, "_current_session_id", lambda: "20261028_120000_aaaaaa")


def test_the_tool_spawns_the_after_turn_move_detached(hosts, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    started = []

    class _Proc:
        pid = 4242

    def popen(argv, **kw):
        started.append((argv, kw))
        return _Proc()

    monkeypatch.setattr(subprocess, "Popen", popen)
    out = json.loads(smt.session_move("hub", "needs the hub's GPU"))
    assert out == {"ok": True, "session_id": "20261028_120000_aaaaaa", "to": "hub", "pid": 4242,
                   "message": "moving after this turn; reopen the chat on hub"}
    [(argv, kw)] = started
    assert argv[1:] == ["-m", "hermes_cli.main", "sessions", "move", "20261028_120000_aaaaaa", "--to", "hub",
                        "--reason", "needs the hub's GPU", "--after-turn"]
    assert kw["start_new_session"] is True and kw["stdin"] is subprocess.DEVNULL
    assert (tmp_path / "logs" / "session-move-20261028_120000_aaaaaa.log").exists()


def test_the_tool_refuses_a_target_not_in_sessions_move_hosts(hosts, monkeypatch):
    monkeypatch.setattr(sh, "spawn_after_turn_move", lambda *a: pytest.fail("must not spawn"))
    out = json.loads(smt.session_move("linux-2", "x"))
    assert out["code"] == "unknown_host" and "linux-2" in out["error"]
    assert json.loads(smt.session_move("hub", " "))["code"] == "reason_required"


def test_the_tool_is_offered_to_chats_with_hosts_only_never_to_kanban_workers(hosts, monkeypatch):
    assert smt.check_session_move_requirements() is True
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_123")
    assert smt.check_session_move_requirements() is False
    assert "error" in json.loads(smt.session_move("hub", "x"))
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    monkeypatch.setattr(sh, "move_hosts", lambda cfg=None: {})
    assert smt.check_session_move_requirements() is False


def test_the_tool_is_registered_in_the_session_move_toolset():
    from tools.registry import registry
    import toolsets
    entry = registry.get_entry("session_move") if hasattr(registry, "get_entry") else registry._tools["session_move"]
    assert entry.toolset == "session_move"
    assert "session_move" in toolsets.TOOLSETS["session_move"]["tools"]
    assert "session_move" not in toolsets.TOOLSETS["kanban"]["tools"]
