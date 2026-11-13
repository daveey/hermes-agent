"""The live resync sweep's ``Held`` (t_01a1d6fcb03dee02, gap tests for PR #2571).

``held_at_start`` reads every outbox op before the rebuild, and the sweep spares those rows and every row of the
cards they name, because a concurrent ``reconcile`` can ack the ops mid-rebuild. Each op kind names its card its own
way: a ``tasks`` op by its uid, a ``task_links`` op by its child, any other child-table op by its row's ``task_id``.
The ``task_links`` uids read at that moment bound the links sweep: a link written later is spared even when its
child card has no ``linked`` event, which ``create_task(parents=...)`` does not write.
"""
from __future__ import annotations

import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_sync_resync_sweep as sweep
from tests.hermes_cli import test_kanban_sync_live_resync_sweep as base

BOARD, ME = base.BOARD, base.ME
kanban_home = base.kanban_home
live = base.live


def _ack_all(conn):
    with kbc.write_txn(conn):
        conn.execute("DELETE FROM kanban_sync_outbox")


def _outbox_tables(conn):
    return {t for (t,) in conn.execute("SELECT table_name FROM kanban_sync_outbox")}


@pytest.mark.parametrize("held_op", ["card", "comment", "link", "link_done_parent"])
def test_an_absent_row_of_a_card_named_by_a_held_op_is_spared(live, held_op):
    if held_op == "card":
        card = kb.create_task(live, title="card whose own op is pending")
    else:
        card = base._acked_card(live, "card the op names")
    base._acked_child(live, "task_comments", card, "c-old", int(time.time()) - 60)
    if held_op == "comment":
        kb.add_comment(live, card, "me", "pending comment")
        # child-table ops only (the comment and its event): their rows' task_id is what names the card
        assert _outbox_tables(live) <= {"task_comments", "task_events"}
    elif held_op == "link":
        kb.link_tasks(live, base._acked_card(live, "parent"), card)
    elif held_op == "link_done_parent":
        parent = base._acked_card(live, "done parent")
        with kbc.write_txn(live):
            live.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (parent,))
            live.execute("DELETE FROM kanban_sync_outbox")
        kb.link_tasks(live, parent, card)  # a done parent: no status write on the child
        # The linked event's op was acked; the link op is still held (pending or refused). Only its child names the card.
        with kbc.write_txn(live):
            live.execute("DELETE FROM kanban_sync_outbox WHERE table_name != 'task_links'")
        assert _outbox_tables(live) == {"task_links"}
    held = sweep.held_at_start(live)
    assert card in held.cards
    _ack_all(live)  # a concurrent reconcile acks every op before the sweep
    seen = base._seen_cards(*base._ids(live))
    swept = sweep.sweep_absent(live, seen, BOARD, ME, since=int(time.time()) + 60, held=held)
    assert "c-old" in base._ids(live, "task_comments", "uid"), swept


def test_a_link_from_a_card_created_with_a_parent_mid_rebuild_is_not_swept(live):
    parent = base._acked_card(live, "parent on the Board")
    held = sweep.held_at_start(live)
    assert held.links == frozenset()
    since = int(time.time())
    child = kb.create_task(live, title="filed mid-rebuild under the parent", parents=[parent])
    _ack_all(live)
    assert not live.execute("SELECT 1 FROM task_events WHERE task_id = ? AND kind IN ('linked', 'unlinked')",
                            (child,)).fetchone()  # so _LINK_QUIET does not spare it: only the Held.links bound does
    seen = {**base._seen_cards(parent), "task_links": set()}
    swept = sweep.sweep_absent(live, seen, BOARD, ME, since=since, held=held)
    assert "task_links" not in swept
    assert (parent, child) in {tuple(r) for r in live.execute("SELECT parent_id, child_id FROM task_links")}
