"""A pacing-held default provider moves a card's route to an unheld peer of its class (``kanban_paced_reroute``).

2026-11-15 (hub core 992c0cb5e3b): openai-codex was pacing-``held`` (limit 0) until 11-20 22:40Z but not walled, so
every default-routed card waited ~5 days while anthropic, the other deep-class provider, was ``paced`` with free
slots. ``resolve_dispatch_tier`` moved a route only off a wall; a pacing hold never records one.
"""
import json
import sqlite3

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_llm_pacing_dispatch as lpd
from hermes_cli import kanban_model_tier as tier
from hermes_cli import kanban_paced_reroute as paced
from hermes_cli import kanban_parent_unclaimed as kpu
from hermes_cli import kanban_queue_position as kqp
from hermes_cli import kanban_provider_walls as walls
from tests.hermes_cli import test_kanban_pin_reroute as pin_tests
from tests.hermes_cli.test_kanban_quota_episode import board as board  # noqa: PLC0414 -- the self-alias is load-bearing: it suppresses F811 for the pytest fixture parameter shadowing this import

PIN, PEER, GROK = pin_tests.PIN, pin_tests.PEER, pin_tests.GROK
HELD_FAR = 5 * 86400  # 11-15 16:52Z -> 11-20 22:40Z


@pytest.fixture
def clock(monkeypatch):
    clock = pin_tests._clock(monkeypatch)
    monkeypatch.setattr(paced.time, 'time', lambda: clock[0])
    monkeypatch.setattr(lpd.time, 'time', lambda: clock[0])
    monkeypatch.setattr(lpd, '_CACHE', {})
    return clock


def _pools(clock, **states):
    """Store ``provider=(state, until_offset or None)`` paces in this home's ``llm_pacing.json``, read just now."""
    data = {}
    for name, (state, offset) in states.items():
        provider = name.replace('_', '-')
        data[provider] = {'read_at': clock[0], 'running': [], 'samples': [], 'pace': {
            'provider': provider, 'state': state, 'factor': 0.0 if state == 'held' else 0.07,
            'until': None if offset is None else float(clock[0] + offset), 'detail': ''}}
    lpd.state_path().write_text(json.dumps(data), encoding='utf-8')


def _deep(monkeypatch, cfg=None, grok=False):
    config, cfg = pin_tests._deep(monkeypatch, cfg)
    if grok:
        deep = config['model_routing']['routes']['deep']
        deep.append(dict(deep[0], model=GROK[0], provider=GROK[1]))
    return config, cfg


def _route(conn, tid, lane='ready'):
    return tier.resolve_dispatch_tier(conn, kb.get_task(conn, tid), lane=lane)


def test_held_far_default_route_moves_to_the_unheld_class_peer(board, clock, monkeypatch, all_assignees_spawnable):
    _deep(monkeypatch)
    _pools(clock, openai_codex=('held', HELD_FAR), anthropic=('paced', None))
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='default card', assignee='a')
        assert _route(conn, tid) == (*PEER, 'default+paced:openai-codex')
        # The governor judges the moved route on its own provider: anthropic paced with a free slot claims it.
        assert lpd.candidate_providers(conn, kb.get_task(conn, tid), 'ready') == ['anthropic']
        assert pin_tests._tick(conn) == [PEER]
        receipt = pin_tests._events(conn, tid, 'run_route')[-1]
        assert (receipt['model'], receipt['provider'], receipt['tier_reason']) == (*PEER, 'default+paced:openai-codex')
        persisted = kb.get_task(conn, tid)
        assert (persisted.model_override, persisted.provider_override) == (None, None)  # the row never moves


@pytest.mark.parametrize('until', [None, HELD_FAR])
def test_held_pool_with_unknown_or_far_reset_moves_the_route(board, clock, monkeypatch, until):
    """A held pool with no known reset admits nothing for an unknown time: it is never "near"."""
    _deep(monkeypatch)
    _pools(clock, openai_codex=('held', until))
    with kbc.connect() as conn:
        assert _route(conn, kb.create_task(conn, title='d', assignee='a')) == (*PEER, 'default+paced:openai-codex')


def test_held_near_reset_keeps_the_default_route(board, clock, monkeypatch):
    _deep(monkeypatch)
    _pools(clock, openai_codex=('held', 1800))
    with kbc.connect() as conn:
        assert _route(conn, kb.create_task(conn, title='d', assignee='a')) == (*PIN, 'default')


@pytest.mark.parametrize('after, until, moved', [
    (0, 1800, True), (1799, 1800, True), (1800, 1800, False), (None, 3599, False), (None, 3601, True),
    # An unreadable, negative or non-finite value is the 3600 s default, never "never" or "always".
    ('junk', 1800, False), (-5, 1800, False), ('nan', 1800, False), ('inf', 7200, True), ('junk', 7200, True)])
def test_reroute_after_s_sets_the_threshold(board, clock, monkeypatch, after, until, moved):
    """``kanban.llm_pacing.reroute_after_s`` (default 3600): a hold resetting sooner keeps the route."""
    _deep(monkeypatch, {} if after is None else {'llm_pacing': {'reroute_after_s': after}})
    _pools(clock, openai_codex=('held', until))
    with kbc.connect() as conn:
        route = _route(conn, kb.create_task(conn, title='d', assignee='a'))
    assert route == ((*PEER, 'default+paced:openai-codex') if moved else (*PIN, 'default'))


@pytest.mark.parametrize('state', ['paced', 'open', 'unknown'])
def test_a_pool_that_is_not_held_never_moves_the_route(board, clock, monkeypatch, state):
    """A ``paced`` pool keeps limit >= 1 (``admit_limit``): the card waits its turn there, it is not rerouted."""
    _deep(monkeypatch)
    _pools(clock, openai_codex=(state, HELD_FAR))
    with kbc.connect() as conn:
        assert _route(conn, kb.create_task(conn, title='d', assignee='a')) == (*PIN, 'default')


def test_non_mandatory_pin_on_a_held_provider_moves_to_its_class_peer(board, clock, monkeypatch,
                                                                       all_assignees_spawnable):
    _deep(monkeypatch)
    _pools(clock, openai_codex=('held', HELD_FAR))
    with kbc.connect() as conn:
        tid = pin_tests._pinned_card(conn)
        assert _route(conn, tid) == (*PEER, 'pinned+paced:openai-codex')
        assert pin_tests._tick(conn) == [PEER]
        assert pin_tests._events(conn, tid, 'run_route')[-1]['tier_reason'] == 'pinned+paced:openai-codex'
        task = kb.get_task(conn, tid)
        assert (task.model_override, task.provider_override) == PIN  # the pin itself is unchanged
        assert tier.owner_model_pin(conn, task) == PEER  # the owner spawns the moved route


@pytest.mark.parametrize('why', ['flag', 'marker'])
def test_mandatory_pin_keeps_its_held_provider(board, clock, monkeypatch, why):
    _deep(monkeypatch)
    _pools(clock, openai_codex=('held', HELD_FAR))
    with kbc.connect() as conn:
        tid = pin_tests._pinned_card(conn, mandatory=why == 'flag',
                                     body=walls.REQUIRE_MODEL_MARKER if why == 'marker' else None)
        assert _route(conn, tid) == (None, None, 'pinned')  # the pin runs as pinned


def test_require_model_default_card_keeps_its_held_route(board, clock, monkeypatch):
    _deep(monkeypatch)
    _pools(clock, openai_codex=('held', HELD_FAR))
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='needs astra', body=f'astra only\n{walls.REQUIRE_MODEL_MARKER}', assignee='a')
        assert _route(conn, tid) == (*PIN, 'default')
        prose = kb.create_task(conn, title='prose', body='a card may say `require_model` in prose', assignee='a')
        assert _route(conn, prose) == (*PEER, 'default+paced:openai-codex')


@pytest.mark.parametrize('peer', ['held', 'walled', 'live_wall'])
def test_no_unheld_unwalled_peer_keeps_the_base_route(board, clock, monkeypatch, peer):
    """Never onto another held or walled pool: with no free peer the governor holds the card as before."""
    _deep(monkeypatch)
    _pools(clock, openai_codex=('held', HELD_FAR), anthropic=('held', 600) if peer == 'held' else ('paced', None))
    if peer == 'walled':
        monkeypatch.setattr(walls, 'active_walls', lambda **_k: {'anthropic': float(clock[0] + 3600)})
    elif peer == 'live_wall':
        monkeypatch.setattr(walls, 'live_quota_wall', lambda p, m: float(clock[0] + 3600) if p == 'anthropic' else None)
    with kbc.connect() as conn:
        assert _route(conn, kb.create_task(conn, title='d', assignee='a')) == (*PIN, 'default')
        assert _route(conn, pin_tests._pinned_card(conn)) == (None, None, 'pinned')


def test_review_unpinned_on_a_held_provider_moves_to_a_free_independent_reviewer(board, clock, monkeypatch):
    _deep(monkeypatch, {'llm_pacing': {'providers': ['anthropic', 'openai-codex', 'xai-oauth']}}, grok=True)
    with kbc.connect() as conn:
        tid = pin_tests._review_card(conn)
        assert _route(conn, tid, lane='review') == (*PIN, 'review_unpinned')  # nothing held yet
        _pools(clock, openai_codex=('held', HELD_FAR))
        assert _route(conn, tid, lane='review') == (*GROK, 'review_unpinned+paced:openai-codex')
        task = kb.get_task(conn, tid)
        assert tier.apply_model_tier(conn, task, lane='review') == 'review_unpinned+paced:openai-codex'
        assert (task.model_override, task.provider_override) == GROK
        _pools(clock, openai_codex=('held', HELD_FAR), xai_oauth=('held', HELD_FAR))
        assert _route(conn, tid, lane='review') == (*PIN, 'review_unpinned')  # every reviewer held: keep


def test_review_unpinned_with_require_model_keeps_its_held_route(board, clock, monkeypatch):
    _deep(monkeypatch, grok=True)
    _pools(clock, openai_codex=('held', HELD_FAR))
    with kbc.connect() as conn:
        tid = pin_tests._review_card(conn, body=walls.REQUIRE_MODEL_MARKER)
        assert _route(conn, tid, lane='review') == (*PIN, 'review_unpinned')


@pytest.mark.parametrize('case', ['stale_read', 'pacing_off', 'ungoverned', 'class_selector'])
def test_no_trusted_hold_on_a_concrete_route_keeps_it(board, clock, monkeypatch, case):
    """Only a fresh stored hold on a governed provider of a concrete route moves it."""
    cfg = {'llm_pacing': {'enabled': False}} if case == 'pacing_off' else {}
    if case == 'ungoverned':
        cfg = {'llm_pacing': {'providers': ['anthropic']}}
    if case == 'class_selector':
        cfg = {'default_model': 'class:deep', 'default_provider': 'auto'}
    _deep(monkeypatch, cfg)
    _pools(clock, openai_codex=('held', HELD_FAR))
    if case == 'stale_read':
        clock[0] += lpd.STALE_READ_INTERVALS * 300
    with kbc.connect() as conn:
        route = _route(conn, kb.create_task(conn, title='d', assignee='a'))
    assert '+paced:' not in route[2]


def test_queue_note_and_stall_notice_name_the_reroute(board, clock, monkeypatch):
    _deep(monkeypatch)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='d', assignee='a')
        assert 'reroute' not in (kqp.position(conn, kb.get_task(conn, tid)) or {})  # nothing held: no note
        _pools(clock, openai_codex=('held', HELD_FAR))
        place = kqp.position(conn, kb.get_task(conn, tid))
    words = 'default+paced:openai-codex -> anthropic/claude-opus-5-5'
    assert place['reroute'] == words
    assert kqp.describe(place).startswith(f'next run rerouted: {words}; ')
    assert kpu._queue_words(place).startswith(f'next run rerouted: {words}; ')
    unplaceable = {'lane': 'unplaceable', 'runs_on': ['x'], 'missing': ['x'], 'machines': ['hub'], 'reroute': words}
    assert kpu._queue_words(unplaceable).count('next run rerouted') == 1


def test_a_failed_probe_keeps_the_route(board, clock, monkeypatch):
    _deep(monkeypatch)
    _pools(clock, openai_codex=('held', HELD_FAR))

    def boom(*_a, **_k):
        raise RuntimeError('policy unreadable')
    monkeypatch.setattr('hermes_cli.kanban_quota_episode.alternate_route', boom)
    with kbc.connect() as conn:
        assert _route(conn, kb.create_task(conn, title='d', assignee='a')) == (*PIN, 'default')


def test_a_sqlite_fault_in_the_probe_propagates(board, clock, monkeypatch):
    """As the wall reroutes do: admission's own error path decides, never a silent keep on a broken board."""
    _deep(monkeypatch)
    _pools(clock, openai_codex=('held', HELD_FAR))

    def locked(*_a, **_k):
        raise sqlite3.OperationalError('database is locked')
    monkeypatch.setattr('hermes_cli.kanban_quota_episode.alternate_route', locked)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='d', assignee='a')
        with pytest.raises(sqlite3.OperationalError):
            _route(conn, tid)


@pytest.mark.parametrize('block', ['walled', 'live_wall'])
def test_review_unpinned_skips_a_walled_independent_reviewer(board, clock, monkeypatch, block):
    _deep(monkeypatch, grok=True)
    _pools(clock, openai_codex=('held', HELD_FAR))
    if block == 'walled':
        monkeypatch.setattr(walls, 'active_walls', lambda **_k: {'xai-oauth': float(clock[0] + 3600)})
    else:
        monkeypatch.setattr(walls, 'live_quota_wall', lambda p, m: float(clock[0] + 3600) if p == 'xai-oauth' else None)
    with kbc.connect() as conn:
        assert _route(conn, pin_tests._review_card(conn), lane='review') == (*PIN, 'review_unpinned')


@pytest.mark.parametrize('card', ['class_pin', 'strong_tier_pattern'])
def test_class_pins_and_other_tier_reasons_keep_their_route(board, clock, monkeypatch, card):
    """Only default, review_unpinned and concrete pins move: a ``class:*`` pin is the allocator's to place, and a
    strong-tier keep-pattern card (here on the held provider) keeps the tier its pattern chose."""
    _deep(monkeypatch, {'strong_model': PIN[0], 'strong_provider': PIN[1], 'default_model_keep_patterns': ['security']})
    _pools(clock, openai_codex=('held', HELD_FAR))
    with kbc.connect() as conn:
        if card == 'class_pin':
            tid = kb.create_task(conn, title='d', assignee='a', model_override='class:deep', provider_override='auto')
            assert _route(conn, tid)[2] == 'pinned'
        else:
            tid = kb.create_task(conn, title='security audit', assignee='a')
            assert _route(conn, tid) == (*PIN, 'pattern:security')


def test_route_words_resolve_nothing_while_no_pool_is_held(board, clock, monkeypatch):
    """Card reads stay cheap: with no held pool no route is resolved; a held pool with a near reset adds no note."""
    _deep(monkeypatch)
    calls = []
    real = tier.resolve_dispatch_tier
    monkeypatch.setattr(tier, 'resolve_dispatch_tier', lambda *a, **k: calls.append(1) or real(*a, **k))
    with kbc.connect() as conn:
        task = kb.get_task(conn, kb.create_task(conn, title='d', assignee='a'))
        assert paced.route_words(conn, task) is None and calls == []
        _pools(clock, openai_codex=('held', 600))
        assert paced.route_words(conn, task) is None and calls == [1]
        assert 'reroute' not in (kqp.position(conn, task) or {})
