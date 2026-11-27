"""Local notice cursors must retain their event-ID namespace across worker pickups."""
from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_notify as notify
from hermes_cli import kanban_worker_node as wn
from tests.hermes_cli import test_kanban_worker_node as helpers

pytestmark = pytest.mark.usefixtures("svc")
gws = helpers.gws
svc = helpers.svc
CHAT = "kanban_owner_kanban_dispatcher"
KINDS = ("failed", "decision_requested", "spend_budget_triaged")


def test_new_run_does_not_replay_resolved_historical_notices(gws):
    hub, worker = gws
    hub.enter()
    tid = kb.create_task(hub.conn, title="Historical notices", assignee="default", runs_on=["linux"])
    hub.round()
    worker.round()
    worker.enter()
    assert wn.pickup(worker.conn, budget=1) == [tid]
    args = dict(task_id=tid, platform="tui", chat_id=CHAT)
    notify.add_notify_sub(worker.conn, **args)
    with kb.write_txn(worker.conn):
        for kind in KINDS:
            kb._append_event(worker.conn, tid, kind, {"reason": "historical resolved notice"})
        kb._append_event(worker.conn, tid, "promoted_manual", {"reason": "resolved"})
    old, cursor, events = notify.claim_unseen_events_for_sub(worker.conn, **args, kinds=KINDS, durable=True)
    assert [e.kind for e in events] == list(KINDS)
    notify.ack_notify_claim(worker.conn, **args, upto=cursor, from_cursor=old)
    worker.round()
    hub.round()
    worker.round()
    for _ in range(3):
        # The dispatcher evicts released foreign copies before the next pickup.
        wn.evict(worker.conn)
        wn.pickup(worker.conn, budget=1)
        with kb.write_txn(worker.conn):
            kb._append_event(worker.conn, tid, "claimed", {"reason": "next run start"})
        _, _, again = notify.claim_unseen_events_for_sub(worker.conn, **args, kinds=KINDS, durable=True)
        assert again == [], "same event UIDs were imported above the acknowledged local cursor"
    worker.tick()
    assert kb.get_task(worker.conn, tid).status == "running"
    assert kb.get_task(worker.conn, tid).current_run_id is not None
    assert notify.claim_unseen_events_for_sub(worker.conn, **args, kinds=KINDS, durable=True)[2] == []


@pytest.mark.parametrize("inflight", [False, True])
def test_subscription_preserves_pending_delivery_and_releases_eviction_on_unsubscribe(gws, inflight):
    hub, worker = gws
    tid = helpers._hub_card(hub, runs_on=["linux"])
    worker.round()
    worker.enter()
    assert wn.pickup(worker.conn, budget=1) == [tid]
    args = dict(task_id=tid, platform="tui", chat_id=CHAT)
    notify.add_notify_sub(worker.conn, **args)
    with kb.write_txn(worker.conn):
        kb._append_event(worker.conn, tid, "decision_requested", {"reason": "still outstanding"})
    if inflight:
        old, cursor, events = notify.claim_unseen_events_for_sub(worker.conn, **args, kinds=KINDS, durable=True)
        assert len(events) == 1
    before = [tuple(r) for r in worker.conn.execute("SELECT * FROM kanban_notify_subs WHERE task_id=?", (tid,))]
    worker.round()
    hub.round()
    worker.round()
    assert tid not in wn.evict(worker.conn)
    assert before == [tuple(r) for r in worker.conn.execute("SELECT * FROM kanban_notify_subs WHERE task_id=?", (tid,))]
    if inflight:
        # A dropped delivery remains retryable; retention must not silently ACK it.
        notify.rewind_notify_cursor(worker.conn, **args, claimed_cursor=cursor, old_cursor=old)
    _, cursor, events = notify.claim_unseen_events_for_sub(worker.conn, **args, kinds=KINDS, durable=True)
    assert [e.kind for e in events] == ["decision_requested"]
    notify.ack_notify_claim(worker.conn, **args, upto=cursor)
    notify.remove_notify_sub(worker.conn, **args)
    assert wn.evict(worker.conn) == [tid]
    assert kb.get_task(worker.conn, tid) is None
