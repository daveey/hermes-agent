"""The hub judges every foreign card it would run before it claims it. [daveey]

11-12 10:55Z: worker-4 (core older than the hub admission gate abec81ec81) created t_01a1cf65a941bf43 with
``runs_on=["machine:hub"]`` and no ``hub_admission`` verdict; at 10:58Z the hub's pickup copied it and claimed it.
Admission ran only on the creating host, so any creator on old code (or any path without the gate) put work on
the hub unjudged. Pickup now judges a hub-bound foreign card that carries no admit verdict: admitted runs as
before, denied is re-placed on the worker queue (``moved`` event, never claimed here), and an unavailable judge
leaves it on the Board for the next tick (fail closed). The dispatcher refuses a hub-bound foreign copy without an
admit verdict too, so no other import path can claim one.
"""
from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_hub_admission as judge
from hermes_cli import kanban_hub_admission_pickup as hap
from hermes_cli import kanban_placement as kp
from hermes_cli import kanban_worker_node as wn
from tests.hermes_cli import hub_judge_support
from tests.hermes_cli._placement_helpers import seen_now
from tests.hermes_cli.kanban_sync_fake_board import FakeService
from tests.hermes_cli.test_kanban_worker_node import BOARD, HUB, WORKER, Gateway

hub_judge = hub_judge_support.hub_judge

POOL = "pool:worker"
MACHINES = {
    HUB: {"name": HUB, "state": "active", "labels": ["linux"], "pinned_only": False, "max_card_runs": None,
          "hold": False, "pickup": "explicit"},
    WORKER: {"name": WORKER, "state": "active", "labels": ["linux", POOL], "pinned_only": False,
             "max_card_runs": 12, "hold": False, "pickup": "all"},
}
CFG = {"worker_queue": {HUB: POOL}, "worker_pools": {HUB: [WORKER]}}


@pytest.fixture
def gws(tmp_path, monkeypatch):
    svc = FakeService(lease_runs=True, machines=MACHINES, recover=True)  # a real Board serves recovery
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: None)
    monkeypatch.setattr(kp, "config", lambda: CFG)
    sync.consume_dispatcher_wake()
    hub = Gateway(tmp_path, HUB, svc, monkeypatch, pickup="explicit")
    worker = Gateway(tmp_path, WORKER, svc, monkeypatch, pickup="true")
    for g in (hub, worker):
        g.enter = _registry_enter(g)
    yield hub, worker, svc
    for g in (hub, worker):
        g.conn.close()
    sync.set_lease_client(None)
    wn.set_registry_fetch(None)


def _registry_enter(g):
    base = g.enter

    def enter():
        out = base()
        wn.set_registry_fetch(lambda: {"me": g.name, "machines": seen_now(MACHINES.values())})
        return out
    return enter


def _worker_card(worker, *, title="state CI red: fix the pre-push test", body="Portable work (git clone + CI).",
                 runs_on=("machine:hub",), audit=None):
    """A card an old-core worker created for the hub: runs_on set, no hub_admission anywhere."""
    worker.enter()
    tid = kb.create_task(worker.conn, title=title, body=body, assignee="default", runs_on=list(runs_on),
                         **({"placement_audit": audit} if audit else {}))
    worker.round()
    return tid


def _hub_tick(hub):
    hub.round()
    return hub.tick()


def _events(conn, tid, kind):
    return [json.loads(p or "null") for (p,) in conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id", (tid, kind))]


def _claimed_on_hub(svc, tid):
    return (svc.board(BOARD)["leases"].get(tid) or {}).get("holder") == HUB


def test_unjudged_foreign_hub_card_is_judged_and_a_deny_is_never_claimed(gws, hub_judge):
    hub, worker, svc = gws
    hub_judge.admit_next = False
    tid = _worker_card(worker)
    _hub_tick(hub)
    assert not _claimed_on_hub(svc, tid)
    assert [c["card"]["title"] for c in hub_judge.calls] == ["state CI red: fix the pre-push test"]
    [verdict] = _events(hub.conn, tid, "hub_admission")
    assert verdict["admit"] is False and verdict["entry"] == "pickup" and verdict["runs_on"] == ["machine:hub"]
    [moved] = _events(hub.conn, tid, "moved")
    assert moved["previous_runs_on"] == ["machine:hub"] and moved["runs_on"][0] == POOL
    assert "hub admission denied" in moved["reason"] and moved["hub_admission"]["why"] == "fake judge denies"
    hub.round()
    worker.round()
    # The Board now carries the pool placement, so a pool worker runs it.
    assert json.loads(svc.board(BOARD)["rows"]["tasks"][tid]["runs_on"])[0] == POOL
    worker.tick()
    assert (svc.board(BOARD)["leases"].get(tid) or {}).get("holder") == WORKER
    # The hub never judges it again.
    _hub_tick(hub)
    assert len(hub_judge.calls) == 1
    from hermes_cli import kanban_pickup_stall as kps
    assert (kps.latest(hub.conn, tid) or {}).get("detail", "").startswith("hub admission denied: fake judge denies")


def test_a_deny_with_no_worker_queue_records_the_verdict_and_is_never_claimed_or_rejudged(gws, hub_judge,
                                                                                       monkeypatch):
    hub, worker, svc = gws
    monkeypatch.setattr(kp, "config", lambda: {"worker_pools": {HUB: [WORKER]}})  # no worker_queue label
    hub_judge.admit_next = False
    tid = _worker_card(worker)
    _hub_tick(hub)
    assert not _claimed_on_hub(svc, tid)
    [verdict] = _events(hub.conn, tid, "hub_admission")
    assert verdict["admit"] is False and _events(hub.conn, tid, "moved") == []
    assert json.loads(hub.conn.execute("SELECT runs_on FROM tasks WHERE id = ?", (tid,)).fetchone()[0]) == [
        "machine:hub"]
    from hermes_cli import kanban_pickup_stall as kps
    assert "no worker queue fits" in (kps.latest(hub.conn, tid) or {}).get("detail", "")
    for _ in range(4):  # pushed, evicted, then seen on the Board with its pickup deny: skipped, not re-judged
        _hub_tick(hub)
        assert not _claimed_on_hub(svc, tid)
    # The replica learns the hub's own verdict one round after the push, so a copy evicted in between can be
    # judged once more (a cached verdict in production: no model call). After that it is never judged again.
    calls = len(hub_judge.calls)
    assert calls <= 2
    for _ in range(3):
        _hub_tick(hub)
    assert len(hub_judge.calls) == calls and not _claimed_on_hub(svc, tid)
    # A new hub placement is a new question: the hub judges it again.
    worker.enter()
    with kb.write_txn(worker.conn):
        wn.set_runs_on(worker.conn, tid, ["machine:hub", "linux"])
    worker.round()
    hub_judge.admit_next = True
    for _ in range(3):
        _hub_tick(hub)
    assert len(hub_judge.calls) > calls


def test_screen_makes_no_model_call_inside_a_write_transaction(gws, hub_judge):
    hub, worker, _svc = gws
    tid = _worker_card(worker)
    hub.round()
    hub.enter()
    replica = wn.open_replica(hub.conn)
    assert replica is not None
    try:
        hub.conn.execute("BEGIN IMMEDIATE")
        copy, plans, skips = hap.screen(hub.conn, replica, HUB, [tid], cfg=CFG, machines=MACHINES)
        hub.conn.execute("ROLLBACK")
    finally:
        replica.close()
    assert (copy, plans, hub_judge.calls) == ([], {}, [])
    assert skips[tid] == ("other", hap.UNAVAILABLE)


def test_target_follows_the_creation_time_deny_route(monkeypatch):
    worker = {**MACHINES[WORKER], "labels": [*MACHINES[WORKER]["labels"], "capability:hub-write"]}
    hub_write = {**MACHINES, WORKER: worker}
    wn.set_registry_fetch(lambda: {"me": HUB, "machines": seen_now(hub_write.values())})
    try:
        fields = {"title": "t", "body": "b", "assignee": "default", "workspace_kind": "scratch",
                  "requires": ["terminal"], "skills": None, "board": "default"}
        assert hap._target(fields, HUB, CFG, {})[0] == POOL
        assert hap._target({**fields, "assignee": "not-allowlisted"}, HUB, CFG, {}) is None  # structural
        assert hap._target({**fields, "workspace_kind": "worktree"}, HUB, CFG, {}) is None
        assert hap._target(fields, HUB, {}, {}) is None  # no worker_queue for the hub
        assert "capability:hub-write" not in hap._target(fields, HUB, CFG, {})
        monkeypatch.setattr(kp, "portable_reason", lambda args, cfg=None: "home gateway marker")
        assert "capability:hub-write" in hap._target(fields, HUB, CFG, {})
        # No live machine holds pool:worker + capability:hub-write: the deny route falls back to worker-first.
        wn.set_registry_fetch(lambda: {"me": HUB, "machines": seen_now(MACHINES.values())})
        assert hap._target(fields, HUB, CFG, {}) is None
    finally:
        wn.set_registry_fetch(None)


def test_refusals_skip_local_and_legacy_cards():
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT, payload TEXT)")
    rows = [{"id": "legacy", "created_on": None, "runs_on": '["machine:hub"]'},
            {"id": "own", "created_on": HUB, "runs_on": '["machine:hub"]'},
            {"id": "foreign", "created_on": WORKER, "runs_on": '["machine:hub"]'},
            {"id": "pooled", "created_on": WORKER, "runs_on": f'["{POOL}"]'}]
    assert hap.refusals(conn, rows, HUB, MACHINES, CFG) == {"foreign": hap.MISSING}
    conn.execute("INSERT INTO task_events (task_id, kind, payload) VALUES ('foreign', 'hub_admission', ?)",
                 (json.dumps({"admit": True}),))
    assert hap.refusals(conn, rows, HUB, MACHINES, CFG) == {}


def test_an_admitted_card_is_claimed_and_the_verdict_recorded(gws, hub_judge):
    hub, worker, svc = gws
    tid = _worker_card(worker, title="restart the hub gateway", body="needs the hub's live process")
    _hub_tick(hub)
    assert _claimed_on_hub(svc, tid)
    [verdict] = _events(hub.conn, tid, "hub_admission")
    assert verdict["admit"] is True and verdict["entry"] == "pickup"
    assert _events(hub.conn, tid, "moved") == []


def test_a_card_already_admitted_at_creation_is_not_judged_again(gws, hub_judge):
    hub, worker, svc = gws
    admitted = {"hub_admission": {"admit": True, "need": "hub_chrome", "evidence": "x", "why": "ok"}}
    tid = _worker_card(worker, audit=admitted)
    _hub_tick(hub)
    assert hub_judge.calls == [] and _claimed_on_hub(svc, tid)


def test_an_unavailable_judge_leaves_the_card_on_the_board(gws, monkeypatch):
    hub, worker, svc = gws
    calls = []
    monkeypatch.setattr(judge, "admit", lambda card, hub_reason=None: calls.append(card) or judge.Verdict())
    first = _worker_card(worker, title="a")
    second = _worker_card(worker, title="b")
    _hub_tick(hub)
    for tid in (first, second):
        assert not _claimed_on_hub(svc, tid)
        assert kb.get_task(hub.conn, tid) is None  # not copied: nothing to evict, nothing recorded
    assert len(calls) == 1  # one outage per tick, not one 30 s timeout per card
    from hermes_cli import kanban_pickup_stall as kps
    for tid in (first, second):
        assert "hub admission unavailable" in (kps.latest(hub.conn, tid) or {}).get("detail", ""), tid
    # The judge comes back: the next tick admits and claims.
    monkeypatch.setattr(judge, "admit", hub_judge_support.FakeJudge())
    _hub_tick(hub)
    assert _claimed_on_hub(svc, first) and _claimed_on_hub(svc, second)


def test_a_card_a_worker_can_claim_is_not_judged(gws, hub_judge):
    hub, worker, _svc = gws
    _worker_card(worker, runs_on=("linux",))
    _hub_tick(hub)
    assert hub_judge.calls == []


def test_dispatch_refuses_a_hub_bound_foreign_copy_without_an_admit_verdict(gws, hub_judge, monkeypatch):
    """A copy that arrived by any other path (an older pickup, a refill) is never claimed unjudged."""
    hub, worker, svc = gws
    tid = _worker_card(worker)
    hub.round()
    hub.enter()
    replica = wn.open_replica(hub.conn)
    assert replica is not None
    try:
        assert wn.import_cards(hub.conn, replica, [tid]) == [tid]
    finally:
        replica.close()
    refused = wn.local_refusals(hub.conn, "ready")
    assert refused.get(tid) == hap.MISSING
    real = wn.pickup
    monkeypatch.setattr(wn, "pickup", lambda *a, **k: [])  # the copy stays: only the dispatcher decides
    hub.tick()
    assert not _claimed_on_hub(svc, tid)
    assert kb.get_task(hub.conn, tid).status == "ready"
    # With pickup back, the copy is evicted, re-copied through the gate, judged, then claimed.
    monkeypatch.setattr(wn, "pickup", real)
    hub.round()  # push what the refused tick wrote, so the copy can be evicted
    hub.round()  # ... and let the replica take that push: a lagging replica never re-copies it (replica_lag)
    hub.tick()
    assert _claimed_on_hub(svc, tid) and _events(hub.conn, tid, "hub_admission")[-1]["admit"] is True, (
        kb.get_task(hub.conn, tid), hub.conn.execute("SELECT kind, payload FROM task_events WHERE task_id = ?",
                                                     (tid,)).fetchall(), wn.local_refusals(hub.conn, "ready"))
    # The worker's own card on the hub is not affected, nor a hub-created hub card.
    own = kb.create_task(hub.conn, title="hub's own", assignee="default", runs_on=["machine:hub"])
    assert own not in wn.local_refusals(hub.conn, "ready")


def test_hub_bound_rules():
    assert hap.hub_bound(["machine:hub"], HUB, CFG, MACHINES)
    assert hap.hub_bound(["machine:hermes-hub"], HUB, CFG, MACHINES)
    assert not hap.hub_bound(["linux"], HUB, CFG, MACHINES)
    assert not hap.hub_bound([POOL], HUB, CFG, MACHINES)
    assert not hap.hub_bound(["machine:worker-1"], HUB, CFG, MACHINES)
    assert not hap.hub_bound(None, HUB, CFG, MACHINES)
    assert not hap.hub_bound(["machine:hub"], WORKER, CFG, MACHINES)  # only the hub gates its own pickup
    gone = {**MACHINES, WORKER: {**MACHINES[WORKER], "state": "retired"}}
    assert hap.hub_bound(["linux"], HUB, CFG, gone)  # no active worker carries the labels
    assert not hap.hub_bound(["machine:mini"], HUB, CFG, MACHINES)  # pinned to another (unregistered) machine


def test_screen_on_a_non_hub_gateway_copies_everything_unjudged(gws, hub_judge):
    _hub, worker, _svc = gws
    tid = _worker_card(worker)
    worker.enter()
    wn.set_registry_fetch(lambda: pytest.fail("a non-hub pickup must not read the registry"))
    replica = wn.open_replica(worker.conn)
    assert replica is not None
    try:
        out = hap.screen(worker.conn, replica, WORKER, [tid], cfg=CFG)
    finally:
        replica.close()
    assert out == ([tid], {}, {}) and hub_judge.calls == []


def test_an_admitted_pickup_is_not_reported_as_denied(gws, hub_judge):
    hub, worker, _svc = gws
    tid = _worker_card(worker)
    hub.round()
    hub.enter()
    replica = wn.open_replica(hub.conn)
    assert replica is not None
    try:
        copy, plans, skips = hap.screen(hub.conn, replica, HUB, [tid], cfg=CFG, machines=MACHINES)
        imported = wn.import_cards(hub.conn, replica, copy)
    finally:
        replica.close()
    assert plans[tid]["audit"]["admit"] is True and skips == {}
    assert hap.apply(hub.conn, HUB, plans, imported) == {}
    assert _events(hub.conn, tid, "hub_admission")[-1]["admit"] is True


def test_verdict_reads_events_after_the_created_payload():
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT, payload TEXT)")
    conn.execute("INSERT INTO task_events (task_id, kind, payload) VALUES ('t', 'created', ?)",
                 (json.dumps({"hub_admission": {"admit": True}}),))
    assert hap.verdict(conn, "t") == {"admit": True}
    conn.execute("INSERT INTO task_events (task_id, kind, payload) VALUES ('t', 'hub_admission', ?)",
                 (json.dumps({"admit": False, "entry": "move"}),))
    assert hap.verdict(conn, "t")["admit"] is False
    assert hap.verdict(conn, "other") is None


HOME_SKIP = ("hub admission denied: fake judge denies; kept home (needs home coordination action), "
             "left for its creator")


def _screen_and_copy(hub, tid):
    hub.round()
    hub.enter()
    replica = wn.open_replica(hub.conn)
    assert replica is not None
    try:
        copy, plans, skips = hap.screen(hub.conn, replica, HUB, [tid], cfg=CFG, machines=MACHINES)
        imported = wn.import_cards(hub.conn, replica, copy)
    finally:
        replica.close()
    return copy, plans, skips, imported


def test_apply_skip_text_names_the_home_reason_when_no_queue_takes_it(gws, hub_judge):
    hub, worker, _svc = gws
    hub_judge.admit_next = False
    tid = _worker_card(worker)
    _copy, plans, _skips, imported = _screen_and_copy(hub, tid)
    audit = {**plans[tid]["audit"], "home": "needs home coordination action"}
    assert hap.apply(hub.conn, HUB, {tid: {"audit": audit, "target": None}}, imported) == {tid: ("other", HOME_SKIP)}
    assert _events(hub.conn, tid, "moved") == []


def test_denied_text_without_home_or_once_re_placed():
    audit = {"admit": False, "why": "w"}
    assert hap._denied(audit, False) == "hub admission denied: w; no worker queue fits, left for its creator"
    assert hap._denied({**audit, "home": "h"}, True) == "hub admission denied: w"


def test_a_re_pickup_prior_verdict_skip_keeps_the_home_reason(gws, hub_judge, monkeypatch):
    hub, worker, _svc = gws
    tid = _worker_card(worker)
    prior = {"admit": False, "why": "fake judge denies", "entry": hap.ENTRY, "runs_on": ["machine:hub"],
             "home": "needs home coordination action"}
    monkeypatch.setattr(hap, "verdict", lambda conn, task_id: prior if task_id == tid else None)
    copy, plans, skips, _imported = _screen_and_copy(hub, tid)
    assert (copy, plans, hub_judge.calls) == ([], {}, [])
    assert skips[tid] == ("other", HOME_SKIP)
    del prior["home"]
    assert _screen_and_copy(hub, tid)[2][tid][1].endswith("; no worker queue fits, left for its creator")
