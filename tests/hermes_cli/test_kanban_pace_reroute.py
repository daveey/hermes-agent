"""The quota pace governor starts a card on a pool with room instead of holding it (t_01a1deaf95f5b885). [daveey]

Pool workers 2026-11-15: every governed pool (Anthropic, Codex) paced to one running card while ready cards queued for
hours, though the routing registry held a subscription Grok route (xai-oauth, X Ultra) in the same classes.
"""
from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_llm_pacing as lp
from hermes_cli import kanban_llm_pacing_dispatch as pd
from hermes_cli import kanban_model_tier as tier
from hermes_cli import kanban_pace_reroute as reroute
from hermes_cli import kanban_provider_walls as walls
from tests.hermes_cli.test_kanban_quota_episode import board as board, routing  # noqa: PLC0414

HOUR = 3600.0
GROK = ("grok-4.7", "xai-oauth")
# The governor's claim-time move is judged here on its own: the resolver's far-hold move (kanban_paced_reroute, a
# pool held with its reset more than reroute_after_s away) would otherwise move these cards first. The two composed are
# covered by the ``resolver_first`` tests below.
POOL_CFG = {"default_model": "gpt-6-astra", "default_provider": "openai-codex",
            "strong_model": "class:deep", "strong_provider": "auto", "llm_pacing": {"reroute_after_s": 1e12}}
RESOLVER_CFG = {k: v for k, v in POOL_CFG.items() if k != "llm_pacing"}


def _route(provider, model):
    return dict(provider=provider, model=model, billing="subscription", weight=1, eligible=True,
                funding_verified=True, capabilities={"tools": True, "streaming": True})


@pytest.fixture
def pool(board, monkeypatch, all_assignees_spawnable):
    """A pool worker's registry: Grok in standard and deep next to Codex and Claude; no walls anywhere."""
    config, _ = routing(monkeypatch)
    config["model_routing"]["routes"] = {
        "standard": [_route("openai-codex", "gpt-6-astra"), _route("anthropic", "claude-sonnet-5-5"), _route(*GROK[::-1])],
        "deep": [_route("anthropic", "claude-opus-5-5"), _route("openai-codex", "gpt-6.1-sol"), _route(*GROK[::-1])]}
    monkeypatch.setattr(tier, "load_tier_config", lambda: dict(POOL_CFG))
    monkeypatch.setattr(walls, "active_walls", lambda **_k: {})
    monkeypatch.setattr(walls, "live_quota_wall", lambda *_a, **_k: None)
    serving = {"xai-oauth", "anthropic", "openai-codex"}
    monkeypatch.setattr(walls, "live_pool_serving", lambda provider, model=None: provider in serving)
    monkeypatch.setattr(pd, "_CACHE", {})
    monkeypatch.setattr(pd, "_WARNED", {})
    monkeypatch.setattr(pd, "state_path", lambda: board / "llm_pacing.json")
    reroute.clear()
    yield config, serving
    reroute.clear()


def _pools(monkeypatch, used):
    """``used``: provider -> seven-day percent; 99 holds the pool (past reserve), 5 leaves it open. Grok (xai-oauth) is
    a governed pool (b6f19b5dbc) and reads open unless ``used`` names it."""
    used = {"xai-oauth": 5, **used}

    def read(provider, now):
        return [lp.Reading(now, f"{provider}-1", lp.SEVEN_DAY, used[provider], now + 9 * HOUR)]
    monkeypatch.setattr(pd, "read_pool", read)


def _dispatch(conn):
    seen = []
    result = kbd.dispatch_once(conn, dry_run=False, spawn_fn=lambda task, ws: seen.append(
        (task.id, task.model_override, task.provider_override)))
    return seen, result


def _route_receipt(conn, tid):
    row = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='run_route' ORDER BY id DESC LIMIT 1",
                       (tid,)).fetchone()
    return kb._json_dict(row[0]) if row else None


def _paced_events(conn, tid):
    return [e for e in kb.list_events(conn, tid) if e.kind == pd.EVENT_KIND]


@pytest.mark.parametrize("title", ["routine work", "security work"])
def test_pace_route_reaches_fresh_owner(pool, strong_pattern, monkeypatch, title):
    _pools(monkeypatch, {"anthropic": 99, "openai-codex": 99})
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title=title, assignee="a")
        seen, _ = _dispatch(conn)
        assert seen == [(tid, *GROK)]
        assert tier.owner_model_pin(conn, kb.get_task(conn, tid)) == GROK


@pytest.mark.parametrize("title", ["routine work", "security work"])
def test_pace_receipt_failure_refuses_spawn(pool, strong_pattern, monkeypatch, title):
    _pools(monkeypatch, {"anthropic": 99, "openai-codex": 99})
    append = kb._append_event

    def fail_receipt(conn, tid, kind, *args, **kwargs):
        if kind == "model_tier":
            raise RuntimeError("receipt unavailable")
        return append(conn, tid, kind, *args, **kwargs)

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title=title, assignee="a")
        monkeypatch.setattr(kb, "_append_event", fail_receipt)
        seen, _ = _dispatch(conn)
        assert seen == []
        assert _route_receipt(conn, tid) is None


def test_default_card_starts_on_grok_when_codex_and_claude_are_paced(pool, monkeypatch):
    _pools(monkeypatch, {"anthropic": 99, "openai-codex": 99})
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="routine work", assignee="a")
        seen, result = _dispatch(conn)
        receipt = _route_receipt(conn, tid)
        assert _paced_events(conn, tid) == []
    assert seen == [(tid, *GROK)]
    assert tid not in result.clock_held
    assert receipt["provider"] == "xai-oauth" and receipt["model"] == "grok-4.7"
    assert receipt["tier_reason"] == "default+paced:anthropic,openai-codex"


def test_default_card_on_paced_codex_takes_the_open_claude_pool_first(pool, monkeypatch):
    """The paced pools are the walls; a governed pool still behind pace is a valid alternate (class order)."""
    _pools(monkeypatch, {"anthropic": 5, "openai-codex": 99})
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="routine work", assignee="a")
        seen, _ = _dispatch(conn)
        receipt = _route_receipt(conn, tid)
    assert seen == [(tid, "claude-sonnet-5-5", "anthropic")]
    assert receipt["tier_reason"] == "default+paced:openai-codex"


@pytest.fixture
def strong_pattern(pool, monkeypatch):
    """Cards titled ``security ...`` take the strong tier: ``class:deep``/``auto`` (reason ``pattern:security``)."""
    config, _ = pool
    monkeypatch.setattr(tier, "load_tier_config", lambda: {**POOL_CFG, "default_model_keep_patterns": ["security"]})
    monkeypatch.setattr("hermes_cli.config_effective.load_user_config_effective", lambda *_a, **_k: config)


def test_strong_class_card_moves_to_grok_not_a_paced_pool(pool, strong_pattern, monkeypatch):
    """A class card whose class also holds a Grok pool with room (xai-oauth) is still judged pool by pool: otherwise it
    starts unmoved and the class allocator may hand it to a paced Claude/Codex pool."""
    _pools(monkeypatch, {"anthropic": 99, "openai-codex": 99})
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="security work", assignee="a")
        seen, result = _dispatch(conn)
        receipt = _route_receipt(conn, tid)
    assert seen == [(tid, *GROK)]
    assert tid not in result.clock_held
    assert receipt["provider"] == "xai-oauth" and receipt["model"] == "grok-4.7"
    assert receipt["tier_reason"] == "pattern:security+paced:anthropic,openai-codex"


def test_strong_class_card_with_every_pool_open_starts_unmoved(pool, strong_pattern, monkeypatch):
    _pools(monkeypatch, {"anthropic": 5, "openai-codex": 5})
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="security work", assignee="a")
        seen, result = _dispatch(conn)
        assert _paced_events(conn, tid) == []
    assert seen == [(tid, "class:deep", "auto")] and tid not in result.clock_held


def test_strong_class_card_with_one_paced_pool_moves_off_it(pool, strong_pattern, monkeypatch):
    """One paced pool is enough to move a class card: the round-robin class allocator is not pace-aware and would
    hand it to Codex on its turn. It moves to the first open pool of its class (class order: Claude)."""
    _pools(monkeypatch, {"anthropic": 5, "openai-codex": 99})
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="security work", assignee="a")
        seen, result = _dispatch(conn)
        receipt = _route_receipt(conn, tid)
        assert _paced_events(conn, tid) == []
    assert seen == [(tid, "claude-opus-5-5", "anthropic")] and tid not in result.clock_held
    assert receipt["tier_reason"] == "pattern:security+paced:openai-codex"


def test_strong_class_card_waits_when_grok_is_paced_too(pool, strong_pattern, monkeypatch):
    """Grok is a governed pool: a class card whose every pool is at its limit holds, Grok named in the hold."""
    _pools(monkeypatch, {"anthropic": 99, "openai-codex": 99, "xai-oauth": 99})
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="security work", assignee="a")
        seen, result = _dispatch(conn)
        events = _paced_events(conn, tid)
    assert seen == [] and result.clock_held[tid][0] == lp.GUARD_REASON
    assert sorted(p["provider"] for p in events[-1].payload["providers"]) == ["anthropic", "openai-codex", "xai-oauth"]


def test_default_card_holds_when_grok_is_paced_too(pool, monkeypatch):
    """A governed move target is judged before the move: with Claude and Grok both at their limit, the default Codex
    card has no pool with room and holds."""
    _pools(monkeypatch, {"anthropic": 99, "openai-codex": 99, "xai-oauth": 99})
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="routine work", assignee="a")
        seen, result = _dispatch(conn)
    assert seen == [] and result.clock_held[tid][0] == lp.GUARD_REASON


def test_strong_class_card_without_grok_credential_waits(pool, strong_pattern, monkeypatch):
    _, serving = pool
    serving.discard("xai-oauth")
    _pools(monkeypatch, {"anthropic": 99, "openai-codex": 99})
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="security work", assignee="a")
        seen, result = _dispatch(conn)
    assert seen == [] and result.clock_held[tid][0] == lp.GUARD_REASON


def test_no_grok_credential_on_this_host_keeps_the_hold(pool, monkeypatch):
    _, serving = pool
    serving.discard("xai-oauth")
    _pools(monkeypatch, {"anthropic": 99, "openai-codex": 99})
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="routine work", assignee="a")
        seen, result = _dispatch(conn)
        assert len(_paced_events(conn, tid)) == 1
    assert seen == [] and result.clock_held[tid][0] == lp.GUARD_REASON


def test_ineligible_grok_route_keeps_the_hold(pool, monkeypatch):
    config, _ = pool
    for routes in config["model_routing"]["routes"].values():
        for route in routes:
            if route["provider"] == "xai-oauth":
                route["funding_verified"] = False
    _pools(monkeypatch, {"anthropic": 99, "openai-codex": 99})
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="routine work", assignee="a")
        seen, result = _dispatch(conn)
    assert seen == [] and result.clock_held[tid][0] == lp.GUARD_REASON


@pytest.mark.parametrize("pin", ["override", "require_model"])
def test_pinned_cards_still_wait_for_their_pool(pool, monkeypatch, pin):
    _pools(monkeypatch, {"anthropic": 99, "openai-codex": 99})
    with kbc.connect() as conn:
        if pin == "override":
            tid = kb.create_task(conn, title="needs opus", assignee="a", model_override="claude-opus-5-5",
                                 provider_override="anthropic")
        else:
            tid = kb.create_task(conn, title="needs astra", assignee="a", body=walls.REQUIRE_MODEL_MARKER)
        seen, result = _dispatch(conn)
    assert seen == [] and result.clock_held[tid][0] == lp.GUARD_REASON


def test_pickup_takes_a_card_the_claim_would_start_on_grok(pool, monkeypatch):
    _pools(monkeypatch, {"anthropic": 99, "openai-codex": 99})
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="routine work", assignee="a")
        card = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        assert pd.pickup_hold(conn, conn, card) is None
    _, serving = pool
    serving.discard("xai-oauth")
    monkeypatch.setattr(pd, "_CACHE", {})
    with kbc.connect() as conn:
        assert pd.pickup_hold(conn, conn, card) == "openai-codex held"


def test_alternate_probe_fault_keeps_the_hold(pool, monkeypatch):
    _pools(monkeypatch, {"anthropic": 99, "openai-codex": 99})

    def boom(*_a, **_k):
        raise RuntimeError("probe down")
    monkeypatch.setattr("hermes_cli.kanban_quota_episode.alternate_route", boom)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="routine work", assignee="a")
        seen, result = _dispatch(conn)
    assert seen == [] and result.clock_held[tid][0] == lp.GUARD_REASON


@pytest.mark.parametrize("config", [{}, {**POOL_CFG, "default_model_profiles": ["other"]}], ids=["tier-off", "out-of-scope"])
def test_profile_fallback_route_keeps_the_hold(pool, monkeypatch, config):
    """A route the tier resolver did not choose (tier off, or a profile outside its scope) falls back to the profile's
    own route, which the route applier never persists: a pace move there would be dropped after releasing the hold and
    the card would launch on the paced profile route (review 5912306106). It keeps the hold."""
    monkeypatch.setattr(tier, "load_tier_config", lambda: config)
    monkeypatch.setattr(tier, "profile_route", lambda *_: ("gpt-6-astra", "openai-codex"))
    _pools(monkeypatch, {"anthropic": 99, "openai-codex": 99})
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="routine work", assignee="a")
        seen, result = _dispatch(conn)
        assert tier.owner_model_pin(conn, kb.get_task(conn, tid)) == (None, None)
    assert seen == [] and result.clock_held[tid][0] == lp.GUARD_REASON


def test_offer_applies_only_to_the_route_it_judged():
    reroute.clear()
    assert reroute.take("t1", ("gpt-6-astra", "openai-codex")) is None  # no offer: an ordinary admission
    reroute.offer("t1", ("gpt-6-astra", "openai-codex"), ("grok-4.7", "xai-oauth", "openai-codex"))
    with pytest.raises(reroute.OfferLost):  # the route moved since: the admission it rested on is gone
        reroute.take("t1", ("claude-opus-5-5", "anthropic"))
    assert reroute.take("t1", ("gpt-6-astra", "openai-codex")) is None  # consumed by the first take
    reroute.offer("t1", ("gpt-6-astra", "OpenAI-Codex"), ("grok-4.7", "xai-oauth", "openai-codex"))
    assert reroute.take("t1", ("gpt-6-astra", "openai-codex")) == ("grok-4.7", "xai-oauth", "openai-codex")


def test_stale_offer_fails_closed(monkeypatch):
    reroute.clear()
    now = [1000.0]
    monkeypatch.setattr(reroute.time, "monotonic", lambda: now[0])
    reroute.offer("t1", ("gpt-6-astra", "openai-codex"), ("grok-4.7", "xai-oauth", "openai-codex"))
    now[0] += reroute.TTL_SECONDS
    with pytest.raises(reroute.OfferLost):
        reroute.take("t1", ("gpt-6-astra", "openai-codex"))
    reroute.offer("t1", ("gpt-6-astra", "openai-codex"), ("grok-4.7", "xai-oauth", "openai-codex"))
    reroute.withdraw("t1")
    assert reroute.take("t1", ("gpt-6-astra", "openai-codex")) is None


def _lapse_during_prepare(monkeypatch, lapse):
    """Run ``lapse()`` inside the claimed card's workspace preparation (between the governor's offer and the take)."""
    from hermes_cli import kanban_source_launch as launch
    prepare = launch._prepare

    def slow_prepare(*args, **kwargs):
        lapse()
        return prepare(*args, **kwargs)
    monkeypatch.setattr(launch, "_prepare", slow_prepare)


def _expire(monkeypatch):
    """A clock that jumps past the offer's TTL during the first launch only."""
    now, fired = [1000.0], []
    monkeypatch.setattr(reroute.time, "monotonic", lambda: now[0])

    def lapse():
        if not fired:
            fired.append(True)
            now[0] += reroute.TTL_SECONDS + 1
    return lapse


def _requeued_without_penalty(conn, tid):
    task = kb.get_task(conn, tid)
    assert task.status == "ready" and not task.claim_lock
    assert int(task.consecutive_failures or 0) == 0
    assert _route_receipt(conn, tid) is None


@pytest.mark.parametrize("title", ["routine work", "security work"])
def test_offer_expiring_during_launch_refuses_spawn(pool, strong_pattern, monkeypatch, title):
    """Review t_01a1e104366a08a8: a slow claim outlived the offer and the card started on the held Codex route."""
    _pools(monkeypatch, {"anthropic": 99, "openai-codex": 99})
    _lapse_during_prepare(monkeypatch, _expire(monkeypatch))
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title=title, assignee="a")
        seen, _ = _dispatch(conn)
        assert seen == []
        _requeued_without_penalty(conn, tid)
        seen, result = _dispatch(conn)  # requeued like any refused admission: spaced by the infrastructure cooldown
        assert seen == [] and result.respawn_guarded and "infrastructure_cooldown" in str(result.respawn_guarded)
        monkeypatch.setattr(kb, "_resolve_rate_limit_cooldown_seconds", lambda: 0)
        seen, _ = _dispatch(conn)  # the next tick judges it afresh and starts it on Grok
        assert seen == [(tid, *GROK)]


@pytest.mark.parametrize("title", ["routine work", "security work"])
def test_offer_for_another_route_refuses_spawn(pool, strong_pattern, monkeypatch, title):
    _pools(monkeypatch, {"anthropic": 99, "openai-codex": 99})
    resolve = tier.resolve_dispatch_tier

    def moved_route():  # the card's route changed between the governor's read and the launch
        monkeypatch.setattr(tier, "resolve_dispatch_tier",
                            lambda *a, **k: ("claude-sonnet-5-5", "anthropic", resolve(*a, **k)[2]))
    _lapse_during_prepare(monkeypatch, moved_route)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title=title, assignee="a")
        seen, _ = _dispatch(conn)
        assert seen == []
        _requeued_without_penalty(conn, tid)


def test_offer_for_an_unresolvable_route_refuses_spawn(pool, monkeypatch):
    _pools(monkeypatch, {"anthropic": 99, "openai-codex": 99})

    def broken():
        def fail(*_a, **_k):
            raise RuntimeError("resolver down")
        monkeypatch.setattr(tier, "resolve_dispatch_tier", fail)
    _lapse_during_prepare(monkeypatch, broken)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="routine work", assignee="a")
        seen, _ = _dispatch(conn)
        assert seen == []
        _requeued_without_penalty(conn, tid)


def test_an_earlier_ticks_offer_never_steers_an_ordinary_admission(pool, monkeypatch):
    _pools(monkeypatch, {"anthropic": 5, "openai-codex": 5})
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="routine work", assignee="a")
        reroute.offer(tid, ("gpt-6-astra", "openai-codex"), (*GROK, "openai-codex"))  # left by a claim that never ran
        seen, _ = _dispatch(conn)
        assert seen == [(tid, "gpt-6-astra", "openai-codex")]


@pytest.fixture
def resolver_first(pool, monkeypatch):
    """The shipped default: the resolver's far-hold move (reroute_after_s 3600) runs before the governor's."""
    monkeypatch.setattr(tier, "load_tier_config", lambda: dict(RESOLVER_CFG))


def test_resolver_first_far_held_default_card_starts_on_grok(pool, resolver_first, monkeypatch):
    """Both pools held 9 h: the resolver moves the default route off the held Codex pool to the first serving class
    peer (Grok: Claude is held too); the governor then judges Grok, which it does not govern, and starts the card."""
    _pools(monkeypatch, {"anthropic": 99, "openai-codex": 99})
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="routine work", assignee="a")
        seen, result = _dispatch(conn)
        receipt = _route_receipt(conn, tid)
    assert seen == [(tid, *GROK)] and tid not in result.clock_held
    assert receipt is not None and (receipt["model"], receipt["provider"]) == GROK
    assert receipt["tier_reason"] == "default+paced:openai-codex"


def test_resolver_first_no_grok_credential_keeps_the_hold(pool, resolver_first, monkeypatch):
    """Neither move names a provider this host cannot serve: with no xai-oauth credential the card stays held."""
    _, serving = pool
    serving.discard("xai-oauth")
    _pools(monkeypatch, {"anthropic": 99, "openai-codex": 99})
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="routine work", assignee="a")
        for _ in range(2):
            seen, result = _dispatch(conn)
            assert seen == [] and result.clock_held[tid][0] == lp.GUARD_REASON
        assert _route_receipt(conn, tid) is None
        card = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        assert pd.pickup_hold(conn, conn, card) == "openai-codex held"
