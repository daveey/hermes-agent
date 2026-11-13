"""Worker-first placement honours the pool's pickup gate (``kanban_placement_coordination.claim_refusal``). [daveey]

Incident (11-13, MacBook default board): t_01a1d5386cb55abf and t_01a1d54acc8c8e54 quoted the hub CLI's task-list
command in backticks as evidence. ``requirements()`` reads that as a home-only coordination action, so every pool
worker's pickup gate refused both cards, yet creation enrolled them worker-first and ``reevaluate`` reserved a
worker about every 10 s (lease_version 1519 and 642) for 42 and 22 minutes; nobody ever ran them. Creation no longer
enrolls such a card (PR #2339), but one enrolled by a gateway still on older code, or whose body was edited after
enrollment, still is. Entry paths covered: the origin's placement tick (``kanban_worker_placement.reevaluate``) and
the queue reason a Board reply records.
"""
from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_placement as placement
from hermes_cli import kanban_placement_coordination as coord
from hermes_cli import kanban_queue_reason as queue
from hermes_cli import kanban_worker_node as wn
from hermes_cli import kanban_worker_placement as wp
from tests.hermes_cli import test_kanban_worker_node as node_fixtures
from tests.hermes_cli.test_kanban_worker_move_request import PlacementHub
from tests.hermes_cli.test_kanban_worker_node import BOARD, HUB, WORKER

gws = node_fixtures.gws
svc = node_fixtures.svc
POOLS = {"worker_pools": {HUB: [WORKER]}}
# The shape both incident bodies had: the hub CLI's task-list command quoted as evidence, not as a step.
QUOTED = "hermes-hub tasks"
BODY = f"Evidence: `{QUOTED}` had no open card for it, so nobody owns the fix yet."
TICKS = 4


def _mismatch_events(conn, tid):
    return [json.loads(p) for (p,) in conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id", (tid, queue.EVENT))
        if json.loads(p).get("code") == "capability_mismatch"]


def _placement_comments(conn, tid):
    return [b for (b,) in conn.execute(
        "SELECT body FROM task_comments WHERE task_id=? AND author='worker-placement' ORDER BY id", (tid,))]


def _enrolled(origin, body):
    """A card enrolled worker-first directly (an old-code gateway, or a body edited after enrollment)."""
    origin.enter()
    tid = kb.create_task(origin.conn, title="Fix the parser", body=body, assignee="default",
                         placement_policy=wp.POLICY)
    origin.round()
    origin.round()
    return tid


def _ticks(origin, hub, n=TICKS):
    for _ in range(n):
        origin.enter()
        wp.reevaluate(origin.conn, hub, BOARD, HUB, cfg=POOLS)
        wp._queued.clear()  # as if each tick came after the queued keepalive: the throttle is not what stops it


# --- cards already enrolled -------------------------------------------------------------------------------------

def test_an_edited_enrolled_body_still_hits_the_gate():
    """Reachability on main: the incident text outside an "Existing work checked" section still refuses everywhere,
    so an enrolled card carrying it (old-code gateway, body edited after enrollment) is still possible."""
    card = {"title": "Fix the parser", "body": BODY, "placement_policy": wp.POLICY}
    assert coord.claim_refusal(card, "worker-2") == coord.ACTION_REASON
    assert coord.pool_refusal(card, ["worker-2", "worker-3"])["quote"] == QUOTED


def test_an_enrolled_card_every_pickup_gate_refuses_is_repinned_to_its_origin_once(gws, svc, monkeypatch):
    origin, _worker = gws
    monkeypatch.setattr(wp, "local_admission", lambda conn_: True)
    tid = _enrolled(origin, BODY)
    row = dict(origin.conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
    assert coord.claim_refusal(row, WORKER) == coord.ACTION_REASON  # the pickup gate refuses every candidate

    hub = PlacementHub(svc, HUB)
    _ticks(origin, hub)
    assert hub.reserves == [], f"reserved {len(hub.reserves)} times in {TICKS} ticks; no worker can claim it"
    assert [r["runs_on"] for r in hub.repins] == [[f"machine:{HUB}"]]
    local = origin.conn.execute("SELECT placement_policy, runs_on FROM tasks WHERE id=?", (tid,)).fetchone()
    assert (local["placement_policy"], json.loads(local["runs_on"])) == (None, [f"machine:{HUB}"])
    [event] = _mismatch_events(origin.conn, tid)
    assert event["pickup_gate"] == {"reason": coord.ACTION_REASON, "quote": QUOTED}
    assert [c["machine"] for c in event["candidates"]] == [WORKER]
    assert event["candidates"][0]["reasons"] == ["pickup_gate_refused"]
    [comment] = _placement_comments(origin.conn, tid)
    assert f"`{QUOTED}`" in comment and coord.ACTION_REASON in comment and f"machine:{HUB}" in comment

    # The origin runs it; the worker never could.
    origin.tick()
    assert kb.get_task(origin.conn, tid).status == "running"
    assert svc.board(BOARD)["leases"][tid]["holder"] == HUB


def test_a_started_card_every_gate_refuses_is_never_reserved_and_noted_once(gws, svc, monkeypatch):
    """The origin repin covers only unstarted cards; a started one stays put without reserves or a comment loop."""
    origin, _worker = gws
    tid = _enrolled(origin, BODY)
    origin.enter()
    origin.conn.execute("UPDATE tasks SET started_at=1 WHERE id=?", (tid,))
    origin.conn.commit()
    origin.round()  # the edit is acknowledged, so the tick selects the card again
    hub = PlacementHub(svc, HUB)
    _ticks(origin, hub)
    assert hub.reserves == [] and hub.repins == []
    assert len(_mismatch_events(origin.conn, tid)) == 1
    assert len(_placement_comments(origin.conn, tid)) == 1


def test_origin_target_ignores_a_card_not_enrolled_worker_first(gws):
    from hermes_cli import kanban_worker_pickup_gate as gate
    origin, _worker = gws
    tid = _enrolled(origin, BODY)
    row = dict(origin.conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
    assert gate.origin_target(origin.conn, row, HUB, [WORKER]) is not None
    assert gate.origin_target(origin.conn, {**row, "placement_policy": None}, HUB, [WORKER]) is None


def test_a_board_refused_origin_repin_is_not_asked_again_at_that_revision(gws, svc, monkeypatch):
    from hermes_cli import kanban_sync_client as ksc
    origin, _worker = gws
    tid = _enrolled(origin, BODY)

    class Refusing(PlacementHub):
        def repin(self, board, **kw):
            self.repins.append(kw)
            raise ksc.HubError(409, json.dumps({"error": "invalid_pin"}))

    hub = Refusing(svc, HUB)
    _ticks(origin, hub)
    assert hub.reserves == [] and len(hub.repins) == 1
    assert len(_mismatch_events(origin.conn, tid)) == 1


# --- regressions ------------------------------------------------------------------------------------------------

def test_a_card_the_gate_accepts_is_enrolled_and_reserved_as_today(gws, svc, monkeypatch):
    body = "Fix the off-by-one in the parser and add a test."
    args = {"title": "Fix the parser", "body": body}
    assert placement.decide(args, cfg={"worker_pools": {"macbook": ["worker-2"]}},
                            self_gw="macbook")["placement_policy"] == wp.POLICY
    origin, _worker = gws
    tid = _enrolled(origin, body)
    hub = PlacementHub(svc, HUB)
    _ticks(origin, hub, 2)
    assert hub.reserves == [tid, tid] and hub.repins == []
    assert _mismatch_events(origin.conn, tid) == []


@pytest.mark.parametrize("body", [
    "Evidence: hermes-hub's task list had no open card for it.",
    "Checked on hermes-hub earlier: no open card for it.",
])
def test_a_plain_prose_mention_is_not_an_action(body):
    assert coord.requirements(body) == (set(), None)
    assert coord.pool_refusal({"title": "Fix the parser", "body": body}, ["worker-2"]) is None
    result = placement.decide({"title": "Fix the parser", "body": body},
                              cfg={"worker_pools": {"macbook": ["worker-2"]}}, self_gw="macbook")
    assert result["placement_policy"] == wp.POLICY


# --- label-level refusals and the queue reason ------------------------------------------------------------------

CORE = "Land it with hermes-core-wt ship."


def _registry(monkeypatch, machines):
    monkeypatch.setattr(wn, "registry", lambda **kw: {"machines": machines})


def test_label_refusal_needs_every_known_candidate_to_lack_the_labels(monkeypatch):
    card = {"title": "Fix the parser", "body": CORE}
    plain = {"labels": ["pool:worker"]}
    _registry(monkeypatch, {"worker-2": plain, "worker-3": plain})
    gate = coord.pool_refusal(card, ["worker-2", "worker-3"])
    assert gate["reason"] == coord.REASON and gate["quote"] == "capability:core-ship"
    assert gate["machines"]["worker-2"]["missing_labels"] == ["capability:core-ship"]
    # A member that carries the labels takes it, even while held or stale: liveness is not a refusal.
    _registry(monkeypatch, {"worker-2": plain, "worker-3": {"labels": ["capability:core-ship"], "hold": True}})
    assert coord.pool_refusal(card, ["worker-2", "worker-3"]) is None
    assert set(coord.pickup_refusals(card, ["worker-2", "worker-3"])) == {"worker-2"}
    # A member the registry does not know, or no registry at all, is not a refusal.
    _registry(monkeypatch, {"worker-2": plain})
    assert coord.pool_refusal(card, ["worker-2", "worker-3"]) is None
    monkeypatch.setattr(wn, "registry", lambda **kw: None)
    assert coord.pool_refusal(card, ["worker-2"]) is None
    # A card needing no label is never refused, whatever the registry says; nor is any card by an empty pool.
    assert coord.pickup_refusals({"title": "Fix the parser"}, ["worker-2"]) == {}
    assert coord.pool_refusal({"title": "x", "body": BODY}, []) is None


def test_invalid_requires_refuses_everywhere():
    gate = coord.pool_refusal({"title": "x", "requires": "not json"}, ["worker-2"])
    assert gate["reason"] == "invalid worker access requirements"


def test_a_label_mismatch_is_left_to_the_board_and_still_reserved(gws, svc, monkeypatch):
    """The Board owns label routing (its structural ``origin`` return); this gate acts only on text it can't read."""
    origin, _worker = gws
    tid = _enrolled(origin, CORE)
    _registry(monkeypatch, {WORKER: {"labels": ["pool:worker"]}})
    row = dict(origin.conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
    assert coord.pool_refusal(row, [WORKER])["reason"] == coord.REASON
    hub = PlacementHub(svc, HUB)
    _ticks(origin, hub, 2)
    assert hub.reserves == [tid, tid] and hub.repins == []
    assert _mismatch_events(origin.conn, tid) == []


def test_the_queue_reason_never_lists_a_refusing_candidate_as_eligible(gws, monkeypatch):
    origin, _worker = gws
    tid = _enrolled(origin, CORE)
    reply = {"state": "queued", "observation": {"version": 1, "code": "workers_saturated", "candidates": [
        {"machine": "worker-2", "reasons": []}, {"machine": "worker-3", "reasons": ["saturated"]}]}}
    _registry(monkeypatch, {"worker-2": {"labels": ["pool:worker"]},
                            "worker-3": {"labels": ["capability:core-ship"]}})
    row = dict(origin.conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
    refused = coord.pickup_refusals(row, ["worker-2", "worker-3"])
    origin.enter()
    assert queue.record_reply(origin.conn, tid, HUB, reply, ["worker-2", "worker-3"], refused=refused)
    text = queue.describe(queue.latest(origin.conn, tid))
    assert "worker-2: pickup_gate_refused (missing capability:core-ship)" in text, text
    assert "eligible" not in text and "worker-3: saturated" in text, text
