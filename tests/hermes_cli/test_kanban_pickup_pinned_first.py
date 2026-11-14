"""Pickup budget goes to cards only this machine can run (t_01a1d95eabbd2b9d).

worker-1 2026-11-14: t_01a1d6d855a058a2 (``runs_on machine:worker-1``) waited ``budget_exhausted`` for ~6 h. Two
causes. A fresh ``require_model`` card pinned to a quota-walled openai-codex model (t_01a1d882f58d9df9, never run,
so no quota episode) was copied, held ``pinned_quota_wall`` by local dispatch, evicted and copied again every ~18 s
(~700 times), taking the batch each time. And a wide pass of 14 pool cards still left the pinned card exhausted:
pool cards were ranked ahead of it, although other machines can take them.
"""
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_dispatch_skip as kds
from hermes_cli import kanban_pickup_stall as kps
from hermes_cli import kanban_provider_walls as walls
from hermes_cli import kanban_worker_node as wn
from tests.hermes_cli import test_kanban_worker_node as support

svc = support.svc
gws = support.gws
PIN = [f"machine:{support.WORKER}"]
MODEL = ("gpt-6-astra", "openai-codex")
WALL_S = 6 * 86400


@pytest.fixture
def imports(monkeypatch):
    seen: list[str] = []
    real = wn.import_cards

    def counting(conn, replica, ids):
        done = real(conn, replica, ids)
        seen.extend(done)
        return done
    monkeypatch.setattr(wn, "import_cards", counting)
    return seen


@pytest.fixture
def walled_model(monkeypatch):
    until = int(time.time()) + WALL_S
    monkeypatch.setattr(walls, "live_quota_wall",
                        lambda provider, model: until if (model, provider) == MODEL else None)
    return until


def _status(conn, tid):
    t = kb.get_task(conn, tid)
    return t.status if t else None


def test_a_mandatory_pin_on_a_walled_model_with_no_episode_is_not_copied(gws, imports, walled_model):
    hub, worker = gws
    # Both pinned to this machine, so ranking alone cannot save the eligible one: the walled card is ahead of it.
    walled = support._hub_card(hub, title="mandatory pin, walled, never run", priority=1000, runs_on=PIN,
                               model_override=MODEL[0], provider_override=MODEL[1], pin_mandatory=True)
    pinned = support._hub_card(hub, title="pinned, eligible", priority=0, runs_on=PIN)
    worker.round()
    worker.enter()
    # Parity: on a copy, the local dispatcher holds the walled card pre-claim (pinned_quota_wall).
    replica = wn.open_replica(worker.conn)
    assert replica is not None
    try:
        assert wn.import_cards(worker.conn, replica, [walled]) == [walled]
    finally:
        replica.close()
    assert kds.pinned_quota_hold(worker.conn, walled) == (*MODEL, float(walled_model))
    assert wn.evict(worker.conn) == [walled]
    imports.clear()
    # Budget 1: the first tick copies and starts the pinned card, not the walled one.
    worker.tick(max_spawn=1)
    assert imports == [pinned]
    assert _status(worker.conn, pinned) == "running"
    for _ in range(3):  # and no later tick copies the walled card: no import/evict churn
        worker.round()
        worker.tick(max_spawn=2)
    assert imports == [pinned]
    assert _status(worker.conn, walled) is None
    skip = kps.latest(worker.conn, walled)
    assert skip is not None and skip["reason"] == "quota_hold"
    assert skip["detail"].startswith("pinned_quota_wall gpt-6-astra (openai-codex) until ")


def test_a_fresh_pin_with_no_wall_is_copied(gws, imports, monkeypatch):
    hub, worker = gws
    monkeypatch.setattr(walls, "live_quota_wall", lambda provider, model: None)
    tid = support._hub_card(hub, title="mandatory pin, no wall", priority=10, runs_on=PIN,
                            model_override=MODEL[0], provider_override=MODEL[1], pin_mandatory=True)
    worker.round()
    worker.enter()
    assert wn.pickup(worker.conn, budget=1) == [tid]


def test_a_machine_pinned_card_is_picked_ahead_of_more_pool_cards_than_the_budget(gws, imports):
    hub, worker = gws
    pool = [support._hub_card(hub, title=f"pool {i}", priority=1000 - i, runs_on=["linux"]) for i in range(3)]
    pinned = support._hub_card(hub, title="pinned to this machine", priority=0, runs_on=PIN)
    worker.round()
    worker.enter()
    replica = wn.open_replica(worker.conn)
    assert replica is not None
    try:
        picked, skips = wn._evaluate(worker.conn, replica, support.WORKER, mode="all", limit=2,
                                     profile_exists=None, full_detail="pickup batch full this tick")
    finally:
        replica.close()
    assert picked == [pinned, pool[0]]  # this machine's card first, then pool cards in their own order
    assert pinned not in skips
    worker.tick(max_spawn=2)
    assert pinned in imports
    assert _status(worker.conn, pinned) == "running"
