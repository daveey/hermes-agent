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
from tests.hermes_cli import kanban_pacing_state as pacing_state
from tests.hermes_cli import test_kanban_worker_node as support

svc = support.svc
base_gws = support.gws
WINDOW = 9


def configure_pool_profile(gateway, name, provider):
    """Real isolated execution profiles; pacing observes eligible routes, not card pins."""
    import json
    from hermes_cli.config_effective import load_profile_config_effective
    from hermes_cli.kanban_model_assignment import _assignee_config

    gateway.enter()
    config = load_profile_config_effective(gateway.home / "config.yaml", fail_closed=True)
    policy = config["model_routing"]
    policy["routes"] = {
        level: [route for route in routes if route["provider"] == provider]
        for level, routes in policy["routes"].items()
        if any(route["provider"] == provider for route in routes)
    }
    profile = gateway.home / "profiles" / name
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text(json.dumps(config))
    effective = _assignee_config(name)
    assert effective is not None
    assert effective["model_routing"] == policy


@pytest.fixture
def gws(base_gws):
    for gateway in base_gws:
        configure_pool_profile(gateway, "paced", "openai-codex")
        configure_pool_profile(gateway, "open", "anthropic")
    return base_gws


@pytest.fixture
def pools(monkeypatch):
    """Per-provider state: codex follows ``state['codex']`` (HELD = at reserve in v2), anthropic is open; each
    refresh stores both as this machine's dispatcher would."""
    state = {"codex": lp.HELD}

    def pace_for(conn, provider, cfg, now):
        pacing_state.store(("openai-codex", state["codex"], now + 86400), ("anthropic", lp.OPEN), now=now)
        return lp.PoolPace(provider, state["codex"] if provider == "openai-codex" else lp.OPEN)
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
    ids = [kb.create_task(hub.conn, title=f"codex {i}", assignee="paced", priority=100 - i, runs_on=["linux"])
           for i in range(n)]
    for tid in ids:
        task = kb.get_task(hub.conn, tid)
        assert task is not None
        assert (task.model_override, task.provider_override) == (None, None)
        assert pd.candidate_providers(hub.conn, task, "ready") == ["openai-codex"]
    hub.round()
    return ids


def _anthropic(hub, priority=0):
    hub.enter()
    tid = kb.create_task(hub.conn, title="anthropic", assignee="open", priority=priority, runs_on=["linux"])
    task = kb.get_task(hub.conn, tid)
    assert task is not None
    assert (task.model_override, task.provider_override) == (None, None)
    assert pd.candidate_providers(hub.conn, task, "ready") == ["anthropic"]
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
