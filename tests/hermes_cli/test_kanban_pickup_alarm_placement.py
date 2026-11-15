"""Pickup alarms queue for capable pool workers, never their provenance source. [daveey]

Placement is independent of transient registry availability and capacity. Healthy legacy pins remain untouched;
stranded pins move to the pool queue. Same fake-Board gateway harness as ``test_kanban_pickup_stall``.
"""
from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_pickup_stall as kps
from hermes_cli import kanban_worker_node as wn
from tests.hermes_cli import test_kanban_pickup_stall as stall
from tests.hermes_cli import test_kanban_worker_node as support

WORKER, HUB = support.WORKER, support.HUB
OTHER = "worker-2"
svc, gws = support.svc, support.gws
NEED = ["capability:file", "capability:kanban", "capability:terminal", "profile:default"]
QUEUE = sorted([*NEED, "capability:hub-write", "pool:worker"])
NOW = 1_800_000_000_000


def _worker(name, **kw):
    return {"name": name, "state": "active", "labels": ["linux", *QUEUE], "pinned_only": False,
            "max_card_runs": 10, "hold": False, "pickup": "explicit", "metrics": {"active_card_runs": 2}, **kw}


def _hub(**kw):
    return {"name": HUB, "state": "active", "labels": ["linux", *NEED], "pinned_only": False, "max_card_runs": 7,
            "hold": False, "pickup": "explicit", "metrics": {"active_card_runs": 0}, **kw}


def _registry(monkeypatch, *, held=False, other=True, hub=None):
    """Gateway.enter re-reads this registry on every pass."""
    monkeypatch.setitem(support.MACHINES, WORKER, _worker(WORKER, hold=held))
    if other:
        monkeypatch.setitem(support.MACHINES, OTHER, _worker(OTHER))
    if hub is not None:
        monkeypatch.setitem(support.MACHINES, HUB, hub)
    wn._REGISTRY.update(at=None, value=None, retry=False)


def _runs_on(row):
    return json.loads(row["runs_on"]) if row["runs_on"] else None


def _pin(conn, tid):
    return _runs_on(conn.execute("SELECT runs_on FROM tasks WHERE id = ?", (tid,)).fetchone())


def test_a_hold_alarm_joins_the_worker_pool_with_the_evidence(gws, monkeypatch):
    hub, worker = gws
    _registry(monkeypatch, held=True)
    a, b = stall._pinned(hub, title="a"), stall._pinned(hub, title="b")
    worker.round()
    worker.enter()
    wn.pickup(worker.conn, budget=4)
    stall._age(worker, a, 900)
    stall._age(worker, b, 700)
    kps.sweep(worker.conn)
    [card] = stall._repairs(worker.conn)
    assert "held" in card["title"].lower()
    assert _runs_on(card) == QUEUE
    assert f"machine:{WORKER}" not in (card["runs_on"] or "")
    body = card["body"]
    assert f"- {a} (" in body and "last skip hold" in body
    [line_b] = [ln for ln in body.splitlines() if ln.startswith(f"- {b} (")]
    assert "last skip hold" in line_b
    assert f"Registry entry for {WORKER}" in body and "hold=True" in body
    assert str(wn._replica_path(worker.conn)) in body and "On this host" not in body
    kps.sweep(worker.conn)
    assert [r["id"] for r in stall._repairs(worker.conn)] == [card["id"]]  # dedupe


def test_a_queue_full_alarm_joins_the_worker_pool(gws, monkeypatch):
    hub, worker = gws
    _registry(monkeypatch, hub=_hub())
    monkeypatch.setitem(support.MACHINES, OTHER, _worker(OTHER, metrics={"active_card_runs": 9}))
    old, new = stall._pinned(hub, title="oldest"), stall._pinned(hub, title="newer")
    worker.round()
    worker.tick(max_spawn=0)
    assert kps.latest(worker.conn, old)["reason"] == "budget_exhausted"
    stall._age(worker, old, 900)
    stall._age(worker, new, 700)
    worker.tick(max_spawn=0)
    [card] = stall._repairs(worker.conn)
    assert "queue" in card["title"].lower()
    assert _runs_on(card) == QUEUE
    assert "last skip budget_exhausted" in card["body"] and "no spawn slot on worker-1" in card["body"]


def test_another_worker_claims_the_alarm_after_the_source_regains_capacity(tmp_path, monkeypatch, svc):
    """Recovery makes the source match every queue label, but only another worker may lease its alarm."""
    from hermes_cli import kanban_db_dispatch as kbd
    from hermes_cli import kanban_db_sync as sync
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: None)
    sync.consume_dispatcher_wake()
    hub = support.Gateway(tmp_path, HUB, svc, monkeypatch, pickup="explicit")
    worker = support.Gateway(tmp_path, WORKER, svc, monkeypatch, pickup="true")
    other = support.Gateway(tmp_path, OTHER, svc, monkeypatch, pickup="explicit")
    try:
        _registry(monkeypatch, held=True, hub=_hub())
        monkeypatch.setitem(support.MACHINES, WORKER, _worker(WORKER, hold=True))
        tid = stall._pinned(hub)
        hub.round()
        worker.round()
        worker.enter()
        wn.pickup(worker.conn, budget=4)
        stall._age(worker, tid, 900)
        [alarm] = kps.sweep(worker.conn)
        assert _pin(worker.conn, alarm) == QUEUE
        worker.round()
        row = svc.board(support.BOARD)["rows"]["tasks"][alarm]
        assert row["created_by"] == kps.DETECTOR and row["created_on"] == WORKER
        assert row["idempotency_key"].startswith(f"pickup-stall:{WORKER}:")
        monkeypatch.setitem(support.MACHINES, WORKER, _worker(WORKER, metrics={"active_card_runs": 0}))
        worker.enter()
        assert set(QUEUE) <= wn.effective_labels(support.MACHINES[WORKER], WORKER)
        assert wn.eligible(row, WORKER, support.MACHINES) == "pickup_stall_source"
        # Exercise pickup without the detector's separate hold-lift resolution sweep archiving the alarm.
        wn.pickup(worker.conn, budget=4)
        assert alarm not in svc.board(support.BOARD)["leases"]
        assert kb.get_task(worker.conn, alarm).status != "running"
        other.round()
        other.tick()
        assert kb.get_task(other.conn, alarm).status == "running"
        assert svc.board(support.BOARD)["leases"][alarm]["holder"] == OTHER
    finally:
        for g in (hub, worker, other):
            g.conn.close()
        sync.set_lease_client(None)
        wn.set_registry_fetch(None)


def _held_alarm(gws, monkeypatch, **registry):
    hub, worker = gws
    _registry(monkeypatch, held=True, **registry)
    tid = stall._pinned(hub)
    worker.round()
    worker.enter()
    wn.pickup(worker.conn, budget=4)
    stall._age(worker, tid, 900)
    [alarm] = kps.sweep(worker.conn)
    return worker, alarm


def _replaced(conn, alarm):
    return [c.body for c in kb.list_comments(conn, alarm) if "Re-placed" in c.body]


def test_an_open_legacy_alarm_pinned_to_the_stalled_machine_is_re_pinned_once(gws, monkeypatch):
    worker, alarm = _held_alarm(gws, monkeypatch)
    with kb.write_txn(worker.conn):
        wn.set_runs_on(worker.conn, alarm, [f"machine:{WORKER}"])
    assert kps.sweep(worker.conn) == [alarm]
    assert _pin(worker.conn, alarm) == QUEUE
    notes = _replaced(worker.conn, alarm)
    assert len(notes) == 1 and f"machine:{WORKER}" in notes[0]
    kps.sweep(worker.conn)
    assert len(_replaced(worker.conn, alarm)) == 1


def test_an_alarm_on_a_healthy_machine_stays_when_a_better_one_appears(gws, monkeypatch):
    worker, alarm = _held_alarm(gws, monkeypatch)
    with kb.write_txn(worker.conn):  # explicitly seed the healthy legacy pin under test
        wn.set_runs_on(worker.conn, alarm, [f"machine:{OTHER}"])
    monkeypatch.setitem(support.MACHINES, "worker-3", _worker("worker-3", metrics={"active_card_runs": 0}))
    worker.enter()
    assert kps.alarm_placement(WORKER, wn.registry()["machines"], assignee="default")[0] == QUEUE
    kps.sweep(worker.conn)
    assert _pin(worker.conn, alarm) == [f"machine:{OTHER}"] and not _replaced(worker.conn, alarm)


@pytest.mark.parametrize("assignee", [None, ""])
def test_an_alarm_with_no_assignee_is_judged_as_profile_default_and_not_re_pinned(gws, monkeypatch, assignee):
    worker, alarm = _held_alarm(gws, monkeypatch)
    with kb.write_txn(worker.conn):
        wn.set_runs_on(worker.conn, alarm, [f"machine:{OTHER}"])
        worker.conn.execute("UPDATE tasks SET assignee = ? WHERE id = ?", (assignee, alarm))
    monkeypatch.setitem(support.MACHINES, "worker-3", _worker("worker-3", metrics={"active_card_runs": 0}))
    worker.enter()
    kps.sweep(worker.conn)
    assert _pin(worker.conn, alarm) == [f"machine:{OTHER}"] and not _replaced(worker.conn, alarm)


def test_a_queued_alarm_is_not_re_commented_when_no_worker_is_available(gws, monkeypatch):
    worker, alarm = _held_alarm(gws, monkeypatch, other=False, hub=_hub(hold=True))
    assert _pin(worker.conn, alarm) == QUEUE
    kps.sweep(worker.conn)
    kps.sweep(worker.conn)
    assert not _replaced(worker.conn, alarm)


def test_a_hold_alarm_resolves_when_the_source_hold_lifts(gws, monkeypatch):
    worker, alarm = _held_alarm(gws, monkeypatch)
    monkeypatch.setitem(support.MACHINES, WORKER, {**support.MACHINES[WORKER], "hold": False})
    worker.enter()
    kps.sweep(worker.conn)
    assert kb.get_task(worker.conn, alarm).status == "archived"
    assert len(stall._repairs(worker.conn)) == 1


@pytest.mark.parametrize("state", ["running", "claimed"])
def test_a_claimed_or_running_alarm_is_never_re_pinned(gws, monkeypatch, state):
    worker, alarm = _held_alarm(gws, monkeypatch)
    with kb.write_txn(worker.conn):
        wn.set_runs_on(worker.conn, alarm, [f"machine:{WORKER}"])
        if state == "running":
            worker.conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (alarm,))
        else:
            worker.conn.execute("UPDATE tasks SET claim_lock = 'x' WHERE id = ?", (alarm,))
    kps.sweep(worker.conn)
    assert _pin(worker.conn, alarm) == [f"machine:{WORKER}"]
    assert not _replaced(worker.conn, alarm)


@pytest.mark.parametrize("machines", [
    None, {},
    {WORKER: _worker(WORKER)},
    {HUB: _hub()},
    {OTHER: _worker(OTHER)},
    {OTHER: _worker(OTHER, hold=True)},
    {OTHER: _worker(OTHER, state="retired")},
    {OTHER: _worker(OTHER, pickup=None)},
    {OTHER: _worker(OTHER, labels=["linux"])},
    {OTHER: _worker(OTHER, last_seen=NOW - 301_000)},
    {OTHER: _worker(OTHER, metrics={"active_card_runs": 10})},
    {OTHER: _worker(OTHER, metrics={"drain_active": 1})},
    {OTHER: _worker(OTHER), "worker-3": _worker("worker-3", metrics={"active_card_runs": 0})},
])
@pytest.mark.parametrize("own", [WORKER, HUB])
def test_alarm_placement_is_a_stable_worker_queue_regardless_of_registry(machines, own):
    runs_on, why = kps.alarm_placement(own, machines, assignee="default", now_ms=NOW)
    assert runs_on == QUEUE
    assert why
    assert not any(label.startswith("machine:") for label in runs_on)


@pytest.mark.parametrize("assignee", [None, ""])
def test_a_null_or_empty_assignee_needs_profile_default_like_the_board(assignee):
    assert kps.alarm_placement(WORKER, None, assignee=assignee, now_ms=NOW)[0] == QUEUE


def test_queue_placement_preserves_explicit_profile_and_requirements():
    runs_on, _ = kps.alarm_placement(WORKER, None, assignee="reviewer", requires=("terminal", "file"))
    assert runs_on == sorted(["capability:terminal", "capability:file", "capability:hub-write",
                              "pool:worker", "profile:reviewer"])


@pytest.mark.parametrize("runs_on,machines,want", [
    ([f"machine:{WORKER}"], None, True),
    (["machine:worker-3"], None, False),
    (["machine:worker-3"], {"worker-3": _worker("worker-3")}, False),
    (["machine:worker-3"], {"worker-3": _worker("worker-3", hold=True)}, True),
    (["machine:worker-3"], {}, True),
    (QUEUE, {}, False),
])
def test_stranded(runs_on, machines, want):
    assert kps._stranded(json.dumps(runs_on), WORKER, machines, set(NEED)) is want
