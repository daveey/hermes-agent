"""Ready-and-unclaimed notices judge only from a copy at the feed head. [daveey]

2026-11-12 22:07-22:12 PT: David-MBP's live board was ~74k changes behind the Board (its sync round was stuck in a
long pull). The 30-minute "ready with no worker" notice told a chat that three cards had no worker while on the Board
one was running on worker-2, one was scheduled on its CI wake-check and one was running on worker-3. Neither the
direct chat-wait sweep nor the parent-unclaimed sweep may judge "unclaimed" from a copy whose replica last reached
the feed head more than ``kanban_replica_freshness.FRESH_SECONDS`` ago; the ready timer keeps running, so a card still
unclaimed after catch-up is reported on the next tick.
"""
from __future__ import annotations

import time

from hermes_cli import kanban_chat_wait as cw
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_notify as kn
from hermes_cli import kanban_parent_unclaimed as pu
from hermes_cli import kanban_replica_freshness as fresh
from tests.hermes_cli import test_kanban_cross_gateway_parents as cg

kanban_home, svc, live = cg.kanban_home, cg.svc, cg.live
STALE = fresh.FRESH_SECONDS + 1


def _count(conn, tid, kind):
    return conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = ?", (tid, kind)).fetchone()[0]


def _backdate(conn, ids, seconds):
    then = int(time.time()) - seconds
    for tid in ids:
        conn.execute("UPDATE tasks SET created_at = ? WHERE id = ?", (then, tid))
        conn.execute("UPDATE task_events SET created_at = ? WHERE task_id = ?", (then, tid))
    conn.commit()


def _chat_ready_card(conn, kanban_home, svc):
    tid = kb.create_task(conn, title="ready card a chat waits on", assignee="worker")
    conn.execute("UPDATE tasks SET session_id = 'origin' WHERE id = ?", (tid,))
    conn.commit()
    cg._round(kanban_home, svc)  # the replica exists, stamped at the feed head
    _backdate(conn, [tid], 31 * 60)
    return tid


def _waiting_child(conn, kanban_home, svc):
    parent = kb.create_task(conn, title="parent", assignee="default", initial_status="running")
    child = kb.create_task(conn, title="child", assignee="default", parents=[parent])
    kn.add_notify_sub(conn, task_id=child, platform="tui", chat_id="origin-chat", delivery_metadata={"reason": "x"})
    cg._round(kanban_home, svc)
    _backdate(conn, [parent, child], 31 * 60)
    return parent, child


def test_chat_wait_sends_nothing_from_a_stale_copy_then_reports_after_catch_up(live, svc, kanban_home):
    tid = _chat_ready_card(live, kanban_home, svc)
    cg._stamp_replica(time.time() - STALE)
    assert cw.sweep(live) == 0  # before the fix: "ready 30 min with no worker" from a copy 74k changes behind
    assert _count(live, tid, cw.NOTICE_KIND) == 0
    cg._stamp_replica(time.time())  # caught up and the card is really still unclaimed: the next tick reports it
    assert cw.sweep(live) == 1
    assert _count(live, tid, cw.NOTICE_KIND) == 1


def test_chat_wait_from_a_fresh_copy_is_unchanged(live, svc, kanban_home):
    tid = _chat_ready_card(live, kanban_home, svc)
    assert cw.sweep(live) == 1 and cw.sweep(live) == 0
    assert _count(live, tid, cw.NOTICE_KIND) == 1


def test_parent_unclaimed_sends_nothing_from_a_stale_copy(live, svc, kanban_home):
    parent, child = _waiting_child(live, kanban_home, svc)
    assert kb.get_task(live, parent).status == "ready" and kb.get_task(live, child).status == "todo"
    cg._stamp_replica(time.time() - STALE)
    assert pu.sweep(live) == 0
    assert _count(live, child, pu.NOTICE_KIND) == 0
    cg._stamp_replica(time.time())
    assert pu.sweep(live) == 1
    assert _count(live, child, pu.NOTICE_KIND) == 1


def test_an_unsynced_board_is_its_own_authority(tmp_path, monkeypatch):
    from tests.hermes_cli import kanban_sync_support as support
    from hermes_cli import kanban_db_connect as kbc
    home = support.kanban_home(tmp_path, monkeypatch)
    support.write_sync_config(home, False)
    conn = kbc.connect(home / "kanban.db")
    try:
        assert not fresh.local_copy_stale(conn, "test")
    finally:
        conn.close()
