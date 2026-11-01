"""A foreign continuation park has one home-owned requester across Board replicas."""
from __future__ import annotations

import pytest

from hermes_cli import kanban_continuation as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_resume_exec as rx
from hermes_cli import kanban_resume_requester as rr
from tests.hermes_cli import test_kanban_worker_node as worker_node

# Reuse the real multi-gateway fixture rather than mocking ownership or sync.
gws = worker_node.gws
svc = worker_node.svc


@pytest.mark.parametrize("home_first", [False, True])
def test_foreign_park_has_one_board_wide_requester(gws, svc, home_first):
    home, worker = gws
    home.enter()
    target = kb.create_task(home.conn, title="foreign exhaustion", assignee="default", runs_on=["linux"])
    home.round()
    worker.round()
    worker.tick()
    worker.round()  # receive the Board lease's claimed event before parking
    task = kb.get_task(worker.conn, target)
    assert task is not None
    run = task.current_run_id
    assert run is not None
    out = kc.continue_task(worker.conn, target, checkpoint="partial", expected_run_id=run, max_continuations=0)
    assert out["reason_code"] == "continuation_exhausted"
    assert kb.block_task(worker.conn, target, kind="transient", expected_run_id=run,
                         reason="continuation_exhausted: split the work or reset the continuation budget")
    worker.round()
    home.round()
    assert rx.exhaustion_refusal(home.conn, target) is None
    assert rx.exhaustion_refusal(worker.conn, target) is None
    # Each dispatcher can see the same park before eviction; ordering must not matter.
    for gateway in ((home, worker) if home_first else (worker, home)):
        gateway.enter()
        rr.ensure_recovery_cards(gateway.conn)
        rr.ensure_recovery_cards(gateway.conn)
        gateway.round()
    rows = svc.board(worker_node.BOARD)["rows"]["tasks"]
    cards = [r for r in rows.values() if r.get("created_by") == rr.CREATED_BY]
    assert len(cards) == 1, [(r["id"], r["created_on"]) for r in cards]
    assert cards[0]["created_on"] == home.name
    home.enter()
    assert rx.refusal(home.conn, cards[0]["id"], target) is None
    # The writer publishes the blocked event's stable UID for replica readers.
    import json
    link = home.conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind=?", (target, rr.EVENT)
    ).fetchone()
    park = home.conn.execute(
        "SELECT uid FROM task_events WHERE id=?", (rx._park_event_id(home.conn, target),)
    ).fetchone()
    assert json.loads(link[0])["park_event_uid"] == park[0]
    assert rr.open_recovery_card(home.conn, target) == cards[0]["id"]
