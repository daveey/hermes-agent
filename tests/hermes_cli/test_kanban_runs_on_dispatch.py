"""Local dispatch applies the Board's runs_on / label eligibility before it claims (MacBook stall, 2026-10-28).

The Mac's review lane held a card with ``runs_on=["machine:hub"]``: every tick the review reservation kept a slot
for it, the Board refused the lease (``labels_mismatch``) and the eligible ready queue never started. Cards this
machine cannot lease are never candidates (counted in ``skipped_ineligible``), a deferred claim never ends the
tick, and the machine named by ``runs_on`` picks the review card up and reviews it.
"""

from __future__ import annotations

import os

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_worker_node as wn
from tests.hermes_cli.kanban_sync_fake_board import FakeService
from tests.hermes_cli.test_kanban_worker_node import BOARD, HUB, MACHINES, WORKER, Gateway


@pytest.fixture
def svc():
    return FakeService(lease_runs=True, machines=MACHINES)  # the Board refuses runs_on it can't satisfy


@pytest.fixture
def gws(tmp_path, monkeypatch, svc):
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: None)
    monkeypatch.setattr(kbd, "_memory_pressure_level", lambda sample=None: "normal")
    monkeypatch.setattr(kbd, "review_dispatch_enabled", lambda: True)
    sync.consume_dispatcher_wake()
    hub = Gateway(tmp_path, HUB, svc, monkeypatch, pickup=False)
    worker = Gateway(tmp_path, WORKER, svc, monkeypatch, pickup="explicit")
    yield hub, worker
    for g in (hub, worker):
        g.conn.close()
    sync.set_lease_client(None)
    wn.set_registry_fetch(None)


def _spawned(res):
    return [tid for tid, _a, _w in res.spawned]


def _review_card_pinned_to_worker(hub, worker):
    """The production path: the hub creates a card for worker-1, worker-1 runs it and asks for review, and the
    card lands back in the hub's review column with runs_on=[machine:worker-1]."""
    hub.enter()
    tid = kb.create_task(hub.conn, title="pinned to worker-1", assignee="default", runs_on=["machine:worker-1"])
    hub.round()
    worker.round()
    assert tid in _spawned(worker.tick())
    worker.enter()
    run_id = worker.conn.execute("SELECT current_run_id FROM tasks WHERE id = ?", (tid,)).fetchone()[0]
    from tests.hermes_cli.kanban_review_fixture import AUTHOR
    from hermes_cli.kanban_review_model import implementer_models
    kb._append_event(worker.conn, tid, "worker_result",
                     {"route_model": AUTHOR, "route_provider": "anthropic"}, run_id=run_id)
    assert kb.request_review(worker.conn, tid, summary="implemented", expected_run_id=run_id)
    assert implementer_models(worker.conn, kb.get_task(worker.conn, tid)) == {AUTHOR}
    worker.round()
    hub.round()
    assert kb.get_task(hub.conn, tid).status == "review"
    return tid


def test_an_ineligible_review_card_does_not_hold_the_last_slot(gws, svc):
    """Acceptance: one free slot, a review card pinned to another machine ahead of an eligible ready card. The
    ready card spawns in that same tick."""
    hub, worker = gws
    review = _review_card_pinned_to_worker(hub, worker)
    hub.enter()
    ready = kb.create_task(hub.conn, title="eligible here", assignee="default")
    res = hub.tick(max_in_progress=1)
    assert _spawned(res) == [ready]
    assert (review, "labels_mismatch") in res.skipped_ineligible
    assert kb.get_task(hub.conn, review).status == "review"
    assert svc.board(BOARD)["leases"].get(review, {}).get("holder") != HUB


def test_dry_run_never_lists_a_card_this_machine_cannot_lease(gws):
    hub, worker = gws
    review = _review_card_pinned_to_worker(hub, worker)
    hub.enter()
    other = kb.create_task(hub.conn, title="worker only", assignee="default", runs_on=["machine:worker-1"])
    res = hub.tick(dry_run=True)
    assert review not in _spawned(res) and other not in _spawned(res)
    assert {t for t, _r in res.skipped_ineligible} >= {review, other}


def test_cli_dispatch_dry_run_json_names_ineligible_cards(gws):
    """The MacBook readback: `hermes kanban dispatch --dry-run --json` lists the refused card, not as spawned."""
    import json

    from hermes_cli import kanban as kcli
    hub, worker = gws
    review = _review_card_pinned_to_worker(hub, worker)
    hub.enter()
    out = json.loads(kcli.run_slash("dispatch --dry-run --json"))
    assert review not in [s["task_id"] for s in out["spawned"]]
    assert {"task_id": review, "reason": "labels_mismatch"} in out["skipped_ineligible"]
    assert out["claim_deferred"] == []


def test_health_probe_does_not_count_ineligible_cards(gws):
    hub, worker = gws
    _review_card_pinned_to_worker(hub, worker)
    hub.enter()
    kb.create_task(hub.conn, title="worker only", assignee="default", runs_on=["machine:worker-1"])
    assert not kbd.has_spawnable_ready(hub.conn)
    assert not kbd.has_spawnable_review(hub.conn)
    kb.create_task(hub.conn, title="mine", assignee="default")
    assert kbd.has_spawnable_ready(hub.conn)


def test_the_pinned_machine_picks_up_and_reviews_the_card(gws, svc):
    """The other half of the stall: worker-1 must take the review it is pinned to, or the card sits forever."""
    hub, worker = gws
    tid = _review_card_pinned_to_worker(hub, worker)
    worker.round()
    res = worker.tick()
    assert tid in _spawned(res)
    lease = svc.board(BOARD)["leases"][tid]
    assert lease["holder"] == WORKER and lease.get("lane", "review") == "review"
    assert worker.conn.execute("SELECT claim_lock FROM tasks WHERE id = ?", (tid,)).fetchone()[0]
    worker.enter()
    run_id = worker.conn.execute("SELECT current_run_id FROM tasks WHERE id = ?", (tid,)).fetchone()[0]
    assert kb.complete_task(worker.conn, tid, summary="approved", expected_run_id=run_id)
    worker.round()
    hub.round()
    assert kb.get_task(hub.conn, tid).status == "done"


def test_a_deferred_review_claim_hands_its_slot_to_ready_work(gws, svc):
    """A review card this machine may lease, but the Board refuses this tick (another gateway holds it): the
    reserved slot goes to the next ready card instead of ending the tick."""
    hub, _ = gws
    hub.enter()
    from tests.hermes_cli.kanban_review_fixture import completed_gateway_source, create_review
    completed_gateway_source(hub)
    review = create_review(hub.conn, title="needs review", assignee="default")
    hub.round()
    svc.board(BOARD)["leases"][review] = {"task_id": review, "holder": "mini", "run_uid": "r-mini", "lane": "review",
                                          "expires": 9e12}
    ready = kb.create_task(hub.conn, title="ready work", assignee="default")
    res = hub.tick(max_in_progress=1)
    assert _spawned(res) == [ready]
    assert [t for t, _r in res.claim_deferred] == [review]
    assert "claim_deferred" in kbd.describe_suppression([res])


def test_stuck_warning_names_deferred_claims_not_profile_health():
    res = kbd.DispatchResult()
    res.claim_deferred.append(("t_x", "labels_mismatch"))
    res.skipped_ineligible.append(("t_y", "labels_mismatch"))
    msg = kbd.stuck_warning(45, [res])
    assert "claim_deferred(labels_mismatch)=1" in msg and "skipped_ineligible(labels_mismatch)=1" in msg
    assert "profile health" not in msg and "Board" in msg
    assert "profile health" in kbd.stuck_warning(45, [kbd.DispatchResult()])


def test_unsynced_boards_are_unaffected(tmp_path, monkeypatch):
    from hermes_cli import kanban_db_connect as kbc
    from tests.hermes_cli import kanban_sync_support as support
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: None)
    monkeypatch.setattr(kbd, "_memory_pressure_level", lambda sample=None: "normal")
    home = support.kanban_home(tmp_path, monkeypatch)
    support.write_routed_sync_config(home, False)
    conn = kbc.connect(home / "kanban.db")
    try:
        tid = kb.create_task(conn, title="plain", assignee="default")
        assert kbd.has_spawnable_ready(conn)
        res = kbd.dispatch_once(conn, spawn_fn=lambda task, ws, board=None: os.getpid())
        assert _spawned(res) == [tid] and res.skipped_ineligible == []
    finally:
        conn.close()
