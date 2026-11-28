"""Release offers survive a requesting chat's earlier notice-only subscription. [daveey]"""
from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_chat_links as links
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as notify
from hermes_cli import kanban_release_offer as offers
from tests.hermes_cli import test_kanban_release_offer as helpers

kanban_home = helpers.kanban_home
own_gateway = helpers.own_gateway
board_allowance = helpers.board_allowance


@pytest.mark.parametrize("reason", ["touch", "send_refused", "shepherd", "create"])
@pytest.mark.parametrize("remote", [False, True])
def test_explicit_offer_keeps_chat_binding_after_reconcile(
        kanban_home, own_gateway, board_allowance, reason, remote, monkeypatch, capsys):
    gateway = "remote" if remote else helpers.park.OWN
    session = helpers.CHAT
    ref = f"{gateway}/default/{session}"
    platform, chat_id = ("hub", ref) if remote else ("tui", session)
    with kbc.connect_closing() as conn:
        tid = helpers._held(conn, board_allowance)
        notify.add_notify_sub(conn, task_id=tid, platform=platform, chat_id=chat_id,
                              delivery_mode="notify+wake", delivery_metadata={"reason": reason, "anchor": "keep"})
        notify.add_notify_sub(conn, task_id=tid, platform="tui", chat_id="unrelated-chat",
                              delivery_metadata={"reason": "touch"})
        chat = offers.requesting_chat(ref, own=helpers.park.OWN)
        with kb.write_txn(conn):
            helpers.stranded._link_desk(conn, tid, chat, 1)
            conn.execute("UPDATE task_chat_links SET released=1 WHERE task_id=?", (tid,))
        before = dict(conn.execute("SELECT * FROM kanban_notify_subs WHERE task_id=? AND chat_id=?",
                                   (tid, chat_id)).fetchone())
        for expected in ("offered", "already_offered"):
            rc, out = helpers._cli(monkeypatch, capsys, tid, "--for-chat", ref)
            assert rc == 0 and out["outcome"] == expected
            links.reconcile(conn, "default", helpers.park.OWN, ledger=False)
            links.reconcile(conn, "default", helpers.park.OWN, ledger=False)
            row = conn.execute("SELECT released FROM task_chat_links WHERE task_id=? AND chat_session=?",
                               (tid, session)).fetchone()
            assert row is not None and row[0] == 0
            if not remote:
                wanted = links._local_links(conn, helpers.park.OWN)
                assert any(r["task_id"] == tid and r["chat_session"] == session for r in wanted.values())
        after = dict(conn.execute("SELECT * FROM kanban_notify_subs WHERE task_id=? AND chat_id=?",
                                  (tid, chat_id)).fetchone())
        assert json.loads(after["delivery_metadata"]) == {
            "reason": reason if reason in ("create", "shepherd") else "shepherd", "anchor": "keep"}
        assert after["last_event_id"] == before["last_event_id"]
        assert after["delivery_mode"] == before["delivery_mode"]
        assert json.loads(conn.execute("SELECT delivery_metadata FROM kanban_notify_subs WHERE chat_id=?",
                                       ("unrelated-chat",)).fetchone()[0]) == {"reason": "touch"}
        assert len(helpers.park._events(conn, tid, helpers.stranded.EVENT)) == 1
        assert kb.get_task(conn, tid).status == "triage"


def test_new_subscription_precedes_offer_event(kanban_home, own_gateway, board_allowance, monkeypatch):
    from tools import kanban_offer_release_tools as tool
    monkeypatch.setenv("HERMES_SESSION_ID", helpers.CHAT)
    with kbc.connect_closing() as conn:
        tid = helpers._held(conn, board_allowance)
        out = json.loads(tool.handle({"task_id": tid}))
        assert out["ok"] and not out["released"]
        cursor = conn.execute("SELECT last_event_id FROM kanban_notify_subs WHERE task_id=? AND chat_id=?",
                              (tid, helpers.CHAT)).fetchone()[0]
        event = conn.execute("SELECT id FROM task_events WHERE task_id=? AND kind=?",
                             (tid, helpers.stranded.EVENT)).fetchone()[0]
        assert cursor < event
