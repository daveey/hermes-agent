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


_ROUTING_KEY = f"agent:main:local:dm:{MINE}"
# caller -> (subprocess env, the chat id the warning must suggest; None = never warned)
_CALLERS = {
    "routing": ({"HERMES_SESSION_KEY": _ROUTING_KEY}, MINE),
    "bare": ({"HERMES_SESSION_KEY": MINE}, MINE),
    "preferred": ({"HERMES_SESSION_KEY": _ROUTING_KEY, "HERMES_SESSION_ID": OTHER}, OTHER),
    "preferred_bare": ({"HERMES_SESSION_KEY": MINE, "HERMES_SESSION_ID": OTHER}, OTHER),
    "stale_id": ({"HERMES_SESSION_KEY": _ROUTING_KEY, "HERMES_SESSION_ID": "stale-unstored"}, MINE),
    "worker": ({"HERMES_SESSION_KEY": _ROUTING_KEY, "HERMES_KANBAN_TASK": "t_worker"}, None),
    "unknown": ({"HERMES_SESSION_KEY": "agent:main:local:dm:missing"}, None),
}


@pytest.mark.parametrize("target", [MINE, OTHER])
@pytest.mark.parametrize("caller", sorted(_CALLERS))
def test_subscribe_resolves_caller_before_warning(home, monkeypatch, capsys, target, caller):
    env, resolved = _CALLERS[caller]
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    rc, _ = _subscribe(target)
    out = capsys.readouterr()
    assert rc == 0
    if resolved and target != resolved:
        assert "WARNING: binding a different chat" in out.err
        assert f"re-run with --chat-id {resolved} and" in out.err
    else:
        assert "WARNING" not in out.err
    assert "agent:main:local:dm:" not in out.err
    assert "stale-unstored" not in out.err


def test_subscribe_routing_key_same_compression_lineage(home, monkeypatch, capsys):
    from hermes_state import SessionDB
    with SessionDB(db_path=home / "state.db") as db:
        db.publish_compression_child(parent_session_id=MINE, child_session_id="compressed",
                                     source="desktop", messages=[{"role": "user", "content": "summary"}],
                                     require_compression_lease=False)
    monkeypatch.setenv("HERMES_SESSION_KEY", f"agent:main:local:dm:{MINE}")
    rc, _ = _subscribe("compressed")
    assert rc == 0
    assert "WARNING" not in capsys.readouterr().err


def test_unknown_target_keeps_id_diagnostic_without_lineage_warning(home, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_SESSION_ID", MINE)
    rc, _ = _subscribe("missing")
    out = capsys.readouterr()
    assert rc == 0
    assert "not a chat in this profile's state.db" in out.out
    assert "WARNING" not in out.err


def test_real_routing_shaped_id_is_not_replaced_by_suffix(home, monkeypatch, capsys):
    from hermes_state import SessionDB
    key = f"agent:main:local:dm:{MINE}"
    with SessionDB(db_path=home / "state.db") as db:
        db.create_session(session_id=key, source="desktop")
    monkeypatch.setenv("HERMES_SESSION_KEY", key)
    rc, _ = _subscribe(MINE)
    assert rc == 0
    assert f"re-run with --chat-id {key} and" in capsys.readouterr().err


def test_subscribe_from_a_plain_shell_does_not_warn(home, capsys):
    rc, _ = _subscribe(OTHER)
    out = capsys.readouterr()
    assert rc == 0 and "WARNING" not in out.out + out.err
    assert "Katie Suarez stabbing case status" in out.out
