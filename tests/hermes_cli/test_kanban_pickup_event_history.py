"""Pickup must carry policy history, not only a noisy diagnostic tail."""
import json
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch_pr_state as prs
from hermes_cli import kanban_continuation as continuation
from hermes_cli import kanban_worker_node as wn
from hermes_cli import kanban_sync_client as ksc
from tests.hermes_cli import test_kanban_worker_node as helpers

gws = helpers.gws
svc = helpers.svc
PR = "https://github.com/example/project/pull/123"


def _owner(hub, monkeypatch, *, park=True, repeat=False):
    hub.enter()
    clock = [int(time.time())]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    tid = kb.create_task(hub.conn, title="Continue existing implementation", assignee="default", runs_on=["linux"])
    assert kb.claim_task(hub.conn, tid) is not None
    run = kb.get_task(hub.conn, tid).current_run_id
    hub.round()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run))
    clock[0] += 1
    kb.add_comment(hub.conn, tid, author="default", body=f"Published {PR}")
    clock[0] += 1
    if park:
        result = continuation.continue_task(hub.conn, tid, checkpoint="Retain original implementation checkpoint",
                                            expected_run_id=run, resume_after_seconds=0)
        assert result["ok"], result
    else:
        # No orderly park is evidence: publication followed by failure must hold.
        with kb.write_txn(hub.conn):
            hub.conn.execute("UPDATE task_runs SET status='crashed', outcome='crashed', ended_at=? WHERE id=?",
                             (clock[0], run))
            hub.conn.execute("UPDATE tasks SET status='ready', current_run_id=NULL, claim_lock=NULL, "
                             "claim_expires=NULL WHERE id=?", (tid,))
    if repeat:
        clock[0] += 1
        kb.add_comment(hub.conn, tid, author="default", body=f"Published again {PR}")
    # Preserve the incident shape: noisy queue/guard events push ALL policy events
    # outside the old 200-row tail, while comments and runs still travel in full.
    with kb.write_txn(hub.conn):
        for _ in range(230):
            kb._append_event(hub.conn, tid, "worker_queue_reason", {"reason": "active_pr"})
    hub.round()
    return tid, clock[0]


@pytest.mark.parametrize("park,repeat,expected", [(True, False, None), (False, False, "active_pr"),
                                                  (True, True, "active_pr")])
@pytest.mark.parametrize("github_state", ["OPEN", None])
def test_pickup_preserves_guard_decision_and_event_identity(gws, monkeypatch, park, repeat, expected, github_state):
    hub, worker = gws
    monkeypatch.setattr(prs, "pr_state", lambda url: github_state)
    # Occupy integer ids on the receiver; event foreign keys must travel by UID.
    worker.enter()
    own = kb.create_task(worker.conn, title="unrelated local run", assignee="default")
    assert kb.claim_task(worker.conn, own) is not None
    worker.round()
    tid, now = _owner(hub, monkeypatch, park=park, repeat=repeat)
    assert prs.active_pr_reason(hub.conn, tid, now) == expected
    worker.round()
    worker.enter()
    assert wn.pickup(worker.conn, budget=1) == [tid]
    assert prs.active_pr_reason(worker.conn, tid, now) == expected
    source = list(hub.conn.execute("SELECT uid,kind,payload FROM task_events WHERE task_id=? ORDER BY id", (tid,)))
    copied = list(worker.conn.execute("SELECT uid,kind,payload FROM task_events WHERE task_id=? ORDER BY id", (tid,)))
    assert [tuple(r) for r in copied] == [tuple(r) for r in source]
    assert not worker.conn.execute("SELECT 1 FROM kanban_sync_outbox").fetchone()
    if park:
        event = worker.conn.execute("SELECT run_id FROM task_events WHERE task_id=? AND kind='continued'", (tid,)).fetchone()
        run = worker.conn.execute("SELECT metadata,uid FROM task_runs WHERE id=?", (event[0],)).fetchone()
        assert json.loads(run[0])["checkpoint"] == "Retain original implementation checkpoint"
        original = hub.conn.execute("SELECT uid FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
        assert run[1] == original[0]
        original_id = hub.conn.execute("SELECT id FROM task_runs WHERE uid=?", (run[1],)).fetchone()[0]
        assert event[0] != original_id


def test_supported_reconcile_repairs_legacy_truncated_copy(gws, svc, monkeypatch):
    hub, worker = gws
    monkeypatch.setattr(prs, "pr_state", lambda url: "OPEN")
    tid, now = _owner(hub, monkeypatch)
    worker.round()
    worker.enter()
    original = wn._changes_for

    def old_tail(replica, task_id, card):
        changes = original(replica, task_id, card)
        events = [c for c in changes if c.table == "task_events"]
        return [c for c in changes if c.table != "task_events"] + list(reversed(events[-200:]))

    with monkeypatch.context() as old:
        old.setattr(wn, "_changes_for", old_tail)
        assert wn.pickup(worker.conn, budget=1) == [tid]
    assert prs.active_pr_reason(worker.conn, tid, now) == "active_pr"
    result = ksc.reconcile(helpers.BOARD, worker.home / "kanban.db", hub=svc.hub(worker.name))
    assert result["restored"]["task_events"] > 0
    assert prs.active_pr_reason(worker.conn, tid, now) is None
    assert not worker.conn.execute("SELECT 1 FROM kanban_sync_outbox WHERE state='pending'").fetchone()
    again = ksc.reconcile(helpers.BOARD, worker.home / "kanban.db", hub=svc.hub(worker.name))
    assert again["restored"] == {}


def test_full_pickup_allows_real_dispatch_claim_from_retained_checkpoint(gws, monkeypatch):
    hub, worker = gws
    monkeypatch.setattr(prs, "pr_state", lambda url: "OPEN")
    tid, _ = _owner(hub, monkeypatch)
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID")
    worker.round()
    worker.tick()
    task = kb.get_task(worker.conn, tid)
    assert task.status == "running"
    run = worker.conn.execute("SELECT metadata FROM task_runs WHERE task_id=? AND outcome='continued'", (tid,)).fetchone()
    assert json.loads(run[0])["checkpoint"] == "Retain original implementation checkpoint"
