"""``hermes kanban notify-subscribe --platform tui`` says which chat it binds. [daveey]

agent-884fdd96f8: an agent ran the subscribe with another chat's id (a wrong hint), read back the subscriber ids
and reported the card linked to its own chat. The command now prints the target chat's title, and a WARNING when
the calling agent's chat (HERMES_SESSION_KEY / HERMES_SESSION_ID in its env) is a different chat.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

import gateway.session_context as sc
from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc

MINE, OTHER = "20261103_032737_370c8b", "20261102_172718_5fb607"


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in ("HERMES_SESSION_KEY", "HERMES_SESSION_ID", "HERMES_KANBAN_TASK"):
        monkeypatch.delenv(var, raising=False)
    kb.init_db()
    from hermes_state import SessionDB
    db = SessionDB(db_path=home / "state.db")
    try:
        db.create_session(session_id=MINE, source="desktop")
        db.set_session_title(MINE, "Repair cards for the forwarder")
        db.create_session(session_id=OTHER, source="desktop")
        db.set_session_title(OTHER, "Katie Suarez stabbing case status")
    finally:
        db.close()
    tokens = [var.set(sc._UNSET) for var in sc._VAR_MAP.values()]  # a CLI subprocess: env only
    engaged = sc._session_context_engaged
    sc._session_context_engaged = False
    yield home
    sc._session_context_engaged = engaged
    for var, tok in zip(sc._VAR_MAP.values(), tokens):
        var.reset(tok)


def _subscribe(chat_id: str) -> tuple[int, str]:
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="repair", assignee="default")
    root = argparse.ArgumentParser(prog="hermes")
    kc.build_parser(root.add_subparsers())
    args = root.parse_args(["kanban", "notify-subscribe", tid, "--platform", "tui", "--chat-id", chat_id])
    return kc._cmd_notify_subscribe(args), tid


def test_subscribe_names_the_target_chat(home, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_SESSION_ID", MINE)
    rc, _ = _subscribe(MINE)
    out = capsys.readouterr()
    assert rc == 0
    assert "Repair cards for the forwarder" in out.out
    assert "WARNING" not in out.out + out.err


def test_subscribe_warns_when_binding_a_different_chat(home, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_SESSION_ID", MINE)
    rc, _ = _subscribe(OTHER)
    out = capsys.readouterr()
    text = out.out + out.err
    assert rc == 0
    assert "WARNING: binding a different chat (Katie Suarez stabbing case status)" in text, text
    assert MINE in text


def test_subscribe_from_a_plain_shell_does_not_warn(home, capsys):
    rc, _ = _subscribe(OTHER)
    out = capsys.readouterr()
    assert rc == 0 and "WARNING" not in out.out + out.err
    assert "Katie Suarez stabbing case status" in out.out
