"""Dependency edges of picked-up foreign copies stay what the Board holds (hub, 2026-10-29).

Two paths dropped edges from the live file while the Board kept them, so ``diff`` reported settled
``task_links`` mismatches with pending=0:

* ``evict`` deleted every edge of an evicted copy, including the edge from it to one of OUR cards: the hub
  child t_01a189f115bdb5ef of the copy t_01a189a30ea5c64d (evicted between its review hand-off and re-pickup)
  lost its gate and was promoted 51 times while the Board refused every lease ``parents_not_done``.
* ``import_cards`` copied a card's runs/comments/events but not its parent edges, although the feed applies
  every later edge of a copy (``own_partition`` keeps a link whose child is in the file).

The next sync round restores edges already lost that way (``repair_partition_links``)."""

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_sync_client as ksc
from hermes_cli import kanban_worker_node as wn
from tests.hermes_cli.test_kanban_cross_gateway_parents import (
    BOARD, ME, OTHER, _other, _round, kanban_home, live, svc,
)

__all__ = ["kanban_home", "live", "svc"]


def _import(live, *ids):
    replica = ksc.open_shadow(BOARD, ME)
    try:
        replica.row_factory = ksc.sqlite3.Row
        assert wn.import_cards(live, replica, list(ids)) == list(ids)
    finally:
        replica.close()


def _diff(live):
    shadow = ksc.open_shadow(BOARD, ME)
    try:
        return ksc.diff(live, shadow, ME)
    finally:
        shadow.close()


def _edges(live, tid):
    return sorted(tuple(r) for r in live.execute(
        "SELECT parent_id, child_id FROM task_links WHERE parent_id = ? OR child_id = ?", (tid, tid)))


def _drop_uncaptured(live, parent, child):
    """The pre-fix loss: an edge deleted with capture suppressed (old ``evict``) or never imported."""
    with kbc.write_txn(live):
        live.execute("UPDATE kanban_sync_state SET applying = 1")
        live.execute("DELETE FROM task_links WHERE parent_id = ? AND child_id = ?", (parent, child))
        live.execute("UPDATE kanban_sync_state SET applying = 0")


def _foreign_parent_with_own_child(live, svc, kanban_home):
    _other(svc, "p", "tasks", "t_copy", {"title": "theirs", "status": "running", "created_at": 1,
                                         "created_on": OTHER})
    _round(kanban_home, svc)
    _import(live, "t_copy")
    # A worker running the copy here files a follow-up card of ours that waits on it.
    child = kb.create_task(live, title="follow-up", assignee="worker", parents=["t_copy"])
    _round(kanban_home, svc)
    _round(kanban_home, svc)
    assert _edges(live, child) == [("t_copy", child)]
    assert _diff(live)["child_mismatch"] == {}
    return child


def test_evicting_a_foreign_parent_keeps_its_edge_to_our_child(live, svc, kanban_home):
    child = _foreign_parent_with_own_child(live, svc, kanban_home)
    with kbc.write_txn(live):  # the copy is handed back for review: unclaimed, not running
        live.execute("UPDATE kanban_sync_state SET applying = 1")
        live.execute("UPDATE tasks SET status = 'review', claim_lock = NULL WHERE id = 't_copy'")
        live.execute("UPDATE kanban_sync_state SET applying = 0")
    assert wn.evict(live) == ["t_copy"]
    assert _edges(live, child) == [("t_copy", child)]
    kb.recompute_ready(live)
    assert kb.get_task(live, child).status == "todo"  # before the fix: promoted, then refused by the Board
    _round(kanban_home, svc)
    assert _diff(live)["child_mismatch"] == {}


def test_a_round_restores_a_lost_edge_from_a_present_foreign_parent_to_our_child(live, svc, kanban_home):
    child = _foreign_parent_with_own_child(live, svc, kanban_home)
    _drop_uncaptured(live, "t_copy", child)  # hub t_01a189a30ea5c64d>t_01a189f115bdb5ef after re-pickup
    assert _diff(live)["child_mismatch"] == {"task_links": 1}
    _round(kanban_home, svc)
    assert _edges(live, child) == [("t_copy", child)]
    assert not live.execute("SELECT 1 FROM kanban_sync_outbox WHERE table_name = 'task_links'").fetchone()
    assert _diff(live)["child_mismatch"] == {}


def _foreign_child_of_done_parent(svc, kanban_home, status="running"):
    _other(svc, "dp", "tasks", "t_done", {"title": "parent", "status": "done", "created_at": 1,
                                          "created_on": OTHER})
    _other(svc, "dc", "tasks", "t_kid", {"title": "kid", "status": status, "created_at": 2,
                                         "created_on": OTHER, "assignee": "worker"})
    _other(svc, "dl", "task_links", "t_done>t_kid", {"parent_id": "t_done", "child_id": "t_kid"})
    _round(kanban_home, svc)


def test_a_picked_up_copy_carries_its_parent_edges(live, svc, kanban_home):
    _foreign_child_of_done_parent(svc, kanban_home)
    _import(live, "t_kid")
    assert kb.get_task(live, "t_done") is None
    assert _edges(live, "t_kid") == [("t_done", "t_kid")]
    assert _diff(live)["child_mismatch"] == {}
    _round(kanban_home, svc)
    assert not live.execute("SELECT 1 FROM kanban_sync_outbox WHERE table_name = 'task_links'").fetchone()
    assert _diff(live)["child_mismatch"] == {}


def test_a_ready_copy_with_a_done_foreign_parent_is_claimable_right_after_pickup(live, svc, kanban_home):
    _foreign_child_of_done_parent(svc, kanban_home, status="ready")
    _import(live, "t_kid")
    assert kb.parent_states(live, "t_kid") == [("t_done", "done")]
    assert kb.claim_task(live, "t_kid") is not None
    kid = kb.get_task(live, "t_kid")
    assert kid is not None and kid.status == "running"
    assert not live.execute("SELECT 1 FROM task_events WHERE task_id = 't_kid' AND kind = 'claim_rejected'").fetchone()


def test_a_round_restores_the_parent_edge_of_a_copy_imported_without_it(live, svc, kanban_home):
    _foreign_child_of_done_parent(svc, kanban_home)
    _import(live, "t_kid")
    _drop_uncaptured(live, "t_done", "t_kid")  # hub t_01a189c2ac722da1>t_01a189ecd7fcbec2 (old import)
    assert _diff(live)["child_mismatch"] == {"task_links": 1}
    _round(kanban_home, svc)
    assert _edges(live, "t_kid") == [("t_done", "t_kid")]
    assert _diff(live)["child_mismatch"] == {}


def test_an_evicted_copy_takes_its_own_parent_edges_with_it(live, svc, kanban_home):
    _foreign_child_of_done_parent(svc, kanban_home)
    _import(live, "t_kid")
    with kbc.write_txn(live):
        live.execute("UPDATE kanban_sync_state SET applying = 1")
        live.execute("UPDATE tasks SET status = 'done', claim_lock = NULL WHERE id = 't_kid'")
        live.execute("UPDATE kanban_sync_state SET applying = 0")
    assert wn.evict(live) == ["t_kid"]
    assert _edges(live, "t_kid") == []
    _round(kanban_home, svc)
    assert _edges(live, "t_kid") == []  # the copy is gone: nothing to restore onto


def test_an_edge_between_own_cards_missing_live_is_still_a_mismatch(live, svc, kanban_home):
    parent = kb.create_task(live, title="parent", assignee="worker")
    child = kb.create_task(live, title="child", assignee="worker", parents=[parent])
    _round(kanban_home, svc)
    _round(kanban_home, svc)
    assert _diff(live)["child_mismatch"] == {}
    _drop_uncaptured(live, parent, child)
    assert _diff(live)["child_mismatch"] == {"task_links": 1}
