"""One pickup pass spends a paced provider's headroom once (t_01a1e14333fada79).

worker-3 2026-11-15 ~21:30Z: anthropic paced at limit 1, running 0. Each candidate was judged against the
provider's running count alone, so every anthropic card looked runnable and the pass copied a full batch of 10.
Local dispatch started one and pace-held nine, which were evicted and copied again the next tick. A card pinned
to the machine below that batch was recorded ``budget_exhausted`` although spawn slots were free and the real
gate was pacing, so the pickup-stall alarm filed "Pickup queue full".
"""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_llm_pacing as lp
from hermes_cli import kanban_llm_pacing_dispatch as pd
from hermes_cli import kanban_worker_node as wn
from tests.hermes_cli import test_kanban_worker_node as support
from tests.hermes_cli import test_kanban_pace_reroute as routing_support

svc = support.svc
gws = support.gws
board = routing_support.board
pool = routing_support.pool
strong_pattern = routing_support.strong_pattern
PIN = [f"machine:{support.WORKER}"]
BATCH = 10


@pytest.fixture
def anthropic_paced(monkeypatch):
    """anthropic PACED with no recent concurrency: admit limit 1 for a ready card (``lp.admit_limit``)."""
    monkeypatch.setattr(pd, "pace_for", lambda conn, provider, cfg, now: lp.PoolPace(
        provider, lp.PACED, 0.5) if provider == "anthropic" else lp.PoolPace(provider, lp.OPEN, lp.OPEN_RAMP_FACTOR))
    monkeypatch.setattr(pd, "avg_running", lambda provider, now, cfg: 0.0)


def _card(hub, title, priority, runs_on, provider="anthropic"):
    model = "claude-sonnet-5-5" if provider == "anthropic" else "gpt-5.5"
    return support._hub_card(hub, title=title, priority=priority, runs_on=runs_on, model_override=model,
                             provider_override=provider)


def _evaluate(worker, limit=BATCH):
    replica = wn.open_replica(worker.conn)
    assert replica is not None
    try:
        return wn._evaluate(worker.conn, replica, support.WORKER, mode="all", limit=limit, profile_exists=None,
                            full_detail="pickup batch full this tick")
    finally:
        replica.close()


def test_a_pass_copies_one_card_per_unit_of_provider_headroom(gws, anthropic_paced):
    hub, worker = gws
    pool = [_card(hub, f"pool {i}", 1000 - i, ["linux"]) for i in range(5)]
    pinned = _card(hub, "pinned below the pool", 0, PIN)
    worker.round()
    worker.enter()
    picked, skips = _evaluate(worker)
    assert picked == [pool[0]]  # RED before: all 6 anthropic cards copied against one slot
    reason, detail = skips[pinned]
    assert reason == "other" and detail.startswith("llm_paced: anthropic paced")
    assert "1 picked this pass" in detail


def test_the_pinned_card_is_paced_not_budget_exhausted_when_the_batch_also_fills(gws, anthropic_paced):
    hub, worker = gws
    [_card(hub, f"pool {i}", 1000 - i, ["linux"]) for i in range(3)]
    pinned = _card(hub, "pinned below the pool", 0, PIN)
    worker.round()
    worker.enter()
    picked, skips = _evaluate(worker, limit=1)
    assert len(picked) == 1
    assert skips[pinned][0] == "other" and skips[pinned][1].startswith("llm_paced:")  # never budget_exhausted


def test_spent_headroom_leaves_batch_room_for_other_providers(gws, anthropic_paced):
    hub, worker = gws
    pool = [_card(hub, f"anthropic {i}", 1000 - i, ["linux"]) for i in range(4)]
    codex = [_card(hub, f"codex {i}", 10 - i, ["linux"], provider="openai-codex") for i in range(2)]
    worker.round()
    worker.enter()
    picked, _ = _evaluate(worker, limit=3)
    assert picked == [pool[0], *codex]


def test_running_cards_and_this_pass_share_the_limit(gws, anthropic_paced, monkeypatch):
    hub, worker = gws
    [_card(hub, f"pool {i}", 1000 - i, ["linux"]) for i in range(3)]
    worker.round()
    worker.enter()
    monkeypatch.setattr(pd, "local_running", lambda conn, provider: 1 if provider == "anthropic" else 0)
    picked, _ = _evaluate(worker)
    assert picked == []  # the slot is already taken by a run here: nothing to copy


def test_pickup_copies_only_what_the_provider_can_start(gws, anthropic_paced):
    hub, worker = gws
    pool = [_card(hub, f"pool {i}", 1000 - i, ["linux"]) for i in range(5)]
    worker.round()
    worker.enter()
    assert wn.pickup(worker.conn, budget=BATCH) == [pool[0]]  # RED before: 5 copies, 4 pace-held then evicted
    assert [tid for tid in pool if kb.get_task(worker.conn, tid) is not None] == [pool[0]]


@pytest.mark.parametrize("title", ["routine work", "security work"])
def test_alternate_target_spends_headroom_and_claim_remains_unreserved(pool, strong_pattern, monkeypatch, title):
    """Main's class reroute must use pickup reservations without changing claim-time accounting."""
    monkeypatch.setattr(pd, "pace_for", lambda conn, provider, cfg, now: lp.PoolPace(
        provider, lp.PACED, 0.5) if provider == "xai-oauth" else lp.PoolPace(provider, lp.HELD, 0.0))
    monkeypatch.setattr(pd, "avg_running", lambda *args: 0.0)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title=title, assignee="a")
        card = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        admitted = []
        assert pd.pickup_hold(conn, conn, card, picked={}, admitted=admitted) is None
        assert admitted == ["xai-oauth"]
        admitted = []
        assert pd.pickup_hold(conn, conn, card, picked={"xai-oauth": 1}, admitted=admitted) is not None
        assert admitted == []
        # Reservations are per pickup pass, not persisted into the later real claim.
        seen, result = routing_support._dispatch(conn)
        assert seen == [(tid, *routing_support.GROK)]
        assert tid not in result.clock_held


def test_class_reserves_each_limited_candidate_without_forcing_an_alternate(pool, strong_pattern, monkeypatch):
    monkeypatch.setattr(pd, "pace_for", lambda conn, provider, cfg, now: lp.PoolPace(provider, lp.PACED, 0.5))
    monkeypatch.setattr(pd, "avg_running", lambda *args: 0.0)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="security work", assignee="a")
        card = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        admitted = []
        assert pd.pickup_hold(conn, conn, card, admitted=admitted) is None
        assert set(admitted) == {"anthropic", "openai-codex", "xai-oauth"}
        assert pd.pickup_hold(conn, conn, card, picked=dict.fromkeys(admitted, 1)) is not None
