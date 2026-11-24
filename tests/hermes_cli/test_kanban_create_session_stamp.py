"""`hermes kanban create` run from an agent's terminal stamps the calling session.

Agents often create cards by shelling out to the CLI (terminal / execute_code)
instead of the ``kanban_create`` tool. Without a ``session_id`` those cards never
show in the Desktop session's task list, so the chat says "N cards running"
while its Tasks view is empty. The CLI now mirrors the tool: it takes
``HERMES_SESSION_ID`` from the environment when that session exists in this
profile's state.db, and never inside a dispatcher-owned worker (whose cards
inherit their creator task's session instead).
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc



@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    for var in ("HERMES_SESSION_KEY", "HERMES_SESSION_PLATFORM", "HERMES_SESSION_CHAT_ID"):
        monkeypatch.delenv(var, raising=False)
    from hermes_cli import kanban_session_activity as ksa
    ksa._reset_cache_for_tests()
    kb.init_db()
    return home


def _seed_session(home: Path, sid: str) -> None:
    from hermes_state import SessionDB

    db = SessionDB(db_path=home / "state.db")
    try:
        db.create_session(session_id=sid, source="desktop")
    finally:
        db.close()


def _create(title: str):
    root = argparse.ArgumentParser(prog="hermes")
    kc.build_parser(root.add_subparsers())
    assert kc._cmd_create(root.parse_args(["kanban", "create", title])) == 0
    with kbc.connect_closing() as conn:
        return next(t for t in kb.list_tasks(conn) if t.title == title).session_id


def test_cli_create_stamps_calling_session(kanban_home, monkeypatch, capsys):
    _seed_session(kanban_home, "20261014_141400_abc123")
    monkeypatch.setenv("HERMES_SESSION_ID", "20261014_141400_abc123")
    assert _create("from a chat terminal") == "20261014_141400_abc123"


def test_cli_create_ignores_unknown_session(kanban_home, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_SESSION_ID", "not_a_real_session")
    # An unregistered session is not stamped, and an agent create with no other authority is refused (req. D).
    root = argparse.ArgumentParser(prog="hermes")
    kc.build_parser(root.add_subparsers())
    assert kc._cmd_create(root.parse_args(["kanban", "create", "stray env"])) == 2
    with kbc.connect_closing() as conn:
        assert not [t for t in kb.list_tasks(conn) if t.title == "stray env"]


def test_cli_create_without_session_env_is_unstamped(kanban_home, capsys):
    assert _create("plain shell") is None


def test_cli_create_in_worker_does_not_stamp_worker_session(kanban_home, monkeypatch, capsys):
    _seed_session(kanban_home, "worker_sess")
    _seed_session(kanban_home, "owner_chat")  # the registered chat the creator descends from
    monkeypatch.setenv("HERMES_SESSION_ID", "worker_sess")
    with kbc.connect_closing() as conn:
        creator = kb.create_task(conn, title="creator", assignee="default", session_id="owner_chat")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='running' WHERE id=?", (creator,))
            kb._append_event(conn, creator, 'owner_admitted', {'session_id': 'worker_sess'})
    monkeypatch.setenv("HERMES_KANBAN_TASK", creator)  # a running admitted creator that descends from a registered owning chat is durable authority
    # never the worker session; the child inherits the creator's owning chat
    assert _create("worker child") == "owner_chat"


def _create_args(*argv: str) -> str:
    root = argparse.ArgumentParser(prog="hermes")
    kc.build_parser(root.add_subparsers())
    assert kc._cmd_create(root.parse_args(["kanban", "create", *argv])) == 0
    with kbc.connect_closing() as conn:
        return next(t for t in kb.list_tasks(conn) if t.title == argv[0]).id


def _subs(task_id: str) -> list[tuple[str, str]]:
    from hermes_cli import kanban_db_notify as kbn

    with kbc.connect_closing() as conn:
        return [(s["platform"], s["chat_id"]) for s in kbn.list_notify_subs(conn, task_id)]


def test_cli_create_with_session_key_stamps_and_subscribes(kanban_home, monkeypatch, capsys):
    """Desktop/TUI terminals carry HERMES_SESSION_KEY: the card is owned by that chat,
    gets the same ``tui`` completion subscription the kanban_create tool adds, and
    keeps the chat's sidebar row busy (kanban_active). [daveey]"""
    from hermes_cli import kanban_session_activity as ksa

    _seed_session(kanban_home, "20261014_152627_e39461")
    monkeypatch.setenv("HERMES_SESSION_KEY", "20261014_152627_e39461")
    tid = _create_args("cli card from desktop terminal")
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).session_id == "20261014_152627_e39461"
    assert ("tui", "20261014_152627_e39461") in _subs(tid)
    ksa._reset_cache_for_tests()
    assert ksa.active_task_counts().get("20261014_152627_e39461") == 1


def test_cli_create_without_session_key_adds_no_subscription(kanban_home, capsys):
    from hermes_cli import kanban_session_activity as ksa

    tid = _create_args("plain user shell")
    assert _subs(tid) == []
    ksa._reset_cache_for_tests()
    assert ksa.active_task_counts() == {}


def test_cli_create_idempotency_hit_does_not_restamp(kanban_home, monkeypatch, capsys):
    tid = _create_args("idem card", "--idempotency-key", "k1")
    _seed_session(kanban_home, "sess_later")
    monkeypatch.setenv("HERMES_SESSION_KEY", "sess_later")
    assert _create_args("idem card", "--idempotency-key", "k1") == tid
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).session_id is None
    assert _subs(tid) == []
