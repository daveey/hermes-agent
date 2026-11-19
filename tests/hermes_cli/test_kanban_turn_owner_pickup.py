"""A card that holds a land turn is picked up ahead of aged work, on every dispatch path. [daveey]

Regression (2026-11-15, t_01a1e023229f1073): state PR #3207's owner card t_01a1dcb349282979 held the state land
turn until 16:30Z with an approved, 13/13-green head from 15:12Z. Codex was paced to one run per worker and every
ready card sat at priority 1000 (the cap), so the order came from the age term (25 points per ready minute). The
owner had just re-entered ready from its CI park, so it ranked behind every card that had waited longer, and the
next claim came at 16:36Z, after the turn expired and ``state`` drifted: another refresh, CI round and review.
The turn-urgency observation names exactly these owners, but it only raised them to a priority FLOOR of 900, a
no-op for a card already at 1000.
"""
import json
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_llm_pacing as lp
from hermes_cli import kanban_llm_pacing_dispatch as pd
from hermes_cli import kanban_worker_node as wn
from tests.hermes_cli import kanban_pacing_state as pacing_state
from tests.hermes_cli import test_kanban_worker_node as support

svc = support.svc
gws = support.gws
AGED_S = 90 * 60  # the aged codex queue: ready 90 min, like the 21 cards ahead of #3207's owner at 16:55Z
TURN_LEFT_S = 70 * 60


def observe(home, board, owner, *, now=None, source="state-turn"):
    """The cache the turn-urgency observer (or its hub snapshot import) writes on this host."""
    now = time.time() if now is None else now
    path = home / "cache" / ("kanban-turn-urgency.json" if board == "default"
                             else f"kanban-turn-urgency-{board}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "version": 1, "board": board, "source_revision": "lane=x;turns=y", "observed_at": now - 1,
        "valid_until": now + 100, "owners": [{"task_id": owner, "source": source,
                                              "ref": "refs/land-turn/pr-3207", "head": "c831bb9a98",
                                              "expires_at": now + TURN_LEFT_S}]}))
    return path


@pytest.fixture
def pools(monkeypatch):
    """codex follows ``state['codex']`` (HELD = every account at reserve in ``v2``, OPEN = the pool frees); each
    refresh stores both pools as this machine's dispatcher would. (Since the per-host start limit is gone, a pool
    at reserve is the only pacing state that holds a start.)"""
    state = {"codex": lp.HELD}

    def pace_for(conn, provider, cfg, now):
        pacing_state.store(("openai-codex", state["codex"], now + 3600), ("anthropic", lp.OPEN), now=now)
        return lp.PoolPace(provider, state["codex"] if provider == "openai-codex" else lp.OPEN)
    monkeypatch.setattr(pd, "pace_for", pace_for)
    return state


def _codex(conn, title, *, aged):
    tid = kb.create_task(conn, title=title, assignee="default", priority=1000, runs_on=["linux"],
                         model_override="gpt-5.5", provider_override="openai-codex")
    if aged:
        old = int(time.time()) - AGED_S
        conn.execute("UPDATE tasks SET created_at = ? WHERE id = ?", (old, tid))
        conn.execute("UPDATE task_events SET created_at = ? WHERE task_id = ?", (old, tid))
        conn.commit()
    return tid


def _hub_queue(hub, n):
    """``n`` aged codex cards at the cap, then the owner, just back in ready from its CI park."""
    hub.enter()
    aged = [_codex(hub.conn, f"aged codex {i}", aged=True) for i in range(n)]
    owner = _codex(hub.conn, "land state PR #3207 (holds the turn)", aged=False)
    hub.round()
    return aged, owner


def test_worker_pickup_offers_the_turn_owner_first_when_a_held_pool_frees(gws, pools):
    """Hub-homed owner, picked up by a pool worker (``kanban_worker_node._evaluate``)."""
    hub, worker = gws
    _aged, owner = _hub_queue(hub, 12)
    worker.round()
    worker.enter()
    observe(worker.home, "default", owner)
    assert wn.pickup(worker.conn, budget=1) == []  # codex at reserve: nothing offered, nothing hidden
    pools["codex"] = lp.OPEN  # an account resets: the pool is under reserve again
    assert wn.pickup(worker.conn, budget=1) == [owner]  # RED before: the oldest aged codex card


def test_without_a_valid_observation_the_aged_order_stands(gws, pools):
    hub, worker = gws
    aged, owner = _hub_queue(hub, 3)
    worker.round()
    worker.enter()
    observe(worker.home, "default", owner, now=time.time() - 600)  # expired generation: no urgency at all
    pools["codex"] = lp.OPEN
    picked = wn.pickup(worker.conn, budget=1)
    assert picked and picked[0] in aged


def test_home_dispatch_claims_the_turn_owner_before_aged_work(gws, pools):
    """Owner homed on the dispatching gateway (``kanban_db_dispatch.dispatch_once``), one slot."""
    hub, _worker = gws
    hub.enter()
    pools["codex"] = lp.OPEN
    aged = [_codex(hub.conn, f"aged codex {i}", aged=True) for i in range(5)]
    owner = _codex(hub.conn, "land state PR (holds the turn)", aged=False)
    observe(hub.home, "default", owner)
    hub.tick(max_spawn=1)
    claimed = [r["id"] for r in hub.conn.execute("SELECT id FROM tasks WHERE status = 'running'")]
    assert claimed == [owner], (claimed, aged)
