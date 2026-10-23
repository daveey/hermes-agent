"""Every way an agent can create a Kanban card links it to the calling chat. [daveey]

The chat's task strip and sidebar badge list exactly ``session_tasks(<lineage ids>)``.
A card that misses the chat there is invisible while the chat says it is running.
Each entry path got the link separately (tool; CLI from the terminal in 0067/0068;
CLI from execute_code in 0111) and the execute_code path shipped broken while its
doc claimed it worked. This test drives all of them with the env each tool really
builds, so a new or regressed path fails here instead of in a user's chat.
Add a case here whenever a new way to create cards appears.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pytest

import gateway.session_context as sc
from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_session_activity as ksa

SID = "20261016_185414_c283c2"
_SESSION_ENV = ("HERMES_SESSION_KEY", "HERMES_SESSION_ID", "HERMES_SESSION_SOURCE",
                "HERMES_SESSION_PLATFORM", "HERMES_SESSION_CHAT_ID", "HERMES_RPC_SOCKET",
                "HERMES_KERNEL_SENTINEL", "HERMES_KANBAN_TASK")


@pytest.fixture
def chat(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in _SESSION_ENV:
        monkeypatch.delenv(var, raising=False)
    ksa._reset_cache_for_tests()
    kb.init_db()
    from hermes_state import SessionDB
    db = SessionDB(db_path=home / "state.db")
    try:
        db.create_session(session_id=SID, source="desktop")
    finally:
        db.close()
    tokens = sc.set_session_vars(session_id=SID, session_key=SID, source="desktop")
    yield home
    sc.clear_session_vars(tokens)


def _cli_create(title: str) -> str:
    root = argparse.ArgumentParser(prog="hermes")
    kc.build_parser(root.add_subparsers())
    assert kc._cmd_create(root.parse_args(["kanban", "create", title])) == 0
    with kbc.connect_closing() as conn:
        return next(t for t in kb.list_tasks(conn) if t.title == title).id


def _run_cli_in(env: dict, title: str, monkeypatch) -> str:
    """Run the CLI as the child process would: os.environ = the env the tool built.
    The CLI is a separate process, so the parent's ContextVars must not leak in."""
    for var in _SESSION_ENV:
        monkeypatch.delenv(var, raising=False)
    for name, value in env.items():
        if name.startswith("HERMES_SESSION_") or name in ("HERMES_RPC_SOCKET", "HERMES_KERNEL_SENTINEL"):
            monkeypatch.setenv(name, value)
    tokens = [var.set(sc._UNSET) for var in sc._VAR_MAP.values()]
    engaged = sc._session_context_engaged
    sc._session_context_engaged = False
    try:
        return _cli_create(title)
    finally:
        sc._session_context_engaged = engaged
        for var, tok in zip(sc._VAR_MAP.values(), tokens):
            var.reset(tok)


def _via_tool(title: str, monkeypatch) -> str:
    from tools.kanban_tools import _handle_create
    out = json.loads(_handle_create({"title": title, "assignee": "default"}))
    return out["task_id"]


def _via_terminal(title: str, monkeypatch) -> str:
    from tools.environments.local import _inject_session_context_env
    env: dict = {}
    _inject_session_context_env(env)
    return _run_cli_in(env, title, monkeypatch)


def _via_execute_code(title: str, monkeypatch) -> str:
    from tools.code_execution_env import _build_child_env
    env = _build_child_env(rpc_endpoint="sock", rpc_token="tok", tmpdir="/tmp/hermes-test",
                           child_python=sys.executable)
    env.setdefault("HERMES_RPC_SOCKET", "sock")
    return _run_cli_in(env, title, monkeypatch)


def _via_origin_courier(title: str, monkeypatch) -> str:
    import io
    from hermes_constants import get_hermes_home
    from tools import kanban_tools
    home = get_hermes_home()
    cfg = home / 'mail.json'
    cfg.write_text(json.dumps({'gateway': 'owner'}))
    monkeypatch.setenv('HERMES_MAIL_CONFIG', str(cfg))
    monkeypatch.setenv('HERMES_HUB_STATE', str(home / 'hub'))
    monkeypatch.setattr(kanban_tools, '_profile_has_kanban_toolset', lambda: True)
    request = {'id': 'request-parity-001', 'from': 'gw:origin', 'to_gw': 'owner',
               'coll': 'tasks', 'key': 'default/default', 'action': 'create',
               'args': {'origin': {'gateway': 'origin', 'profile': 'default', 'session': 'original'},
                        'create': {'title': title, 'assignee': 'default', 'session_id': SID}}}
    monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps(request)))
    root = argparse.ArgumentParser(prog='hermes')
    kc.build_parser(root.add_subparsers())
    assert kc.kanban_command(root.parse_args(['kanban', 'origin-create', '--request-file', '-'])) == 0
    with kbc.connect_closing() as conn:
        return next(t for t in kb.list_tasks(conn) if t.title == title).id


PATHS = {"origin courier CLI": _via_origin_courier,
         "kanban_create tool": _via_tool, "terminal: hermes kanban create": _via_terminal,
         "execute_code: hermes kanban create": _via_execute_code}


@pytest.mark.parametrize("path", list(PATHS))
def test_card_shows_in_the_creating_chats_task_list(chat, monkeypatch, capsys, path):
    task_id = PATHS[path](f"card via {path}", monkeypatch)
    ksa._reset_cache_for_tests()
    listed = {t["id"] for t in ksa.session_tasks([SID])}
    assert task_id in listed, f"{path}: card {task_id} is not in chat {SID}'s task list"
    assert "not linked to any chat" not in capsys.readouterr().err


def test_agent_cli_create_without_a_known_chat_warns(chat, monkeypatch, capsys):
    # e.g. execute_code on another host / another profile: agent env, but no known session.
    task_id = _run_cli_in({"HERMES_RPC_SOCKET": "sock", "HERMES_SESSION_ID": "not_a_real_session"},
                          "orphan", monkeypatch)
    err = capsys.readouterr().err
    assert f"{task_id} is not linked to any chat" in err
    assert f"notify-subscribe {task_id} --platform tui --chat-id" in err


def test_plain_user_shell_create_does_not_warn(chat, monkeypatch, capsys):
    _run_cli_in({}, "from my own shell", monkeypatch)
    assert "not linked to any chat" not in capsys.readouterr().err


def test_suggested_notify_subscribe_links_the_orphan(chat, monkeypatch, capsys):
    task_id = _run_cli_in({"HERMES_RPC_SOCKET": "sock"}, "orphan to fix", monkeypatch)
    root = argparse.ArgumentParser(prog="hermes")
    kc.build_parser(root.add_subparsers())
    args = root.parse_args(["kanban", "notify-subscribe", task_id, "--platform", "tui", "--chat-id", SID])
    assert kc._cmd_notify_subscribe(args) == 0
    ksa._reset_cache_for_tests()
    assert task_id in {t["id"] for t in ksa.session_tasks([SID])}
