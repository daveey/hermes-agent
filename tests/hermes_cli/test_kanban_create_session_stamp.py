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
    assert _create("stray env") is None


def test_cli_create_without_session_env_is_unstamped(kanban_home, capsys):
    assert _create("plain shell") is None


def test_cli_create_in_worker_does_not_stamp_worker_session(kanban_home, monkeypatch, capsys):
    _seed_session(kanban_home, "worker_sess")
    monkeypatch.setenv("HERMES_SESSION_ID", "worker_sess")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_doesnotexist")
    assert _create("worker child") is None
