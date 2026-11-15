"""Pickup's window skips cards this machine's pacing governor would hold, so paced work never hides runnable work.

worker-6 2026-11-15 (t_01a1ddeb76e30be7): the top 9 hub cards were openai-codex cards held by pacing (limit 1,
running 2 to 8). Pickup copied them, local dispatch held them, and a runnable anthropic card at rank 16 was never
offered to any worker.
"""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_llm_pacing as lp
from hermes_cli import kanban_llm_pacing_dispatch as pd
from hermes_cli import kanban_worker_node as wn
from tests.hermes_cli import test_kanban_worker_node as support

svc = support.svc
gws = support.gws
WINDOW = 9


@pytest.fixture
def pools(monkeypatch):
    """Per-provider pace: codex follows ``state['codex']`` (HELD = limit 0, OPEN = no limit); anthropic is open."""
    state = {"codex": lp.HELD}

    def pace_for(conn, provider, cfg, now):
        if provider == "openai-codex":
            return lp.PoolPace(provider, state["codex"], 0.0 if state["codex"] == lp.HELD else 1.0)
        return lp.PoolPace(provider, lp.OPEN, 1.0)
    monkeypatch.setattr(pd, "pace_for", pace_for)
    return state


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


def _codex(hub, n):
    hub.enter()
    ids = [kb.create_task(hub.conn, title=f"codex {i}", assignee="default", priority=100 - i, runs_on=["linux"],
                          model_override="gpt-5.5", provider_override="openai-codex") for i in range(n)]
    hub.round()
    return ids


def _anthropic(hub, priority=0):
    hub.enter()
    tid = kb.create_task(hub.conn, title="anthropic", assignee="default", priority=priority, runs_on=["linux"],
                         model_override="claude-sonnet-5-5", provider_override="anthropic")
    hub.round()
    return tid


def test_paced_cards_ahead_of_a_runnable_card_do_not_hide_it(gws, pools, imports):
    hub, worker = gws
    paced = _codex(hub, WINDOW + 3)
    runnable = _anthropic(hub)
    worker.round()
    worker.enter()
    assert wn.pickup(worker.conn, budget=WINDOW) == [runnable]  # RED before: 9 paced copies, anthropic never offered
    assert not set(imports) & set(paced)


def test_the_window_stays_bounded_and_keeps_admission_order(gws, pools):
    hub, worker = gws
    paced = _codex(hub, WINDOW)
    runnable = [_anthropic(hub, priority=50 - i) for i in range(WINDOW + 2)]
    worker.round()
    worker.enter()
    picked = wn.pickup(worker.conn, budget=WINDOW)
    assert picked == runnable[:WINDOW]
    assert not set(picked) & set(paced)


def test_a_paced_card_is_offered_once_its_limit_frees(gws, pools):
    hub, worker = gws
    [paced] = _codex(hub, 1)
    worker.round()
    worker.enter()
    assert wn.pickup(worker.conn, budget=WINDOW) == []
    pools["codex"] = lp.OPEN
    assert wn.pickup(worker.conn, budget=WINDOW) == [paced]


def test_a_failed_pacing_check_copies_as_before(gws, pools, monkeypatch):
    hub, worker = gws
    [paced] = _codex(hub, 1)
    worker.round()
    worker.enter()

    def boom(*a, **kw):
        raise RuntimeError("usage unreadable")
    monkeypatch.setattr(pd, "pace_for", boom)
    assert wn.pickup(worker.conn, budget=1) == [paced]
