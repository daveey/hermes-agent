"""One dependency view for the promoter, the claim and the Board (2026-10-28 incident).

worker-1 linked a parent card it owned to a card the MacBook owned. The link reached the Board, but the MacBook's
live board file dropped it (``own_partition`` kept a link only when BOTH ends were its own cards), so its promoter
kept marking the child ready while the Board refused every lease ``parents_not_done``: 60 promoted/claim_rejected
events in 7 minutes. These tests pin the fix: the owner keeps a link whose child is its own, gates on the foreign
parent's Board status (mirrored from the replica), never treats a parent it cannot find as satisfied, blocks such a
child after ``PARENT_MISSING_BLOCK_SECONDS``, and never pushes a link ahead of a card it names. [daveey]"""

from __future__ import annotations

import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_client as ksc
from tests.hermes_cli import kanban_sync_support as support
from tests.hermes_cli.kanban_sync_fake_board import FakeService

ME, OTHER, BOARD = "macbook", "worker-1", "default"


# --- local board: missing parent ------------------------------------------------------------------------------

@pytest.fixture
def local(tmp_path, monkeypatch):
    home = support.kanban_home(tmp_path, monkeypatch)
    support.write_sync_config(home, False)
    conn = kbc.connect(home / "kanban.db")
    try:
        yield conn
    finally:
        conn.close()


def _link_raw(conn, parent, child):
    with kbc.write_txn(conn):
        conn.execute("INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)", (parent, child))
        conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (child,))


def _events(conn, tid, kind):
    return conn.execute("SELECT payload, created_at FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id",
                        (tid, kind)).fetchall()


def test_a_child_whose_parent_is_missing_is_not_promoted_then_blocked_after_the_timeout(local):
    child = kb.create_task(local, title="child", assignee="worker")
    _link_raw(local, "t_ghost", child)
    assert kb.parent_states(local, child) == [("t_ghost", None)]
    kb.recompute_ready(local)
    assert kb.get_task(local, child).status == "todo"  # before the fix: promoted (the JOIN dropped the parent)
    assert not kb._parents_satisfied(local, child)
    assert kb.unsatisfied_parents(local, child) == [("t_ghost", "missing")]
    assert len(_events(local, child, "parent_missing")) == 1
    kb.recompute_ready(local)  # still inside the window: no second event, no block
    assert kb.get_task(local, child).status == "todo"
    assert len(_events(local, child, "parent_missing")) == 1
    with kbc.write_txn(local):
        local.execute("UPDATE task_events SET created_at = created_at - ? WHERE task_id = ? AND kind = 'parent_missing'",
                      (kb.PARENT_MISSING_BLOCK_SECONDS + 1, child))
    kb.recompute_ready(local)
    t = kb.get_task(local, child)
    assert (t.status, t.block_kind) == ("blocked", "transient")
    [(payload, _at)] = _events(local, child, "blocked")
    assert "parent t_ghost missing from this board" in payload
    kb.recompute_ready(local)  # a sticky block: the promoter leaves it for an unlink + unblock
    assert kb.get_task(local, child).status == "blocked"


def test_a_forced_ready_child_with_a_missing_parent_is_not_claimed(local):
    child = kb.create_task(local, title="child", assignee="worker")
    _link_raw(local, "t_ghost", child)
    with kbc.write_txn(local):
        local.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (child,))
    assert kb.claim_task(local, child) is None
    assert kb.get_task(local, child).status == "todo"


def test_a_parent_that_comes_back_closes_the_missing_streak(local):
    child = kb.create_task(local, title="child", assignee="worker")
    _link_raw(local, "t_late", child)
    kb.recompute_ready(local)
    assert len(_events(local, child, "parent_missing")) == 1
    with kbc.write_txn(local):
        local.execute("INSERT INTO tasks (id, title, status, created_at) VALUES ('t_late', 'late', 'running', ?)",
                      (int(time.time()),))
    kb.recompute_ready(local)
    assert len(_events(local, child, "parent_found")) == 1
    assert kb.get_task(local, child).status == "todo"  # the parent is open: still gated, never blocked


def test_the_normal_parent_child_flow_is_unchanged(local):
    parent = kb.create_task(local, title="parent", assignee="worker")
    child = kb.create_task(local, title="child", assignee="worker", parents=[parent])
    kb.recompute_ready(local)
    assert kb.get_task(local, child).status == "todo"
    assert kb.unsatisfied_parents(local, child) == [(parent, "ready")]
    assert not _events(local, child, "parent_missing")
    assert kb.claim_task(local, parent) is not None
    assert kb.complete_task(local, parent, result="ok")
    kb.recompute_ready(local)
    assert kb.get_task(local, child).status == "ready"
    assert kb.claim_task(local, child) is not None
    assert not _events(local, child, "parent_missing") and not _events(local, child, "claim_rejected")


# --- synced board: a parent another gateway owns --------------------------------------------------------------

@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    return support.kanban_home(tmp_path, monkeypatch)


@pytest.fixture
def svc():
    return FakeService(lease_runs=True)


@pytest.fixture
def live(kanban_home, svc):
    support.write_sync_config(kanban_home, True)
    conn = kbc.connect(kanban_home / "kanban.db")
    ksc.ensure_identity(conn, BOARD, ME)
    sync.set_mode(conn, "primary")
    previous = sync.set_lease_client(ksc.HubLeaseClient(svc.hub(ME)))
    try:
        yield conn
    finally:
        sync.set_lease_client(previous)
        conn.close()


def _round(kanban_home, svc):
    return ksc.sync_board(BOARD, kanban_home / "kanban.db", svc.hub(ME), ME)


def _other(svc, op_id, table, uid, fields):
    hub = svc.hub(OTHER)
    res = hub.ops(BOARD, [{"client_op_id": f"other-{op_id}", "op": "upsert", "table": table, "uid": uid,
                           "hlc": f"{time.time_ns() // 1_000_000 + 1000:013d}-0000-{OTHER}", "fields": fields}])
    assert res["results"][0]["status"] == "applied", res
    return res


def test_a_foreign_parent_gates_its_child_on_the_owner_until_it_is_done(live, svc, kanban_home):
    local_parent = kb.create_task(live, title="local parent", assignee="worker")
    child = kb.create_task(live, title="child", assignee="worker", parents=[local_parent])
    _round(kanban_home, svc)
    # Another gateway creates a parent card it owns and links it to our child (the worker-1 run of 2026-10-28).
    _other(svc, "p", "tasks", "t_foreign", {"title": "foreign parent", "status": "ready", "created_at": 1})
    _other(svc, "l", "task_links", f"t_foreign>{child}", {"parent_id": "t_foreign", "child_id": child})
    _round(kanban_home, svc)
    assert kb.get_task(live, "t_foreign") is None  # still not a local card: the dispatcher never sees it
    assert live.execute("SELECT 1 FROM task_links WHERE parent_id = 't_foreign' AND child_id = ?",
                        (child,)).fetchone()  # before the fix: dropped by own_partition
    assert kb.parent_states(live, child) == sorted([(local_parent, "ready"), ("t_foreign", "ready")])
    # Our own parent finishes. Before the fix the promoter now promoted the child the Board would refuse.
    assert kb.claim_task(live, local_parent) is not None
    assert kb.complete_task(live, local_parent, result="ok")
    kb.recompute_ready(live)
    assert kb.get_task(live, child).status == "todo"
    assert kb.unsatisfied_parents(live, child) == [("t_foreign", "ready")]
    assert not _events(live, child, "parent_missing")
    # The foreign parent finishes on its gateway: the next round mirrors it and the child is released.
    _other(svc, "done", "tasks", "t_foreign", {"status": "done"})
    sync.consume_dispatcher_wake()
    _round(kanban_home, svc)
    assert sync.consume_dispatcher_wake()  # the round woke the dispatcher for the released child
    kb.recompute_ready(live)
    assert kb.get_task(live, child).status == "ready"
    assert kb.claim_task(live, child) is not None


def test_a_link_from_our_card_to_a_foreign_child_stays_out_of_the_live_board(live):
    mine = kb.create_task(live, title="own", assignee="worker")
    ch = [
        sync.RemoteChange(1, "task_links", f"{mine}>t_foreign", "upsert", {}, 1, "", OTHER),
        sync.RemoteChange(2, "task_links", f"t_foreign>{mine}", "upsert", {}, 1, "", OTHER),
    ]
    assert [c.seq for c in ksc.own_partition(live, ch, ME)] == [2]


def test_a_link_never_reaches_the_board_ahead_of_its_parent_card(live, svc):
    with kbc.write_txn(live):
        sync.drop_triggers(live)
    parent = kb.create_task(live, title="parent made in a capture gap", assignee="worker")  # never queued
    sync.install_triggers(live)
    child = kb.create_task(live, title="child", assignee="worker", parents=[parent])
    res = ksc.push(live, svc.hub(ME), BOARD, ME, primary=True)
    rows = svc.board(BOARD)["rows"]
    # Before the fix the link went out alone and the Board refused it for good (not_found: unknown task).
    assert res.rejected == 0, live.execute("SELECT last_error FROM kanban_sync_outbox").fetchall()
    assert parent in rows["tasks"] and f"{parent}>{child}" in rows["task_links"]


def test_a_link_the_board_refuses_for_an_unknown_card_is_retried_not_dropped():
    assert ksc.transient_rejection({"reason": "not_found", "detail": "unknown task(s): t_x"})
    assert ksc.transient_rejection({"reason": "not_found", "detail": "no task t_x"})
    assert not ksc.transient_rejection({"reason": "cycle", "detail": "x"})


def test_a_worker_node_does_not_copy_a_ready_card_whose_parent_is_open(kanban_home):
    support.write_sync_config(kanban_home, True)
    replica = kbc.connect(ksc.shadow_path(BOARD))
    try:
        now = int(time.time())
        with kbc.write_txn(replica):
            for tid, status in (("t_p", "ready"), ("t_c", "ready"), ("t_free", "ready")):
                replica.execute("INSERT INTO tasks (id, title, status, created_at, created_on) VALUES (?, ?, ?, ?, ?)",
                                (tid, tid, status, now, ME))
            replica.execute("INSERT INTO task_links (parent_id, child_id) VALUES ('t_p', 't_c')")
            replica.execute("INSERT INTO task_links (parent_id, child_id) VALUES ('t_gone', 't_free')")
        from hermes_cli import kanban_worker_node as wn
        ids = {r["id"] for r in wn.candidates(replica, OTHER)}
        assert ids == {"t_p"}  # t_c waits on t_p; t_free names a parent the Board does not have
        with kbc.write_txn(replica):
            replica.execute("UPDATE tasks SET status = 'done' WHERE id = 't_p'")
            replica.execute("DELETE FROM task_links WHERE parent_id = 't_gone'")
        assert {r["id"] for r in wn.candidates(replica, OTHER)} == {"t_c", "t_free"}
    finally:
        replica.close()
