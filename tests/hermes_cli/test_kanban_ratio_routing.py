"""Ratio routing: an unpinned default card starts on the pool furthest below its budget share (``kanban_ratio_routing``).

Hub config 2026-11-19: ``kanban.default_model: gpt-6-astra`` on openai-codex, and the ``deep`` class lists anthropic
(claude-fable-5-2, claude-opus-5-5), openai-codex (gpt-6-astra) and xai-oauth (grok-4.7). Every default card ran on
astra until codex reached reserve, while the planned budget (``llm_pacing.json`` ``v2.pools[p].ratio``) spread the
sustainable token rate over all three pools.
"""
import collections
import json
import logging
import sqlite3

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_llm_pacing as lp
from hermes_cli import kanban_llm_pacing_dispatch as lpd
from hermes_cli import kanban_model_tier as tier
from hermes_cli import kanban_provider_walls as walls
from hermes_cli import kanban_queue_position as kqp
from hermes_cli import kanban_ratio_routing as ratio
from tests.hermes_cli import kanban_pacing_state as pacing_state
from tests.hermes_cli import test_kanban_pin_reroute as pin_tests
from tests.hermes_cli.test_kanban_quota_episode import board as board, reap, routing  # noqa: PLC0414 -- the self-alias is load-bearing: it suppresses F811 for the pytest fixture parameter shadowing this import

FABLE, OPUS = ('claude-fable-5-2', 'anthropic'), ('claude-opus-5-5', 'anthropic')
ASTRA, GROK = ('gpt-6-astra', 'openai-codex'), ('grok-4.7', 'xai-oauth')
HUB_CFG = {'default_model': ASTRA[0], 'default_provider': ASTRA[1], 'strong_model': OPUS[0], 'strong_provider': OPUS[1],
           'default_model_keep_patterns': ['security']}
TARGETS = {'anthropic': 0.5, 'openai-codex': 0.3, 'xai-oauth': 0.2}
UNSERVED: set = set()  # providers this host holds no live credential for, per test


@pytest.fixture
def hub(board, monkeypatch):
    """The hub's tier config and ``deep`` registry, a clock shared by every reader, no walls, every pool serving."""
    clock = pin_tests._clock(monkeypatch)
    for module in (ratio, lpd):
        monkeypatch.setattr(module.time, 'time', lambda: clock[0])
    monkeypatch.setattr(lpd, '_CACHE', {})
    monkeypatch.setattr(lpd, '_V2_MEMO', {})
    monkeypatch.setattr(ratio, '_MEMOS', {})
    monkeypatch.setattr(ratio, '_WARNED', {})
    config, _ = routing(monkeypatch)
    std = config['model_routing']['routes']['standard']
    config['model_routing']['routes'] = {'deep': [dict(std[0], model=m, provider=p) for m, p in (FABLE, OPUS, ASTRA, GROK)]}
    config['providers'] = {}
    cfg = dict(HUB_CFG)
    monkeypatch.setattr(tier, 'load_tier_config', lambda: cfg)
    monkeypatch.setattr(walls, 'active_walls', lambda **_k: {})
    monkeypatch.setattr(walls, 'live_quota_wall', lambda *_a, **_k: None)
    monkeypatch.setattr(walls, 'live_pool_serving', lambda provider, model=None: provider not in UNSERVED)
    UNSERVED.clear()
    return {'clock': clock, 'config': config, 'cfg': cfg}


def _budget(clock, ratios=None, *, age=0.0, at_reserve=(), rate_null=(), v2=None):
    """Store this home's ``llm_pacing.json`` as a dispatcher leaves it, with each pool's planned ``ratio``."""
    ratios = TARGETS if ratios is None else ratios
    now = clock[0]
    data, pools = {}, {}
    for provider, share in ratios.items():
        reserved = provider in at_reserve
        pool = pacing_state.v2_pool(reserved, now - age, now + 86400 if reserved else None)
        pool['ratio'] = share
        pool['rate_tokens_per_s'] = None if provider in rate_null else 1000.0 * share
        pools[provider] = pool
        pace = lp.PoolPace(provider, lp.HELD if reserved else lp.OPEN, 0.0 if reserved else None)
        data[provider] = {'read_at': now - age, 'pace': pace.as_dict(), 'samples': []}
    data['version'] = lp.V2_VERSION
    data['v2'] = v2 if v2 is not None else {'computed_at': now - age, 'margin_s': 10800, 'pools': pools,
                                            'global_rate_tokens_per_s': 1000.0, 'events': [], 'curve': {}}
    lpd.state_path().write_text(json.dumps(data), encoding='utf-8')


def _route(conn, tid, lane='ready'):
    return tier.resolve_dispatch_tier(conn, kb.get_task(conn, tid), lane=lane)


def _starts(conn, provider, n, *, reason='default', foreign=False):
    """``n`` past default-tier starts on ``provider``: real claimed runs with their ``run_route`` receipts."""
    for _ in range(n):
        tid = kb.create_task(conn, title='past', assignee='b')
        task = kb.claim_task(conn, tid, claimer=kb._claimer_id())
        kb._append_event(conn, tid, 'run_route', {'model': 'm', 'provider': provider, 'lane': 'ready',
                                                  'source': 'pin', 'tier_reason': reason}, run_id=task.current_run_id)
        if foreign:
            conn.execute("UPDATE task_runs SET claim_lock='some-other-host:1' WHERE id=?", (task.current_run_id,))
        conn.execute("UPDATE tasks SET status='done', claim_lock=NULL WHERE id=?", (tid,))
    conn.commit()


def test_1000_simulated_default_starts_converge_to_the_target_ratios(hub):
    _budget(hub['clock'])
    with kbc.connect() as conn:
        task = kb.get_task(conn, kb.create_task(conn, title='d', assignee='a'))
        ratio.begin_tick(conn)
        seen = collections.Counter()
        for _ in range(1000):
            model, provider, reason = tier.resolve_dispatch_tier(conn, task, lane='ready')
            assert reason == f'default+ratio:{provider}'
            assert (model, provider) in (FABLE, ASTRA, GROK)
            ratio.note_start(conn, provider, reason)
            seen[provider] += 1
    for provider, target in TARGETS.items():
        assert abs(seen[provider] / 1000 - target) <= 0.03, seen


def test_an_eligible_under_share_pool_takes_the_card_and_ties_go_to_registry_order(hub):
    _budget(hub['clock'], {'anthropic': 0.5, 'openai-codex': 0.5})
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='d', assignee='a')
        assert _route(conn, tid) == (*FABLE, 'default+ratio:anthropic')  # no starts yet: equal deficits, first pool
        _starts(conn, 'anthropic', 2)
        ratio.begin_tick(conn)
        assert _route(conn, tid) == (*ASTRA, 'default+ratio:openai-codex')


@pytest.mark.parametrize('why', ['ratio_zero', 'at_reserve', 'rate_null', 'walled', 'live_wall', 'disabled',
                                 'no_credential', 'ungoverned'])
def test_an_ineligible_pool_is_never_picked(hub, monkeypatch, why):
    """anthropic holds 90% of the budget but may not take a card: every start goes to codex."""
    clock = hub['clock']
    ratios = {'anthropic': 0.0 if why == 'ratio_zero' else 0.9, 'openai-codex': 0.1}
    _budget(clock, ratios, at_reserve=('anthropic',) if why == 'at_reserve' else (),
            rate_null=('anthropic',) if why == 'rate_null' else ())
    if why == 'at_reserve':  # a reserved pool keeps a stale positive ratio: at_reserve alone must exclude it
        state = json.loads(lpd.state_path().read_text())
        state['v2']['pools']['anthropic']['ratio'] = 0.9
        lpd.state_path().write_text(json.dumps(state))
    walls_by_case = {
        'walled': lambda: monkeypatch.setattr(walls, 'active_walls', lambda **_k: {'anthropic': clock[0] + 3600.0}),
        'live_wall': lambda: monkeypatch.setattr(walls, 'live_quota_wall',
                                                 lambda p, m: clock[0] + 3600.0 if p == 'anthropic' else None),
        'disabled': lambda: hub['config']['providers'].update({'anthropic': {'enabled': False}}),
        'no_credential': lambda: UNSERVED.add('anthropic'),
        'ungoverned': lambda: hub['cfg'].update({'llm_pacing': {'providers': ['openai-codex', 'xai-oauth']}}),
    }
    walls_by_case.get(why, lambda: None)()
    with kbc.connect() as conn:
        task = kb.get_task(conn, kb.create_task(conn, title='d', assignee='a'))
        ratio.begin_tick(conn)
        for _ in range(20):
            route = tier.resolve_dispatch_tier(conn, task, lane='ready')
            assert route == (*ASTRA, 'default+ratio:openai-codex')
            ratio.note_start(conn, route[1], route[2])


def _corrupt(clock, case):
    """Write the ``case`` state: v2 missing, stale, malformed, or stamped in the future."""
    stamps = {'stale': 20 * 60 + 1, 'future': -120}
    if case in stamps:
        return _budget(clock, age=stamps[case])
    bodies = {'missing': None, 'malformed_v2': ['not', 'a', 'dict'], 'malformed_pools': {'computed_at': clock[0],
                                                                                          'pools': 'oops'},
              'no_stamp': {'pools': {}}}
    _budget(clock)
    state = json.loads(lpd.state_path().read_text())
    state.pop('v2') if bodies[case] is None else state.update(v2=bodies[case])
    lpd.state_path().write_text(json.dumps(state))


@pytest.mark.parametrize('case', ['missing', 'stale', 'future', 'malformed_v2', 'malformed_pools', 'no_stamp'])
def test_no_fresh_well_formed_budget_keeps_todays_route(hub, case):
    _corrupt(hub['clock'], case)
    with kbc.connect() as conn:
        assert _route(conn, kb.create_task(conn, title='d', assignee='a')) == (*ASTRA, 'default')


def test_a_budget_just_inside_twenty_minutes_still_routes(hub):
    _budget(hub['clock'], age=20 * 60 - 1)
    with kbc.connect() as conn:
        assert _route(conn, kb.create_task(conn, title='d', assignee='a'))[2] == 'default+ratio:anthropic'


@pytest.mark.parametrize('case', ['switch_off', 'pacing_off', 'no_class_level'])
def test_ratio_routing_off_keeps_todays_route(hub, case):
    _budget(hub['clock'])
    changes = {'switch_off': {'llm_pacing': {'ratio_routing': False}}, 'pacing_off': {'llm_pacing': {'enabled': False}}}
    hub['cfg'].update(changes.get(case, {}))
    if case == 'no_class_level':
        hub['config']['model_routing']['routes']['deep'] = [dict(r) for r in hub['config']['model_routing']['routes'][
            'deep'] if r['model'] != ASTRA[0]]
    with kbc.connect() as conn:
        assert _route(conn, kb.create_task(conn, title='d', assignee='a')) == (*ASTRA, 'default')


# --- a class-selector default (``kanban.default_model: class:standard``, provider ``auto``) ------------------------

SONNET, MINI, GROK_MINI = ('claude-sonnet-5-5', 'anthropic'), ('gpt-6-mini', 'openai-codex'), ('grok-4.7-mini', 'xai-oauth')
STANDARD = ('class:standard', 'auto')


@pytest.fixture
def standard(hub):
    """The owner's planned default: ``class:standard`` on provider ``auto``, its level next to ``deep``."""
    std = hub['config']['model_routing']['routes']['deep'][0]
    hub['config']['model_routing']['routes']['standard'] = [dict(std, model=m, provider=p)
                                                            for m, p in (SONNET, MINI, GROK_MINI)]
    hub['cfg'].update(default_model=STANDARD[0], default_provider=STANDARD[1])
    return hub


def test_a_class_default_picks_a_concrete_route_of_that_class(standard):
    _budget(standard['clock'])
    with kbc.connect() as conn:
        task = kb.get_task(conn, kb.create_task(conn, title='d', assignee='a'))
        ratio.begin_tick(conn)
        seen = collections.Counter()
        for _ in range(200):
            model, provider, reason = tier.resolve_dispatch_tier(conn, task, lane='ready')
            assert (model, provider) in (SONNET, MINI, GROK_MINI) and reason == f'default+ratio:{provider}'
            ratio.note_start(conn, provider, reason)
            seen[provider] += 1
    for provider, target in TARGETS.items():
        assert abs(seen[provider] / 200 - target) <= 0.03, seen


def test_a_class_default_honours_ratio_route_models(standard):
    std = standard['config']['model_routing']['routes']['deep'][0]
    standard['config']['model_routing']['routes']['standard'].append(dict(std, model='claude-haiku-5', provider='anthropic'))
    standard['cfg']['llm_pacing'] = {'ratio_route_models': {'anthropic': 'claude-haiku-5', 'openai-codex': ASTRA[0]}}
    _budget(standard['clock'], {'anthropic': 0.5, 'openai-codex': 0.5})
    with kbc.connect() as conn:
        task = kb.get_task(conn, kb.create_task(conn, title='d', assignee='a'))
        ratio.begin_tick(conn)
        first = tier.resolve_dispatch_tier(conn, task, lane='ready')
        assert first == ('claude-haiku-5', 'anthropic', 'default+ratio:anthropic')
        ratio.note_start(conn, first[1], first[2])
        # astra is a deep route, not one of codex's standard routes: codex runs its first standard route.
        assert tier.resolve_dispatch_tier(conn, task, lane='ready') == (*MINI, 'default+ratio:openai-codex')


def test_a_class_default_card_spawns_on_its_concrete_pick(standard, all_assignees_spawnable):
    _budget(standard['clock'], {'anthropic': 0.5, 'openai-codex': 0.5})
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='d', assignee='a')
        assert pin_tests._tick(conn) == [SONNET]
        receipt = pin_tests._events(conn, tid, 'run_route')[-1]
        assert (receipt['model'], receipt['provider'], receipt['tier_reason']) == (*SONNET, 'default+ratio:anthropic')
        assert receipt['ratio']['pool'] == 'anthropic'


@pytest.mark.parametrize('case', ['missing', 'stale', 'malformed_v2', 'switch_off', 'no_eligible_pool', 'unknown_class'])
def test_a_class_default_fails_open_to_the_class_selector_itself(standard, case):
    """Fail-open keeps today's base unchanged: the ``class:standard``/``auto`` selector the worker allocates."""
    clock = standard['clock']
    if case in ('missing', 'stale', 'malformed_v2'):
        _corrupt(clock, case)
    else:
        _budget(clock)
    expected = (*STANDARD, 'default')
    if case == 'switch_off':
        standard['cfg']['llm_pacing'] = {'ratio_routing': False}
    if case == 'no_eligible_pool':
        UNSERVED.update(TARGETS)
    if case == 'unknown_class':
        standard['cfg']['default_model'] = 'class:huge'
        expected = ('class:huge', 'auto', 'default')
    with kbc.connect() as conn:
        assert _route(conn, kb.create_task(conn, title='d', assignee='a')) == expected


def test_under_a_class_default_an_unrecorded_provider_counts_toward_no_pool(standard):
    _budget(standard['clock'], {'anthropic': 0.5, 'openai-codex': 0.5})
    with kbc.connect() as conn:
        _starts(conn, '', 3)  # class runs whose provider the worker chose and nobody recorded
        _starts(conn, 'anthropic', 1)
        tid = kb.create_task(conn, title='d', assignee='a')
        ratio.begin_tick(conn)
        assert _route(conn, tid) == (*MINI, 'default+ratio:openai-codex')


@pytest.mark.parametrize('fault', [RuntimeError('policy unreadable'), sqlite3.OperationalError('database is locked')])
def test_a_fault_keeps_todays_route_and_is_logged_with_its_traceback(hub, monkeypatch, caplog, fault):
    _budget(hub['clock'])

    def boom(*_a, **_k):
        raise fault
    monkeypatch.setattr(ratio, 'count_starts', boom)
    with kbc.connect() as conn, caplog.at_level(logging.WARNING, logger=ratio.logger.name):
        assert _route(conn, kb.create_task(conn, title='d', assignee='a')) == (*ASTRA, 'default')
    records = [r for r in caplog.records if r.name == ratio.logger.name and r.levelno == logging.WARNING]
    assert records and records[0].exc_info is not None


def _review_card(conn):
    tid = kb.create_task(conn, title='review me', assignee='a', model_override=OPUS[0], provider_override=OPUS[1])
    conn.execute("UPDATE tasks SET status='review' WHERE id=?", (tid,))
    conn.commit()
    return tid


def _retry_card(conn):
    tid = kb.create_task(conn, title='second attempt', assignee='a')
    kb._append_event(conn, tid, 'changes_requested', {})
    conn.commit()
    return tid


def _provider_only_card(conn):
    """A legacy row with a provider override and no model (create refuses one now; old rows may carry it)."""
    tid = kb.create_task(conn, title='p', assignee='a')
    conn.execute('UPDATE tasks SET provider_override=? WHERE id=?', (ASTRA[1], tid))
    conn.commit()
    return tid


CARDS = {
    'pinned': (lambda conn: kb.create_task(conn, title='p', assignee='a', model_override=ASTRA[0],
                                           provider_override=ASTRA[1]), 'ready', 'pinned'),
    'provider_override': (_provider_only_card, 'ready', 'default'),
    'require_model': (lambda conn: kb.create_task(conn, title='r', assignee='a',
                                                  body=f'astra only\n{walls.REQUIRE_MODEL_MARKER}'), 'ready', 'default'),
    'review': (_review_card, 'review', 'review_unpinned'),
    'retry': (_retry_card, 'ready', 'retry_after_changes_requested'),
    'keep_pattern': (lambda conn: kb.create_task(conn, title='security audit', assignee='a'), 'ready',
                     'pattern:security'),
}


@pytest.mark.parametrize('card', sorted(CARDS))
def test_cards_outside_the_unpinned_default_tier_are_untouched(hub, card):
    """Pins, provider overrides, ``require_model``, reviews and the strong tier (retries, keep-patterns) keep the
    route they resolve to with ratio routing off."""
    make, lane, reason = CARDS[card]
    with kbc.connect() as conn:
        tid = make(conn)
        before = _route(conn, tid, lane)
        _budget(hub['clock'])
        ratio.begin_tick(conn)
        assert _route(conn, tid, lane) == before
    assert before[2] == reason


def test_ratio_route_models_picks_the_model_each_pool_runs(hub):
    hub['cfg']['llm_pacing'] = {'ratio_route_models': {'anthropic': OPUS[0], 'xai-oauth': 'grok-unregistered'}}
    _budget(hub['clock'], {'anthropic': 0.5, 'xai-oauth': 0.5})
    with kbc.connect() as conn:
        task = kb.get_task(conn, kb.create_task(conn, title='d', assignee='a'))
        ratio.begin_tick(conn)
        first = tier.resolve_dispatch_tier(conn, task, lane='ready')
        assert first == (*OPUS, 'default+ratio:anthropic')
        ratio.note_start(conn, first[1], first[2])
        # A model that is not one of the pool's routes in the class is ignored: its first route runs.
        assert tier.resolve_dispatch_tier(conn, task, lane='ready') == (*GROK, 'default+ratio:xai-oauth')


def test_preview_governor_and_admission_agree_and_the_next_card_sees_the_start(hub, all_assignees_spawnable):
    _budget(hub['clock'], {'anthropic': 0.5, 'openai-codex': 0.5})
    with kbc.connect() as conn:
        _starts(conn, 'openai-codex', 1)
        first = kb.create_task(conn, title='first', assignee='a', priority=10)
        second = kb.create_task(conn, title='second', assignee='a', priority=0)
        ratio.begin_tick(conn)
        preview = _route(conn, first)
        assert preview == (*FABLE, 'default+ratio:anthropic')
        assert lpd.candidate_route(conn, kb.get_task(conn, first), 'ready')[0] == preview
        assert kqp.position(conn, kb.get_task(conn, first))['reroute'] == \
            'next run on anthropic (budget share): anthropic/claude-fable-5-2'
        assert pin_tests._tick(conn) == [FABLE, FABLE]  # 1 codex start, then each card evens the share
        receipts = {tid: pin_tests._events(conn, tid, 'run_route')[-1] for tid in (first, second)}
        assert (receipts[first]['model'], receipts[first]['provider'], receipts[first]['tier_reason']) == preview
        assert receipts[first]['ratio'] == {'pool': 'anthropic', 'model': FABLE[0], 'target': 0.5, 'share': 0.0,
                                            'deficit': 0.5, 'starts': 1}
        assert receipts[second]['ratio']['share'] == 0.5 and receipts[second]['ratio']['starts'] == 2
        tiers = {tid: pin_tests._events(conn, tid, 'model_tier')[-1] for tid in (first, second)}
        assert tiers[first]['ratio'] == receipts[first]['ratio'] and tiers[first]['reason'] == preview[2]
        persisted = kb.get_task(conn, first)
        assert (persisted.model_override, persisted.provider_override) == (None, None)  # the row never moves


def test_the_second_card_in_a_tick_lands_on_the_other_pool(hub, all_assignees_spawnable):
    """Without the in-tick count both would take the first pool: the second card sees the first card's start."""
    _budget(hub['clock'], {'anthropic': 0.5, 'openai-codex': 0.5})
    with kbc.connect() as conn:
        for name in ('one', 'two'):
            kb.create_task(conn, title=name, assignee='a')
        assert sorted(pin_tests._tick(conn)) == sorted([FABLE, ASTRA])


def test_a_walled_ratio_pick_still_moves_via_its_quota_episode(hub):
    """The ratio route is an unpinned default to the episode: its wall moves it like any default route. (Only
    anthropic has a budget here, so the ratio pick stays on it after its own start.)"""
    clock = hub['clock']
    _budget(clock, {'anthropic': 0.9})
    reset = clock[0] + 55 * 3600
    wall = dict(text=f'SubscriptionQuotaUnavailable: Subscription quota unavailable for anthropic/{FABLE[0]}; '
                     f'resets at {reset}.0',
                fields={'failure_reason': 'rate_limit', 'failure_retryable': True, 'failure_resets_at': float(reset),
                        'route_provider': 'anthropic', 'route_model': FABLE[0]})
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='d', assignee='a')
        ratio.begin_tick(conn)
        reap(conn, tid, admit=hub['cfg'], **wall)
        assert pin_tests._events(conn, tid, 'run_route')[-1]['tier_reason'] == 'default+ratio:anthropic'
        clock[0] += 10  # inside the run's doubling backoff: the live pool serving fable never lifts that part
        ratio.begin_tick(conn)
        assert _route(conn, tid) == (*ASTRA, 'default+ratio:anthropic+quota_reroute')


def test_start_counts_are_this_hosts_default_starts_in_the_window(hub):
    clock = hub['clock']
    with kbc.connect() as conn:
        _starts(conn, 'xai-oauth', 2)
        clock[0] += ratio.SHARE_WINDOW_SECONDS + 60  # those two age out
        _starts(conn, 'anthropic', 3)
        _starts(conn, 'openai-codex', 2, foreign=True)  # another host's runs on a replica of this board
        _starts(conn, 'anthropic', 1, reason='pinned')  # not a default-tier start
        _starts(conn, '', 2)  # a receipt with no provider counts toward the default provider
        _starts(conn, 'openai-codex', 1, reason='default+wall:anthropic')
        assert ratio.count_starts(conn, clock[0], 'openai-codex') == {'anthropic': 3, 'openai-codex': 3}


def test_the_window_bound_finds_the_first_recent_event_across_id_gaps(hub):
    clock = hub['clock']
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='t', assignee='a')
        for offset in (0, 10, 20, 30):
            clock[0] = 1_800_000_000 + offset
            kb._append_event(conn, tid, 'note', {})
        ids = [r[0] for r in conn.execute("SELECT id FROM task_events WHERE kind='note' ORDER BY id")]
        conn.execute('DELETE FROM task_events WHERE id=?', (ids[2],))  # a gap where the bound would land
        conn.commit()
        assert ratio._first_event_id_since(conn, 1_800_000_015) == ids[2]
        assert conn.execute('SELECT MIN(id) FROM task_events WHERE id >= ?', (ids[2],)).fetchone()[0] == ids[3]
        assert ratio._first_event_id_since(conn, 1_800_000_031) > ids[3]
        assert ratio._first_event_id_since(conn, 0) == conn.execute('SELECT MIN(id) FROM task_events').fetchone()[0]


def test_choose_is_the_largest_deficit_with_registry_ties():
    order = ['anthropic', 'openai-codex', 'xai-oauth']
    assert ratio.choose(order, {'anthropic': 2, 'openai-codex': 1, 'xai-oauth': 1}, {})[0] == 'anthropic'
    assert ratio.choose(order, {p: 1 for p in order}, {'anthropic': 1, 'openai-codex': 1})[0] == 'xai-oauth'
    pool, target, share, deficit = ratio.choose(order[:2], {'anthropic': 3, 'openai-codex': 1}, {'anthropic': 4})
    assert (pool, target, share, deficit) == ('openai-codex', 0.25, 0.0, 0.25)
