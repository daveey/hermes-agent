"""Quota-episode routing must consult every known wall, and its deadline must feed the stuck warning.

Follow-ups from the Opus review of PR #578 (t_01a19987de068d42):
(a) an episode card with no class alternate skipped the strong-tier fallback and waited out the wall;
(b) ``decision`` admitted a "different" provider without asking whether that provider was walled too;
(c) the stuck warning's ``(until ...)`` read a fixed cooldown, not the episode's doubling deadline.
"""
from typing import Any

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_model_tier as tier
from hermes_cli import kanban_provider_walls as walls
from hermes_cli.kanban_db_dispatch_clock import guard_hold_until
from hermes_cli.kanban_quota_episode import episode
from tests.hermes_cli.test_kanban_quota_episode import board as board, reap, routing  # noqa: PLC0414 -- the self-alias is load-bearing: it suppresses F811 for the pytest fixture parameter shadowing this import

HUB_CFG = {'default_model': 'class:standard', 'strong_model': 'class:deep'}


def _codex_wall(reset) -> dict[str, Any]:
    return dict(text=f'SubscriptionQuotaUnavailable: Subscription quota unavailable for '
                f'openai-codex/gpt-6-astra; resets at {reset}.0',
                fields={'failure_reason': 'rate_limit', 'failure_retryable': True,
                        'failure_resets_at': float(reset),
                        'route_provider': 'openai-codex', 'route_model': 'gpt-6-astra'})


def _clock(monkeypatch, start=1_800_000_000):
    clock = [start]
    monkeypatch.setattr(kb.time, 'time', lambda: clock[0])
    monkeypatch.setattr(walls.time, 'time', lambda: clock[0])
    return clock


def _hub_tier(monkeypatch):
    monkeypatch.setattr(tier, 'load_tier_config', lambda: HUB_CFG)


def _class_routes(config, **by_class):
    """Isolated enabled class routes (``{class: [(provider, model), ...]}``), also for the host routability probe."""
    base = dict(billing='subscription', weight=1, eligible=True, funding_verified=True,
                capabilities={'tools': True, 'streaming': True})
    config['model_routing'] = {'enabled': True, 'routes': {
        level: [dict(base, provider=p, model=m) for p, m in routes] for level, routes in by_class.items()}}


@pytest.mark.parametrize('why', ['routing_disabled', 'no_class_alternate'])
def test_episode_default_card_without_class_alternate_falls_back_to_strong_tier(
        board, monkeypatch, all_assignees_spawnable, why):
    """(a) No class peer for the walled route: the episode's own wall still moves an unpinned default card
    to the strong class, even with no provider_walls.json entry and no live pool evidence. With routing off
    nothing can move it: it holds for the reset instead of spawning into the wall."""
    clock = _clock(monkeypatch)
    config, _ = routing(monkeypatch)
    if why == 'routing_disabled':
        config['model_routing']['enabled'] = False
    else:  # the only standard route is the walled codex one; the strong class has room
        _class_routes(config, standard=[('openai-codex', 'gpt-6-astra')], deep=[('anthropic', 'claude-opus-5-5')])
    _hub_tier(monkeypatch)
    monkeypatch.setattr(walls, 'live_quota_wall', lambda *_a, **_k: None)
    reset = clock[0] + 55 * 3600
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='episode default card', assignee='a')
        reap(conn, tid, admit=HUB_CFG, **_codex_wall(reset))
        # Only the episode knows the wall: no provider file, no live pool evidence.
        monkeypatch.setattr(walls, 'active_walls', lambda **_k: {})
        clock[0] += 301
        guard = dispatch.check_respawn_guard(conn, tid)
        seen = []
        dispatch.dispatch_once(conn, spawn_fn=lambda task, ws: seen.append((task.model_override, task.provider_override)))
        if why == 'routing_disabled':
            assert guard == 'subscription_quota_reset' and seen == []
            return
        assert guard is None
        assert seen == [('claude-opus-5-5', 'anthropic')]
        route = kb._json_dict(conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='run_route' "
                                           "ORDER BY id DESC LIMIT 1", (tid,)).fetchone()[0])
        assert route['provider'] == 'anthropic' and 'quota_reroute' in route['tier_reason']


def test_episode_fallback_never_rewrites_a_legacy_pin_row(board, monkeypatch, all_assignees_spawnable):
    """(a) A legacy Astra pin row (raw seed, dynamically ignored) neither reroutes nor is rewritten: with routing
    off the card holds for the reset and the author row is retained."""
    clock = _clock(monkeypatch)
    config, _ = routing(monkeypatch)
    config['model_routing']['enabled'] = False
    _hub_tier(monkeypatch)
    monkeypatch.setattr(walls, 'live_quota_wall', lambda *_a, **_k: None)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='legacy row card', assignee='a')
        conn.execute("UPDATE tasks SET model_override='gpt-6-astra', provider_override='openai-codex' WHERE id=?", (tid,))
        conn.commit()
        reap(conn, tid, admit=HUB_CFG, **_codex_wall(clock[0] + 55 * 3600))
        clock[0] += 301
        assert dispatch.check_respawn_guard(conn, tid) == 'subscription_quota_reset'
        persisted = kb.get_task(conn, tid)
        assert (persisted.model_override, persisted.provider_override) == ('gpt-6-astra', 'openai-codex')


@pytest.mark.parametrize('evidence', ['provider_file', 'live_pool'])
def test_episode_never_admits_a_target_that_is_walled_elsewhere(board, monkeypatch, all_assignees_spawnable, evidence):
    """(b) The episode walled anthropic; the default route is codex, which another card's wall (or the live
    pool) shows is walled too. "Different from the episode's provider" is not "free": hold."""
    clock = _clock(monkeypatch)
    config, _ = routing(monkeypatch)
    config['model_routing']['enabled'] = False
    _hub_tier(monkeypatch)
    codex_reset = clock[0] + 55 * 3600
    if evidence == 'provider_file':
        monkeypatch.setattr(walls, 'active_walls', lambda **_k: {'openai-codex': float(codex_reset)})
        monkeypatch.setattr(walls, 'live_quota_wall', lambda *_a, **_k: None)
    else:
        monkeypatch.setattr(walls, 'active_walls', lambda **_k: {})
        monkeypatch.setattr(walls, 'live_quota_wall',
                            lambda provider, model: float(codex_reset) if provider == 'openai-codex' else None)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='moved to anthropic then walled there', assignee='a')
        reap(conn, tid, text='Anthropic usage limit resets in ~11h', provider='anthropic',
             fields={'route_provider': 'anthropic', 'route_model': 'claude-opus-5-5'})
        assert episode(conn, tid)['providers'] == ['anthropic']
        clock[0] += 301
        assert dispatch.check_respawn_guard(conn, tid) == 'subscription_quota_reset'
        seen = []
        dispatch.dispatch_once(conn, spawn_fn=lambda task, ws: seen.append(task.provider_override))
        assert seen == []


def test_episode_still_admits_a_target_with_no_wall_anywhere(board, monkeypatch, all_assignees_spawnable):
    """(b) Control: with no wall on the default provider, the different provider is admitted as before."""
    clock = _clock(monkeypatch)
    config, _ = routing(monkeypatch)
    _class_routes(config, standard=[('anthropic', 'claude-opus-5-5'), ('openai-codex', 'gpt-6-astra')])
    _hub_tier(monkeypatch)
    monkeypatch.setattr(walls, 'active_walls', lambda **_k: {})
    monkeypatch.setattr(walls, 'live_quota_wall', lambda *_a, **_k: None)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='walled on anthropic only', assignee='a')
        reap(conn, tid, text='Anthropic usage limit resets in ~11h', provider='anthropic',
             fields={'route_provider': 'anthropic', 'route_model': 'claude-opus-5-5'})
        clock[0] += 301
        assert dispatch.check_respawn_guard(conn, tid) is None
        seen = []
        dispatch.dispatch_once(conn, spawn_fn=lambda task, ws: seen.append(task.provider_override))
        assert seen == ['openai-codex']


def test_stuck_deadline_reads_the_episode_backoff_not_one_cooldown(board, monkeypatch):
    """(c) Fourth rate-limited run with no hint: the episode holds 2400 s (300, 600, 1200, 2400 doubling);
    the stuck warning's deadline must say the same, not ended_at + one 300 s cooldown."""
    clock = _clock(monkeypatch)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='backoff deadline', assignee='a')
        for _ in range(3):
            reap(conn, tid)
            state = episode(conn, tid)
            clock[0] = state['until']
        reap(conn, tid)
        state = episode(conn, tid)
        assert state['count'] == 4 and state['until'] - clock[0] == 2400
        clock[0] += 400  # past one cooldown, inside the episode's backoff
        assert dispatch.check_respawn_guard(conn, tid) == 'rate_limit_cooldown'
        assert guard_hold_until(conn, tid, 'rate_limit_cooldown', clock[0]) == int(state['until'])


def test_stuck_deadline_keeps_an_earlier_providers_longer_wall(board, monkeypatch):
    """(c) The episode holds the latest wall of every provider it hit; the deadline is the longest one."""
    clock = _clock(monkeypatch)
    routing(monkeypatch, blocked=True)  # no alternate: the card is held on both walls
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='two walls', assignee='a')
        reap(conn, tid, text='usage limit resets in ~11h', provider='anthropic')
        long_until = episode(conn, tid)['until']
        reap(conn, tid, text='429 rate limited; retry after 5s', provider='openai-codex')
        assert episode(conn, tid)['until'] < long_until
        now = clock[0] + 600
        clock[0] = now
        assert dispatch.check_respawn_guard(conn, tid) == 'subscription_quota_reset'
        assert guard_hold_until(conn, tid, 'subscription_quota_reset', now) == int(long_until)


def test_stuck_warning_names_the_episode_deadline_end_to_end(board, monkeypatch, all_assignees_spawnable):
    """(c) Through a real tick: the held episode card's warning suffix is the episode deadline."""
    from hermes_cli.kanban_db_dispatch_clock import _iso
    clock = _clock(monkeypatch)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='backoff warning', assignee='a')
        for _ in range(3):
            reap(conn, tid)
            clock[0] = episode(conn, tid)['until']
        reap(conn, tid)
        until = int(episode(conn, tid)['until'])
        clock[0] += 400
        res = dispatch.dispatch_once(conn, spawn_fn=lambda task, ws: None)
    assert res.clock_held == {tid: ('rate_limit_cooldown', until)}
    assert f'rate_limit_cooldown=1 (until {_iso(until)})' in dispatch.stuck_warning(6, [res])


def test_expired_episode_still_honours_another_cards_wall(board, monkeypatch, all_assignees_spawnable):
    """(b) The card's own anthropic wall (11 h) ends before codex's (55 h, known from another card). Once the
    episode is over, routing must still skip walled codex: the provider-wall reroute applies again."""
    clock = _clock(monkeypatch)
    config, _ = routing(monkeypatch)
    _class_routes(config, standard=[('anthropic', 'claude-opus-5-5'), ('openai-codex', 'gpt-6-astra')])
    _hub_tier(monkeypatch)
    codex_reset = float(clock[0] + 55 * 3600)
    monkeypatch.setattr(walls, 'active_walls', lambda **_k: {'openai-codex': codex_reset})
    monkeypatch.setattr(walls, 'live_quota_wall', lambda *_a, **_k: None)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='anthropic wall ends first', assignee='a')
        reap(conn, tid, text='Anthropic usage limit resets in ~11h', provider='anthropic',
             fields={'route_provider': 'anthropic', 'route_model': 'claude-opus-5-5'})
        clock[0] += 301
        assert dispatch.check_respawn_guard(conn, tid) == 'subscription_quota_reset'
        clock[0] = int(episode(conn, tid)['until']) + 1
        assert dispatch.check_respawn_guard(conn, tid) is None
        seen = []
        result = dispatch.dispatch_once(conn, spawn_fn=lambda task, ws: seen.append((task.model_override, task.provider_override)))
        assert seen == [('claude-opus-5-5', 'anthropic')], result


def test_repin_onto_a_provider_walled_elsewhere_waits_and_keeps_the_pin(board, monkeypatch):
    """(b) An explicit pin moved onto codex while codex is walled (another card's wall) is held, not
    admitted into the wall, and the pin is never rewritten."""
    clock = _clock(monkeypatch)
    routing(monkeypatch)
    monkeypatch.setattr(walls, 'active_walls', lambda **_k: {'openai-codex': float(clock[0] + 55 * 3600)})
    monkeypatch.setattr(walls, 'live_quota_wall', lambda *_a, **_k: None)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='pinned', assignee='a')
        conn.execute("UPDATE tasks SET model_override='claude-sonnet-5-5', provider_override='anthropic' WHERE id=?", (tid,))
        conn.commit()
        reap(conn, tid, text='resets in ~11h')
        conn.execute("UPDATE tasks SET model_override='gpt-test', provider_override='openai-codex' WHERE id=?", (tid,))
        conn.commit()
        assert dispatch.check_respawn_guard(conn, tid) == 'subscription_quota_reset'
        persisted = kb.get_task(conn, tid)
        assert (persisted.model_override, persisted.provider_override) == ('gpt-test', 'openai-codex')


def test_review_card_waits_when_its_strong_provider_is_walled_elsewhere(board, monkeypatch):
    """(b) Review lane: the episode walled codex, the review route (strong tier, anthropic) is walled by
    another card's wall. Holding beats sending the review into that wall."""
    clock = _clock(monkeypatch)
    config, _ = routing(monkeypatch)
    _class_routes(config, standard=[('openai-codex', 'gpt-6-astra')], deep=[('anthropic', 'claude-opus-5-5')])
    _hub_tier(monkeypatch)
    monkeypatch.setattr(walls, 'live_quota_wall', lambda *_a, **_k: None)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='review behind two walls', assignee='a')
        conn.execute("UPDATE tasks SET status='review' WHERE id=?", (tid,))
        conn.commit()
        reap(conn, tid, lane='review', provider='openai-codex', **_codex_wall(clock[0] + 55 * 3600))
        clock[0] += 301
        monkeypatch.setattr(walls, 'active_walls', lambda **_k: {})
        free = dispatch.check_respawn_guard(conn, tid, lane='review')
        monkeypatch.setattr(walls, 'active_walls', lambda **_k: {'anthropic': float(clock[0] + 3600)})
        walled = dispatch.check_respawn_guard(conn, tid, lane='review')
        # Tier selection may see a standard fallback, but episode admission must
        # still refuse it: codex is held by this card's authenticated quota receipt.
        assert tier.resolve_model_tier(conn, kb.get_task(conn, tid), lane='review')[1] == 'openai-codex'
        from hermes_cli.kanban_review_model import ReviewRouteDeferred
        monkeypatch.setattr(walls, 'active_walls', lambda **_k: {
            'anthropic': float(clock[0] + 3600), 'openai-codex': float(clock[0] + 54 * 3600)})
        with pytest.raises(ReviewRouteDeferred, match='No eligible deep or standard review route'):
            tier.apply_model_tier(conn, kb.get_task(conn, tid), lane='review')
        seen = []
        dispatch.dispatch_once(conn, spawn_fn=lambda task, ws: seen.append(task.id))
        assert seen == []
    # Missing model attribution is audit-only: the free eligible deep route is
    # usable, but neither a deep nor a standard route bypasses real provider walls.
    assert free is None
    assert walled == 'subscription_quota_reset'


def test_class_with_every_route_walled_falls_back_to_strong_tier(board, monkeypatch, all_assignees_spawnable):
    """(a) The default class exists but its only route is the walled one: the strong class takes it."""
    clock = _clock(monkeypatch)
    config, _ = routing(monkeypatch)
    _class_routes(config, standard=[('openai-codex', 'gpt-6-astra')], deep=[('anthropic', 'claude-opus-5-5')])
    _hub_tier(monkeypatch)
    monkeypatch.setattr(walls, 'live_quota_wall', lambda *_a, **_k: None)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='only codex in class', assignee='a')
        reap(conn, tid, admit=HUB_CFG, **_codex_wall(clock[0] + 55 * 3600))
        monkeypatch.setattr(walls, 'active_walls', lambda **_k: {})
        clock[0] += 301
        seen = []
        dispatch.dispatch_once(conn, spawn_fn=lambda task, ws: seen.append((task.model_override, task.provider_override)))
        assert seen == [('claude-opus-5-5', 'anthropic')]


LIVE_CFG = {'default_model': 'class:standard', 'strong_model': 'class:deep'}


@pytest.mark.parametrize('evidence', ['provider_file', 'live_pool'])
def test_fresh_card_stays_a_provider_neutral_class_beside_a_known_wall(board, monkeypatch, all_assignees_spawnable,
                                                                       evidence):
    """A card with no quota episode of its own and a known codex wall (provider file or live pool): the neutral
    default is a provider-unknown class that the worker allocates, so the dispatcher never concretely routes it
    onto the walled provider (and, by design, no longer pre-places it on another one). Its receipt names none.
    PRODUCTION GAP: the old pre-spawn class alternate (``_class_alternate_off_wall``) skips ``auto`` bases."""
    clock = _clock(monkeypatch)
    config, _ = routing(monkeypatch)
    _class_routes(config, standard=[('anthropic', 'claude-opus-5-5'), ('openai-codex', 'gpt-6-astra')])
    monkeypatch.setattr(tier, 'load_tier_config', lambda: LIVE_CFG)
    codex_reset = float(clock[0] + 5000)
    if evidence == 'provider_file':
        monkeypatch.setattr(walls, 'active_walls', lambda **_k: {'openai-codex': codex_reset})
        monkeypatch.setattr(walls, 'live_quota_wall', lambda *_a, **_k: None)
    else:
        monkeypatch.setattr(walls, 'active_walls', lambda **_k: {})
        monkeypatch.setattr(walls, 'live_quota_wall',
                            lambda provider, model: codex_reset if provider == 'openai-codex' else None)
    with kbc.connect() as conn:
        marked = kb.create_task(conn, title='needs astra card', assignee='a')
        conn.execute("UPDATE tasks SET body=? WHERE id=?", (walls.REQUIRE_MODEL_MARKER, marked))  # raw legacy seed
        conn.commit()
        # The legacy require_model marker is only text now: it is never a concrete route.
        assert tier.resolve_dispatch_tier(conn, kb.get_task(conn, marked), lane='ready')[:2] == (
            'claude-opus-5-5', 'anthropic')
        conn.execute("DELETE FROM tasks WHERE id=?", (marked,))
        conn.commit()
        tid = kb.create_task(conn, title='fresh default card', assignee='a')
        seen = []
        dispatch.dispatch_once(conn, spawn_fn=lambda task, ws: seen.append((task.model_override, task.provider_override)))
        assert seen == [('claude-opus-5-5', 'anthropic')]
        receipt = kb._json_dict(conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='run_route' "
                                             "ORDER BY id DESC LIMIT 1", (tid,)).fetchone()[0])
        assert receipt['provider'] == 'anthropic' and receipt['model'] == 'claude-opus-5-5'


def test_class_alternate_ignores_require_model_named_in_prose(board, monkeypatch, all_assignees_spawnable):
    """The deliberate-line check (not a substring) still decides ``requires_model``: prose naming
    `require_model` is not a marker, and neither form turns the neutral class into a concrete pin."""
    config, _ = routing(monkeypatch)
    _class_routes(config, standard=[('anthropic', 'claude-opus-5-5'), ('openai-codex', 'gpt-6-astra')])
    monkeypatch.setattr(tier, 'load_tier_config', lambda: LIVE_CFG)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='storm alert', assignee='a', body=(
            'Cards pinned to the walled provider (`require_model`) keep their route.\n'
            'require_model: false'))
        assert not walls.requires_model(kb.get_task(conn, tid))
        assert tier.resolve_dispatch_tier(conn, kb.get_task(conn, tid), lane='ready') == (
            'class:standard', 'auto', 'default')


def test_fresh_card_class_alternate_never_lands_on_another_wall(board, monkeypatch, all_assignees_spawnable):
    """Both class routes walled: the neutral class card is never concretely pinned onto either wall."""
    clock = _clock(monkeypatch)
    config, _ = routing(monkeypatch)
    _class_routes(config, standard=[('anthropic', 'claude-opus-5-5'), ('openai-codex', 'gpt-6-astra')])
    monkeypatch.setattr(tier, 'load_tier_config', lambda: LIVE_CFG)
    monkeypatch.setattr(walls, 'active_walls', lambda **_k: {'openai-codex': float(clock[0] + 5000)})
    monkeypatch.setattr(walls, 'live_quota_wall',
                        lambda provider, model: float(clock[0] + 900) if provider == 'anthropic' else None)
    with kbc.connect() as conn:
        kb.create_task(conn, title='both walled', assignee='a')
        seen = []
        result = dispatch.dispatch_once(conn, spawn_fn=lambda task, ws: seen.append((task.model_override, task.provider_override)))
        assert seen == []
        assert result.clock_held
        assert conn.execute('SELECT COUNT(*) FROM task_runs WHERE task_id=?', (result.pinned_quota_held[0][0],)).fetchone()[0] == 0
