"""Triage authority and recovery identity across real stores and an in-memory Board."""
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_resume_exec as rx
from hermes_cli import kanban_resume_requester as rr
from hermes_cli import kanban_triage_sweep as ts
from tests.hermes_cli import test_kanban_worker_node as worker_node

gws = worker_node.gws
svc = worker_node.svc


def _retained_pair(gws):
    home, worker = gws
    worker.enter()
    # Unrelated local events ensure integer event identities differ across stores.
    for n in range(7):
        kb.create_task(worker.conn, title=f"padding {n}", triage=True)
    home.enter()
    target = kb.create_task(home.conn, title="retained triage", assignee="default", runs_on=["linux"])
    home.round()
    worker.round()
    worker.tick()
    worker.round()
    home.round()
    assert kb.get_task(worker.conn, target).current_run_id is not None
    return home, worker, target


def _park(home, worker, target, *, capped=False):
    worker.enter()
    with kb.write_txn(worker.conn):
        worker.conn.execute("UPDATE tasks SET status='triage', current_run_id=NULL WHERE id=?", (target,))
        if capped:
            for _ in range(ts.TRIAGE_SWEEP_CAP):
                kb._append_event(worker.conn, target, "triage_swept", {})
        kb._append_event(worker.conn, target, "blocked", {"reason": "test park"})
    worker.round()
    home.round()


@pytest.mark.parametrize("home_first", [False, True])
@pytest.mark.parametrize("capped", [False, True])
def test_only_home_sweeps_or_caps_retained_copy(gws, svc, home_first, capped):
    home, worker, target = _retained_pair(gws)
    _park(home, worker, target, capped=capped)
    now = int(time.time()) + ts.TRIAGE_STALE_SECONDS + 1
    # No inter-sweep sync: both see the same eligible idle park.
    for gateway in ((home, worker) if home_first else (worker, home)):
        gateway.enter()
        swept = ts.sweep_stale_triage(gateway.conn, now=now)
        assert (target in swept) == (gateway is home and not capped)
        assert kb.get_task(worker.conn, target).status == "triage"
        assert worker.conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE created_by='triage-sweep'"
        ).fetchone()[0] == 0
    home.round()
    worker.round()
    rows = svc.board(worker_node.BOARD)["rows"]
    supervisors = [r for r in rows["tasks"].values() if r.get("created_by") == "triage-sweep"]
    assert len(supervisors) == int(capped)
    if capped:
        assert supervisors[0]["created_on"] == home.name
    assert rows["tasks"][target]["status"] == ("triage" if capped else "ready")


def test_worker_sweep_does_not_escalate_a_retained_contained_copy(gws):
    """A worker's retained copy has no triage authority, even when its authority is done."""
    home, worker, target = _retained_pair(gws)
    now = int(time.time())
    finished = now - 45 * 60
    worker.enter()
    authority = kb.create_task(worker.conn, title="done authority", assignee="default")
    worker.conn.execute(
        "UPDATE tasks SET status='done', completed_at=?, current_run_id=NULL WHERE id=?",
        (finished, authority),
    )
    home_name = home.name
    payload = {
        "version": 1,
        "target": {"gateway": home_name, "board": "default", "task_id": target},
        "policy": {"version": 1, "authority": {"gateway": home_name, "board": "default", "task_id": authority}},
        "producer_baseline": None,
    }
    with kb.write_txn(worker.conn):
        kb._append_event(worker.conn, target, "contained", payload)
        uid = worker.conn.execute(
            "SELECT uid FROM task_events WHERE task_id=? AND kind='contained' ORDER BY id DESC LIMIT 1",
            (target,),
        ).fetchone()[0]
        worker.conn.execute(
            "UPDATE tasks SET status='triage', containment_episode=?, current_run_id=NULL WHERE id=?",
            (uid, target),
        )
    created = ts.escalate_stranded_contained(worker.conn, now=now)
    assert created == []
    assert worker.conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE created_by='triage-sweep'"
    ).fetchone()[0] == 0
    ts.sweep_stale_triage(worker.conn, now=now + ts.TRIAGE_STALE_SECONDS + 1)
    assert worker.conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE created_by='triage-sweep'"
    ).fetchone()[0] == 0


@pytest.mark.parametrize("legacy", [False, True])
def test_recovery_hold_survives_replica_local_event_ids(gws, legacy):
    home, worker, target = _retained_pair(gws)
    _park(home, worker, target)
    home.enter()
    park = rx._park_event_id(home.conn, target)
    uid = home.conn.execute("SELECT uid FROM task_events WHERE id=?", (park,)).fetchone()[0]
    rec = kb.create_task(home.conn, title="recovery", assignee="default", creator_task_id=target)
    payload = {"park_event_id": park, "recovery_task_id": rec}
    if not legacy:
        payload["park_event_uid"] = uid
    with kb.write_txn(home.conn):
        kb._append_event(home.conn, target, rr.EVENT, payload)
    home.round()
    worker.round()
    # Import the recovery through real pickup without running another dispatcher tick.
    from hermes_cli import kanban_worker_node as wn
    worker.enter()
    wn.pickup(worker.conn, budget=None)
    assert kb.get_task(worker.conn, rec) is not None
    assert rx._park_event_id(worker.conn, target) != park
    for gateway in (home, worker):
        gateway.enter()
        assert rr.open_recovery_card(gateway.conn, target) == rec
        assert target not in ts.sweep_stale_triage(
            gateway.conn, now=int(time.time()) + ts.TRIAGE_STALE_SECONDS + 1)
    home.enter()
    with kb.write_txn(home.conn):
        home.conn.execute("UPDATE tasks SET status='done' WHERE id=?", (rec,))
    home.round()
    worker.round()
    for gateway in (home, worker):
        gateway.enter()
        assert rr.open_recovery_card(gateway.conn, target) is None
    home.enter()
    with kb.write_txn(home.conn):
        home.conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (rec,))
    home.round()
    worker.round()
    # Home releases old linkage on a new park, for both formats. Foreign legacy
    # readers conservatively hold; stable UIDs let them distinguish parks exactly.
    home.enter()
    with kb.write_txn(home.conn):
        kb._append_event(home.conn, target, "blocked", {"reason": "new park"})
    assert rr.open_recovery_card(home.conn, target) is None
    home.round()
    worker.round()
    worker.enter()
    assert rr.open_recovery_card(worker.conn, target) == (rec if legacy else None)
    home.enter()
    with kb.write_txn(home.conn):
        home.conn.execute("UPDATE tasks SET status='done' WHERE id=?", (rec,))
    home.round()
    worker.round()
    for gateway in (home, worker):
        gateway.enter()
        assert rr.open_recovery_card(gateway.conn, target) is None
