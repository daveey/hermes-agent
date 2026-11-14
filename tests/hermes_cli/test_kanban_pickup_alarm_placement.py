"""A pickup-stall alarm about machine M never waits on M. [daveey]

t_01a1ce5f6ddc72f6 (2026-11-12): the detector pinned every alarm ``runs_on=[machine:<M>]``. M is the machine taking no
Board work (a registry hold, or no spawn slot: ``budget_exhausted``), so "Pickup held on worker-2" sat ready and
unclaimed for the whole hold, and nobody re-placed the cards its body said to re-place. The alarm now lands on another
machine eligible now (or the hub operator lane), carries M's machine-local evidence in its body, and an open legacy
alarm still pinned to M is re-pinned. Same fake-Board two-gateway harness as ``test_kanban_pickup_stall``.
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


def _worker(name, **kw):
    return {"name": name, "state": "active", "labels": ["linux", *NEED, "capability:hub-write"], "pinned_only": False,
            "max_card_runs": 10, "hold": False, "pickup": "explicit", "metrics": {"active_card_runs": 2}, **kw}


def _hub(**kw):
    return {"name": HUB, "state": "active", "labels": ["linux", *NEED], "pinned_only": False, "max_card_runs": 7,
            "hold": False, "pickup": "explicit", "metrics": {"active_card_runs": 0}, **kw}


def _registry(monkeypatch, *, held=False, other=True, hub=None):
    """The harness re-reads ``support.MACHINES`` on every ``enter``: M (worker-1), optionally an eligible worker-2."""
    me = {**support.MACHINES[WORKER], "hold": held}
    monkeypatch.setitem(support.MACHINES, WORKER, me)
    if other:
        monkeypatch.setitem(support.MACHINES, OTHER, _worker(OTHER))
    if hub is not None:
        monkeypatch.setitem(support.MACHINES, HUB, hub)
    wn._REGISTRY.update(at=None, value=None, retry=False)


def _runs_on(row):
    return json.loads(row["runs_on"]) if row["runs_on"] else None


def _pin(conn, tid):
    return _runs_on(conn.execute("SELECT runs_on FROM tasks WHERE id = ?", (tid,)).fetchone())


# --- the two causes, end to end (both fail on the old machine:<own> pin) ------------------------------------------

def test_a_hold_alarm_lands_on_another_eligible_machine_with_the_evidence(gws, monkeypatch):
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
    assert _runs_on(card) == [f"machine:{OTHER}"]
    assert f"machine:{WORKER}" not in (card["runs_on"] or "")
    body = card["body"]
    # Host-local evidence travels in the body: the skip rows, M's registry entry and replica path.
    assert f"- {a} (" in body and "last skip hold" in body
    [line_b] = [ln for ln in body.splitlines() if ln.startswith(f"- {b} (")]
    assert "last skip hold" in line_b  # each target's own skip row, not only the oldest's
    assert f"Registry entry for {WORKER}" in body and "hold=True" in body
    assert str(wn._replica_path(worker.conn)) in body and "On this host" not in body


def test_a_queue_full_alarm_lands_on_another_eligible_machine(gws, monkeypatch):
    hub, worker = gws
    # A free hub must not win over a capable worker with only one free slot.
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
    assert _runs_on(card) == [f"machine:{OTHER}"]
    assert "last skip budget_exhausted" in card["body"] and "no spawn slot on worker-1" in card["body"]


def test_the_alarm_is_claimed_by_the_hub_while_the_stalled_machine_is_held(tmp_path, monkeypatch, svc):
    """The operator lane really copies and leases it: M stays held, the hub runs the alarm."""
    from hermes_cli import kanban_db_dispatch as kbd
    from hermes_cli import kanban_db_sync as sync
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: None)
    sync.consume_dispatcher_wake()
    hub = support.Gateway(tmp_path, HUB, svc, monkeypatch, pickup="explicit")
    worker = support.Gateway(tmp_path, WORKER, svc, monkeypatch, pickup="true")
    try:
        _registry(monkeypatch, held=True, other=False, hub=_hub())
        tid = stall._pinned(hub)
        hub.round()
        worker.round()
        worker.enter()
        wn.pickup(worker.conn, budget=4)
        stall._age(worker, tid, 900)
        [alarm] = kps.sweep(worker.conn)
        assert _pin(worker.conn, alarm) == [f"machine:{HUB}"]
        worker.round()  # push the alarm
        hub.round()     # the hub's replica sees it
        hub.tick()      # pickup imports it, the Board grants the lease
        assert kb.get_task(hub.conn, alarm).status == "running"
        assert kb.get_task(worker.conn, tid) is None  # the held machine still took nothing
    finally:
        for g in (hub, worker):
            g.conn.close()
        sync.set_lease_client(None)
        wn.set_registry_fetch(None)


def test_an_open_legacy_alarm_pinned_to_the_stalled_machine_is_re_pinned_once(gws, monkeypatch):
    hub, worker = gws
    _registry(monkeypatch, held=True)
    tid = stall._pinned(hub)
    worker.round()
    worker.enter()
    wn.pickup(worker.conn, budget=4)
    stall._age(worker, tid, 900)
    [alarm] = kps.sweep(worker.conn)
    with kb.write_txn(worker.conn):  # what the old code filed
        wn.set_runs_on(worker.conn, alarm, [f"machine:{WORKER}"])
    assert kps.sweep(worker.conn) == [alarm]
    assert _pin(worker.conn, alarm) == [f"machine:{OTHER}"]
    notes = [c.body for c in kb.list_comments(worker.conn, alarm) if "Re-placed" in c.body]
    assert len(notes) == 1 and f"machine:{WORKER}" in notes[0]
    kps.sweep(worker.conn)
    assert len([c for c in kb.list_comments(worker.conn, alarm) if "Re-placed" in c.body]) == 1  # no churn


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


def test_an_alarm_on_a_healthy_machine_stays_when_a_better_one_appears(gws, monkeypatch):
    worker, alarm = _held_alarm(gws, monkeypatch)
    assert _pin(worker.conn, alarm) == [f"machine:{OTHER}"]
    monkeypatch.setitem(support.MACHINES, "worker-3", _worker("worker-3", metrics={"active_card_runs": 0}))
    worker.enter()  # re-read the registry
    assert kps.alarm_placement(WORKER, wn.registry()["machines"], assignee="default")[0] == ["machine:worker-3"]
    kps.sweep(worker.conn)
    assert _pin(worker.conn, alarm) == [f"machine:{OTHER}"] and not _replaced(worker.conn, alarm)


@pytest.mark.parametrize("assignee", [None, ""])
def test_an_alarm_with_no_assignee_is_judged_as_profile_default_and_not_re_pinned(gws, monkeypatch, assignee):
    # t_01a1d32a914ed152: the Board reads a NULL/empty assignee as profile:default; ``profile:None``/``profile:``
    # made the healthy pin look stranded, so it moved to the newly better machine with a Re-placed comment.
    worker, alarm = _held_alarm(gws, monkeypatch)
    with kb.write_txn(worker.conn):
        worker.conn.execute("UPDATE tasks SET assignee = ? WHERE id = ?", (assignee, alarm))
    monkeypatch.setitem(support.MACHINES, "worker-3", _worker("worker-3", metrics={"active_card_runs": 0}))
    worker.enter()
    kps.sweep(worker.conn)
    assert _pin(worker.conn, alarm) == [f"machine:{OTHER}"] and not _replaced(worker.conn, alarm)


def test_a_stranded_alarm_already_on_the_only_choice_is_not_re_commented(gws, monkeypatch):
    worker, alarm = _held_alarm(gws, monkeypatch, other=False, hub=_hub(hold=True))
    assert _pin(worker.conn, alarm) == [f"machine:{HUB}"]  # held too, but not the stalled machine
    kps.sweep(worker.conn)
    kps.sweep(worker.conn)
    assert not _replaced(worker.conn, alarm)


@pytest.mark.parametrize("state", ["running", "claimed"])
def test_a_claimed_or_running_alarm_is_never_re_pinned(gws, monkeypatch, state):
    hub, worker = gws
    _registry(monkeypatch, held=True)
    tid = stall._pinned(hub)
    worker.round()
    worker.enter()
    wn.pickup(worker.conn, budget=4)
    stall._age(worker, tid, 900)
    [alarm] = kps.sweep(worker.conn)
    with kb.write_txn(worker.conn):
        wn.set_runs_on(worker.conn, alarm, [f"machine:{WORKER}"])
        if state == "running":
            worker.conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (alarm,))
        else:
            worker.conn.execute("UPDATE tasks SET claim_lock = 'x' WHERE id = ?", (alarm,))
    kps.sweep(worker.conn)
    assert _pin(worker.conn, alarm) == [f"machine:{WORKER}"]


# --- alarm_placement: one test per condition (each kills its mutant) ---------------------------------------------

NOW = 1_800_000_000_000


def _place(machines, own=WORKER):
    return kps.alarm_placement(own, machines, assignee="default", now_ms=NOW)[0]


def _pool(**entries):
    """The hub is full by default, so it only wins where a test frees it."""
    return {"hub": _hub(last_seen=NOW, metrics={"active_card_runs": 7}), **entries}


@pytest.mark.parametrize("change", [
    {"state": "retired"}, {"hold": True}, {"pickup": None},
    {"labels": ["linux", "capability:file", "capability:terminal", "profile:default", "capability:hub-write"]},
    {"labels": ["linux", *NEED]},  # a worker without hub access
])
def test_a_machine_that_cannot_host_is_skipped(change):
    pool = _pool(**{"worker-2": _worker("worker-2", last_seen=NOW, **change),
                    "worker-3": _worker("worker-3", last_seen=NOW, metrics={"active_card_runs": 9})})
    assert _place(pool) == ["machine:worker-3"]


@pytest.mark.parametrize("change", [
    {"metrics": {"active_card_runs": 0, "drain_active": 1}},
    {"last_seen": NOW - 301_000},  # past kps.FRESH_SECONDS (300)
    {"metrics": {"active_card_runs": 10}},
])
def test_a_machine_not_ready_now_is_skipped(change):
    pool = _pool(**{"worker-2": _worker("worker-2", last_seen=NOW, metrics={"active_card_runs": 0}),
                    "worker-3": _worker("worker-3", last_seen=NOW, metrics={"active_card_runs": 9})})
    pool["worker-2"].update(change)
    assert _place(pool) == ["machine:worker-3"]


def test_the_stalled_machine_itself_is_never_chosen_even_when_it_looks_best():
    pool = _pool(**{WORKER: _worker(WORKER, last_seen=NOW, metrics={"active_card_runs": 0}),
                    "worker-3": _worker("worker-3", last_seen=NOW, metrics={"active_card_runs": 9})})
    assert _place(pool) == ["machine:worker-3"]


def test_the_most_free_slots_wins_then_the_name():
    pool = _pool(**{"worker-3": _worker("worker-3", last_seen=NOW, metrics={"active_card_runs": 8}),
                    "worker-4": _worker("worker-4", last_seen=NOW, metrics={"active_card_runs": 3}),
                    "worker-5": _worker("worker-5", last_seen=NOW, metrics={"active_card_runs": 3})})
    pool["hub"]["metrics"] = {"active_card_runs": 7}  # full
    assert _place(pool) == ["machine:worker-4"]


def test_the_hub_lane_when_no_machine_is_eligible_now():
    # worker-3 has more free slots but is stale: not eligible now, yet it would win the any-host fallback.
    pool = _pool(**{"worker-3": _worker("worker-3", last_seen=NOW - 301_000, metrics={"active_card_runs": 5})})
    pool["hub"]["metrics"] = {"active_card_runs": 7, "drain_active": 1}
    assert _place(pool) == ["machine:hub"]


def test_a_full_machine_is_not_eligible_now_so_the_hub_lane_wins():
    pool = _pool(**{"worker-3": _worker("worker-3", last_seen=NOW, metrics={"active_card_runs": 10})})
    pool["hub"]["metrics"] = {"active_card_runs": 0, "drain_active": 1}
    assert _place(pool) == ["machine:hub"]


def test_an_unheld_full_machine_when_neither_is_eligible():
    pool = _pool(**{"worker-3": _worker("worker-3", last_seen=NOW, metrics={"active_card_runs": 10})})
    pool["hub"]["hold"] = True
    assert _place(pool) == ["machine:worker-3"]


def test_with_no_host_it_waits_on_another_held_machine_hub_first_never_the_stalled_one():
    pool = {"hub": {**_hub(), "hold": True}, "gpu-1": _worker("gpu-1", hold=True),
            WORKER: _worker(WORKER, last_seen=NOW)}
    assert _place(pool) == ["machine:hub"]  # "gpu-1" sorts first: the hub wins by rule, not by name
    del pool["hub"]
    assert _place(pool) == ["machine:gpu-1"]


def test_with_no_other_machine_that_copies_cards_it_takes_default_placement():
    assert _place({"hub": {**_hub(), "pickup": None}, WORKER: _worker(WORKER)}) is None


def test_an_unreadable_registry_sends_a_worker_alarm_to_the_hub_lane_and_a_hub_alarm_off_the_hub():
    assert _place(None) == ["machine:hub"]
    off_hub = _place(None, own=HUB) or []
    assert "capability:hub-write" in off_hub  # the hub carries no hub-write label, so it never matches
    assert not wn.effective_labels(_hub(), HUB) >= set(off_hub)


def test_an_alarm_about_the_hub_lands_on_a_worker():
    pool = _pool(**{"worker-3": _worker("worker-3", last_seen=NOW)})
    assert _place(pool, own=HUB) == ["machine:worker-3"]


@pytest.mark.parametrize("assignee", [None, ""])
def test_a_null_or_empty_assignee_needs_profile_default_like_the_board(assignee):
    pool = _pool(**{"worker-3": _worker("worker-3", last_seen=NOW)})
    assert kps.alarm_placement(WORKER, pool, assignee=assignee, now_ms=NOW)[0] == ["machine:worker-3"]
    off_hub = kps.alarm_placement(HUB, None, assignee=assignee, now_ms=NOW)[0] or []
    assert "profile:default" in off_hub and not {"profile:None", "profile:"} & set(off_hub)


@pytest.mark.parametrize("runs_on,machines,want", [
    ([f"machine:{WORKER}"], None, True),  # pinned to M: stranded even with no registry
    (["machine:worker-3"], None, False),  # unknown without a registry: left alone
    (["machine:worker-3"], {"worker-3": _worker("worker-3")}, False),
    (["machine:worker-3"], {"worker-3": _worker("worker-3", hold=True)}, True),
    (["machine:worker-3"], {}, True),  # left the registry
    (sorted([*NEED, "capability:hub-write"]), {}, False),  # a label set is never stranded
])
def test_stranded(runs_on, machines, want):
    assert kps._stranded(json.dumps(runs_on), WORKER, machines, set(NEED)) is want

