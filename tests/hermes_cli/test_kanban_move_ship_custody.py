"""A move whose reason claims ship custody is refused: ``hermes-core-wt ship`` lands from every host. [daveey]

Regression 2026-11-12 (t_01a1cf350800e915): PR1187's owner, approved and CI-green, moved itself to ``machine:hub``
for "genuine hub task custody" and waited 45+ minutes behind the hub's full lane, while PR1318 landed from
hermes-worker-3 the same hour. The two classifier positives below are the only matches among the 651 real
``moved`` events in the hub board replicas (read-only replay); the negatives are real ship-adjacent move reasons.
"""
from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_move as km
from hermes_cli import kanban_move_ship as kms
from tests.hermes_cli import test_kanban_move as base

svc = base.svc
gws = base.gws

# Real reasons (hub default-board replica, moved events).
PR1187 = ("Final hermes-core-wt ship must run in genuine hub task custody (per custody card t_01a1cdcdd0627aaa); "
          "this run is on hermes-worker-3.")
CUSTODY_CARD = ("Canonical identity authority is on hub: parent is hub-owned and absent from worker-1 execution DB; "
                "only replica exists here. Must verify canonical gate under genuine hub-local landing custody")
NOT_CUSTODY = [
    "Return to existing hub-only implementation worktree and hermes-core-wt ship state to diagnose real CI failure.",
    "Landing continuation needs hub's existing reviewed core-wt ship state and worktree; worker-2 has neither and "
    "SSH to hub is denied. CI is now green, so this is l",
    "ship gate reads only the local board; review t_01a1cce808ae7ea0 is on the hub",
    "Hub CPU-gated (load 34 at 20:26 PST); core fix, tests and ship are portable; live proof stays on t_01a1c.",
    "needs the worker's GPU",
    "",
    None,
]


@pytest.mark.parametrize("reason", [
    PR1187,
    CUSTODY_CARD,
    "the shipping step needs custody of the worktree",       # ship ... custody
    "custody of the review is on the hub, so land there",    # custody ... land
    "needs hub custody",                                     # hub custody alone
    "HUB TASK CUSTODY required",
])
def test_custody_claims_match(reason):
    assert kms.cites_ship_custody(reason)


@pytest.mark.parametrize("reason", NOT_CUSTODY)
def test_other_move_reasons_do_not_match(reason):
    assert not kms.cites_ship_custody(reason)


def test_a_custody_claim_across_a_sentence_break_does_not_match():
    assert not kms.cites_ship_custody("ship is done. The custody card is separate")


def test_refusal_needs_both_the_claim_and_no_host_reason():
    assert "lands from every machine" in (kms.refusal(PR1187, None) or "")
    assert kms.refusal(PR1187, "   ")  # a blank host reason names nothing
    assert kms.refusal(PR1187, "unpushed worktree ~/code/_wt/x exists only on the hub") is None
    assert kms.refusal("needs the worker's GPU", None) is None


def _moved(conn, tid):
    return conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = 'moved'", (tid,)).fetchone()[0]


def test_running_move_for_ship_custody_is_refused_and_leaves_the_run(gws):
    hub, _ = gws
    tid, run_id = base._running_hub_card(hub)
    res = km.move_task(hub.conn, tid, runs_on=["machine:worker-1"], reason=PR1187, checkpoint="c",
                       expected_run_id=run_id)
    assert not res["ok"] and res["reason_code"] == kms.SHIP_CUSTODY_REFUSED, res
    assert "Run ship here" in res["error"]
    assert kb.get_task(hub.conn, tid).status == "running" and _moved(hub.conn, tid) == 0
    assert json.loads(hub.conn.execute("SELECT runs_on FROM tasks WHERE id = ?", (tid,)).fetchone()[0]) == [
        "machine:hub"]


def test_a_named_host_need_still_moves(gws):
    hub, _ = gws
    tid, run_id = base._running_hub_card(hub)
    res = km.move_task(hub.conn, tid, runs_on=["machine:worker-1"], reason=PR1187, checkpoint="c",
                       expected_run_id=run_id, host_reason="unpushed worktree exists only on worker-1")
    assert res["ok"], res
    [ev] = base._events(hub.conn, tid)
    assert ev["host_reason"] == "unpushed worktree exists only on worker-1"


def test_queued_and_reclaim_moves_apply_the_same_rule(gws, monkeypatch):
    hub, _ = gws
    hub.enter()
    queued = kb.create_task(hub.conn, title="q", assignee="default", runs_on=["machine:hub"])
    res = km.move_queued(hub.conn, queued, runs_on=["machine:worker-1"], reason=CUSTODY_CARD)
    assert res["reason_code"] == kms.SHIP_CUSTODY_REFUSED and _moved(hub.conn, queued) == 0
    tid, _run = base._running_hub_card(hub)
    reclaims = []  # a real reclaim signals the fixture run's worker pid (this test process)
    monkeypatch.setattr(kb, "reclaim_task", lambda *a, **k: reclaims.append(a) or True)
    res = km.move_reclaim(hub.conn, tid, runs_on=["machine:worker-1"], reason=PR1187)
    assert res["reason_code"] == kms.SHIP_CUSTODY_REFUSED and kb.get_task(hub.conn, tid).status == "running"
    assert reclaims == [] and _moved(hub.conn, tid) == 0


def test_tool_kanban_move_refuses_with_the_remedy(gws, monkeypatch):
    hub, _ = gws
    tid, run_id = base._running_hub_card(hub)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    from tools import kanban_tools as kt
    out = json.loads(kt._handle_move({"runs_on": ["machine:worker-1"], "reason": PR1187, "checkpoint": "c"}))
    assert "lands from every machine" in json.dumps(out), out
    assert kb.get_task(hub.conn, tid).status == "running"
    out = json.loads(kt._handle_move({"runs_on": ["machine:worker-1"], "reason": PR1187, "checkpoint": "c",
                                      "host_reason": "hub-local evidence file"}))
    assert out["runs_on"] == ["machine:worker-1"], out
