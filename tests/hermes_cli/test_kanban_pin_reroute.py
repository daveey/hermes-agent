"""A far quota wall moves a non-mandatory Kanban card pin to a same-class peer, one run at a time.

paintbot-tools M4 (t_01a19715a1a9ccef, 11-01) was pinned to openai-codex/gpt-6-astra and held for a
~56 h Codex wall although claude-opus-5-5 in the same ``deep`` class was free. David, 11-11
(t_01a19990897b9d9b): pins are reroutable within their class unless marked mandatory.
"""
import argparse

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_db_sync as kbsync
from hermes_cli import kanban_llm_pacing_dispatch as pacing
from hermes_cli import kanban_model_tier as tier
from hermes_cli import kanban_pin_reroute as pr
from hermes_cli import kanban_provider_walls as walls
from hermes_cli.kanban_quota_episode import episode
from hermes_cli.kanban_shared_blockers import task_route
from tests.hermes_cli.test_kanban_quota_episode import board as board, reap, routing  # noqa: PLC0414 -- the self-alias is load-bearing: it suppresses F811 for the pytest fixture parameter shadowing this import

HUB_CFG = {'default_model': 'gpt-6-astra', 'default_provider': 'openai-codex',
           'strong_model': 'claude-opus-5-5', 'strong_provider': 'anthropic'}
PIN = ('gpt-6-astra', 'openai-codex')
PEER = ('claude-opus-5-5', 'anthropic')


def _codex_wall(reset):
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


def _deep(monkeypatch, cfg=None, extra=()):
    """Assignee policy: ``deep`` = opus (anthropic) + astra (codex); tier config = the hub's."""
    config, _ = routing(monkeypatch)
    std = config['model_routing']['routes']['standard']
    config['model_routing']['routes'] = {'deep': [dict(std[0], model='claude-opus-5-5'),
                                                  dict(std[1], model='gpt-6-astra'), *extra]}
    cfg = dict(HUB_CFG, **(cfg or {}))
    monkeypatch.setattr(tier, 'load_tier_config', lambda: cfg)
    monkeypatch.setattr(walls, 'active_walls', lambda **_k: {})
    monkeypatch.setattr(walls, 'live_quota_wall', lambda *_a, **_k: None)
    return config, cfg


def _pinned_card(conn, title='M4 pinned to astra', body=None, mandatory=False):
    tid = kb.create_task(conn, title='Legacy route fixture', assignee='a')
    # Replay only in the isolated test DB, never through policy-checked ingress.
    conn.execute('UPDATE tasks SET title=?, body=?, model_override=?, provider_override=?, model_pin_mandatory=? WHERE id=?',
                 (title, body, *PIN, 1 if mandatory else None, tid))
    conn.commit()
    return tid


def _events(conn, tid, kind):
    return [kb._json_dict(r[0]) for r in conn.execute(
        'SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id', (tid, kind))]


def _comments(conn, tid):
    return [r[0] for r in conn.execute(
        "SELECT body FROM task_comments WHERE task_id=? AND author='kanban-dispatcher' ORDER BY id", (tid,))]


def _tick(conn):
    seen = []
    dispatch.dispatch_once(conn, spawn_fn=lambda task, ws: seen.append((task.model_override, task.provider_override)))
    return seen


def _walled_card(conn, cfg, clock, hours=55, **kw):
    tid = _pinned_card(conn, **kw)
    reap(conn, tid, admit=cfg, **_codex_wall(clock[0] + hours * 3600))
    clock[0] += 301
    return tid


# The historical wall utilities still serve evidence readers, not next-run pin authority.
@pytest.mark.parametrize('hours,min_hours,moved', [(2, None, False), (5.9, None, False),
                                                 (6.2, None, True), (2, 1, True),
                                                 (2, 'bogus', False), (2, -1, False), (55, 100, False)])
def test_historical_wall_threshold_helper(board, monkeypatch, hours, min_hours, moved):
    clock = _clock(monkeypatch)
    _, cfg = _deep(monkeypatch, cfg={} if min_hours is None else {'pin_quota_reroute_min_hours': min_hours})
    with kbc.connect() as conn:
        tid = _pinned_card(conn)
        current = {PIN[1]: clock[0] + hours * 3600}
        assert pr.pinned_route(conn, kb.get_task(conn, tid), lane='ready', now=clock[0], cfg=cfg,
                               current=current) == (PEER if moved else None)


@pytest.mark.parametrize('blocked', ['provider_file', 'live_pool', 'disabled', 'routing_off', 'none'])
def test_historical_peer_helper_respects_current_eligibility(board, monkeypatch, blocked):
    clock = _clock(monkeypatch)
    config, cfg = _deep(monkeypatch)
    until = float(clock[0] + 55 * 3600)
    controls = {
        'provider_file': lambda: monkeypatch.setattr(walls, 'active_walls', lambda **kw: {'anthropic': until}),
        'live_pool': lambda: monkeypatch.setattr(
            walls, 'live_quota_wall', lambda p, m: until if p == 'anthropic' else None),
        'disabled': lambda: config['providers'].update(anthropic={'enabled': False}),
        'routing_off': lambda: config['model_routing'].update(enabled=False),
        'none': lambda: None,
    }
    controls[blocked]()
    with kbc.connect() as conn:
        tid = _pinned_card(conn)
        assert pr.pinned_route(conn, kb.get_task(conn, tid), lane='ready', now=clock[0], cfg=cfg,
                               current={PIN[1]: until}) == (PEER if blocked == 'none' else None)


def test_historical_reroute_records_distinct_runs_and_one_comment(board, monkeypatch):
    clock = _clock(monkeypatch)
    _deep(monkeypatch)
    with kbc.connect() as conn:
        tid = _pinned_card(conn)
        kb._append_event(conn, tid, 'quota_episode', {'provider': PIN[1],
                         'until': clock[0] + 55 * 3600, 'episode_id': 213})
        conn.commit()
        for _ in range(2):
            task = kb.claim_task(conn, tid, claimer=kb._claimer_id())
            assert task is not None
            with kb.write_txn(conn):
                event = pr.record(conn, task, PIN, PEER)
            assert event['run_id'] == task.current_run_id
            assert event['resets_at'] == clock[0] + 55 * 3600
            kb.reclaim_task(conn, tid, reason='test')
        recorded = _events(conn, tid, 'model_reroute')
        assert len(recorded) == 2 and len({event['run_id'] for event in recorded}) == 2
        assert len(_comments(conn, tid)) == 1
        assert 'selected dynamically' in _comments(conn, tid)[0]
        assert '--mandatory' not in _comments(conn, tid)[0]


# Concrete legacy rows are evidence only. Class allocation happens in the owning worker;
# the dispatcher must not resurrect mandatory/threshold/provider restrictions from a row.
@pytest.mark.parametrize('mandatory', [False, True])
@pytest.mark.parametrize('marker', [False, True])
@pytest.mark.parametrize('hours', [2, 5.9, 6.2, 55])
def test_legacy_restrictions_do_not_choose_or_rewrite_a_ready_route(board, monkeypatch, mandatory, marker, hours):
    clock = _clock(monkeypatch)
    _deep(monkeypatch)
    with kbc.connect() as conn:
        tid = _pinned_card(conn, mandatory=mandatory, body='require_model: true' if marker else None)
        _known_codex_wall(monkeypatch, 'live_pool', float(clock[0] + hours * 3600))
        task = kb.get_task(conn, tid)
        before = conn.total_changes
        assert tier.resolve_dispatch_tier(conn, task, lane='ready') == ('class:standard', 'auto', 'default')
        assert conn.total_changes == before
        assert (task.model_override, task.provider_override) == PIN
        assert not pr.is_mandatory_pin(task)
        assert _events(conn, tid, 'model_reroute') == []


@pytest.mark.parametrize('classes', [0, 1, 2])
def test_legacy_model_class_membership_does_not_constrain_new_selection(board, monkeypatch, classes):
    config, _ = _deep(monkeypatch)
    routes = config['model_routing']['routes']
    if classes == 0:
        routes['deep'] = [route for route in routes['deep'] if route['model'] != PIN[0]]
    elif classes == 2:
        routes['standard'] = list(routes['deep'])
    with kbc.connect() as conn:
        tid = _pinned_card(conn)
        assert tier.resolve_dispatch_tier(conn, kb.get_task(conn, tid), lane='ready') == (
            'class:standard', 'auto', 'default')


def test_preview_matches_admission_and_preserves_concrete_row(board, monkeypatch, all_assignees_spawnable):
    import json
    config, _ = _deep(monkeypatch)
    config['model_routing']['routes']['standard'] = list(config['model_routing']['routes']['deep'])
    (board / 'config.yaml').write_text(json.dumps(config))
    with kbc.connect() as conn:
        tid = _pinned_card(conn, mandatory=True)
        before = conn.total_changes
        preview = task_route(conn, tid)
        assert conn.total_changes == before
        assert preview[:2] == ('class:standard', None)  # preview has no concrete provider yet
        assert _tick(conn) == [(preview[0], preview[1] or 'auto')] == [('class:standard', 'auto')]
        stored = kb.get_task(conn, tid)
        assert (stored.model_override, stored.provider_override) == (None, None)
        assert stored.model_pin_mandatory is None
        assert tier.owner_model_pin(conn, stored) == ('class:standard', 'auto')
        route = _events(conn, tid, 'run_route')[-1]
        assert route['model'] == 'class:standard' and route['tier_reason'] == 'default'
        assert _events(conn, tid, 'model_reroute') == []


def test_review_lane_selects_independent_model_despite_author_wall(board, monkeypatch):
    _clock(monkeypatch)
    _deep(monkeypatch)
    with kbc.connect() as conn:
        tid = _pinned_card(conn, mandatory=True)
        task = kb.get_task(conn, tid)
        assert tier.resolve_dispatch_tier(conn, task, lane='review') == (*PEER, 'review_unpinned')
        assert (task.model_override, task.provider_override) == PIN
        assert _events(conn, tid, 'model_reroute') == []


@pytest.mark.parametrize('file_hours, live_hours', [(3, 55), (55, 3)])
def test_known_wall_is_the_later_of_the_provider_file_and_the_live_pool(board, monkeypatch, file_hours, live_hours):
    clock = _clock(monkeypatch)
    _deep(monkeypatch)
    start = clock[0]
    monkeypatch.setattr(walls, 'active_walls', lambda **kw: {'openai-codex': float(start + file_hours * 3600)})
    monkeypatch.setattr(walls, 'live_quota_wall', lambda p, m: float(start + live_hours * 3600) if (m, p) == PIN else None)
    assert pr.known_wall(PIN, start) == {'openai-codex': float(start + 55 * 3600)}


def _known_codex_wall(monkeypatch, evidence, until):
    if evidence == 'provider_file':
        monkeypatch.setattr(walls, 'active_walls', lambda **kw: {'openai-codex': until})
    else:
        monkeypatch.setattr(walls, 'live_quota_wall', lambda p, m: until if (m, p) == PIN else None)


GROK = ('grok-5', 'xai-oauth')


def _review_card(conn, body=None):
    """A legacy-pinned card whose author attribution does not constrain reviewer eligibility."""
    tid = kb.create_task(conn, title='review me', assignee='a')
    conn.execute('UPDATE tasks SET body=?, model_override=?, provider_override=? WHERE id=?', (body, *PEER, tid))
    conn.execute("UPDATE tasks SET status='review' WHERE id=?", (tid,))
    conn.commit()
    return tid


def _hold_author_pool(monkeypatch, clock):
    """Exercise later policy candidates by holding strong, not by excluding its authorship."""
    monkeypatch.setattr(pacing, 'held_pools', lambda *_a: {'anthropic': float(clock[0] + 55 * 3600)})


@pytest.mark.parametrize('evidence', ['provider_file', 'live_pool'])
def test_review_unpinned_on_a_walled_provider_moves_to_a_free_reviewer(
        board, monkeypatch, evidence):
    clock = _clock(monkeypatch)
    config, _ = _deep(monkeypatch)
    _hold_author_pool(monkeypatch, clock)
    config['model_routing']['routes']['deep'].append(
        dict(config['model_routing']['routes']['deep'][0], model=GROK[0], provider=GROK[1]))
    with kbc.connect() as conn:
        tid = _review_card(conn)
        task = kb.get_task(conn, tid)
        assert tier.resolve_dispatch_tier(conn, task, lane='review') == (*PIN, 'review_unpinned')  # no wall yet
        _known_codex_wall(monkeypatch, evidence, float(clock[0] + 55 * 3600))
        assert tier.resolve_dispatch_tier(conn, task, lane='review') == (
            *GROK, 'review_unpinned')
        # Admission (before the first spawn) pins the free reviewer on the in-memory task and records it.
        reason = tier.apply_model_tier(conn, task, lane='review')
        assert reason == 'review_unpinned'
        assert (task.model_override, task.provider_override) == GROK
        tier_event = _events(conn, tid, 'model_tier')[-1]
        assert (tier_event['model'], tier_event['provider']) == GROK
        assert tier_event['review_independence'] == 'separate_execution'
        persisted = kb.get_task(conn, tid)
        assert (persisted.model_override, persisted.provider_override) == PEER  # the row never moves


@pytest.mark.parametrize('case', ['no_alternative', 'alternative_walled', 'marker'])
def test_review_resolution_excludes_walls_before_rerouting(board, monkeypatch, case):
    """With strong held, every remaining candidate must be unwalled at resolution."""
    clock = _clock(monkeypatch)
    config, _ = _deep(monkeypatch)
    _hold_author_pool(monkeypatch, clock)
    if case != 'no_alternative':
        config['model_routing']['routes']['deep'].append(
            dict(config['model_routing']['routes']['deep'][0], model=GROK[0], provider=GROK[1]))
    until = float(clock[0] + 55 * 3600)
    if case == 'alternative_walled':
        monkeypatch.setattr(walls, 'active_walls', lambda **_k: {'openai-codex': until, 'xai-oauth': until})
    else:
        monkeypatch.setattr(walls, 'active_walls', lambda **_k: {'openai-codex': until})
    with kbc.connect() as conn:
        tid = _review_card(conn, body='must review on astra\nrequire_model: true' if case == 'marker' else None)
        if case == 'marker':
            assert tier.resolve_dispatch_tier(conn, kb.get_task(conn, tid), lane='review') == (*GROK, 'review_unpinned')
        else:
            from hermes_cli.kanban_review_model import ReviewModelUnavailable
            with pytest.raises(ReviewModelUnavailable):
                tier.resolve_dispatch_tier(conn, kb.get_task(conn, tid), lane='review')


GROK_MINI = ('grok-5-mini', 'xai-oauth')


@pytest.mark.parametrize('case', ['later_free', 'none_free'])
def test_review_unpinned_skips_an_alternative_walled_only_in_the_live_pool(board, monkeypatch, case):
    """Mutant M4 (t_01a1d04d206e2c6e): ``_review_off_wall`` must check the live pool on each alternate, not
    only provider_walls.json. The first alternate (grok-5) is walled only by the live pool, which walls per
    model: the review moves to a later free reviewer (grok-5-mini, same provider, no wall) or, with none,
    keeps its route; never into grok-5's wall."""
    clock = _clock(monkeypatch)
    config, _ = _deep(monkeypatch)
    _hold_author_pool(monkeypatch, clock)
    deep = config['model_routing']['routes']['deep']
    deep.append(dict(deep[0], model=GROK[0], provider=GROK[1]))
    if case == 'later_free':
        deep.append(dict(deep[0], model=GROK_MINI[0], provider=GROK_MINI[1]))
    until = float(clock[0] + 55 * 3600)
    monkeypatch.setattr(walls, 'active_walls', lambda **_k: {'openai-codex': until})  # the base, file only
    monkeypatch.setattr(walls, 'live_quota_wall', lambda p, m: until if (m, p) == GROK else None)
    with kbc.connect() as conn:
        tid = _review_card(conn)
        if case == 'none_free':
            from hermes_cli.kanban_review_model import ReviewModelUnavailable
            with pytest.raises(ReviewModelUnavailable):
                tier.resolve_dispatch_tier(conn, kb.get_task(conn, tid), lane='review')
            return
        route = tier.resolve_dispatch_tier(conn, kb.get_task(conn, tid), lane='review')
        assert route[:2] != GROK
        assert route == (*GROK_MINI, 'review_unpinned')


# New mandatory restrictions are rejected; a later clear is still permitted.
def _cli(*args):
    from hermes_cli import kanban
    from hermes_cli.kanban_parser import build_parser
    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers())
    return kanban.kanban_command(parser.parse_args(['kanban', *args]))


def test_create_flag_rejects_without_creating_a_card(board):
    with kbc.connect() as conn:
        before = conn.execute('SELECT count(*) FROM tasks').fetchone()[0]
        with pytest.raises(ValueError, match='model-neutral'):
            kb.create_task(conn, title='no pin', assignee='a', pin_mandatory=True)
        assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == before


def test_set_model_mandatory_refusal_is_atomic_and_clear_remains_possible(board, capsys):
    with kbc.connect() as conn:
        tid = _pinned_card(conn, mandatory=True)
        before = _events(conn, tid, 'model_pin_mandatory_set')
    assert _cli('set-model', tid, '--mandatory') == 2
    with kbc.connect() as conn:
        assert kb.get_task(conn, tid).model_pin_mandatory == 1
        assert _events(conn, tid, 'model_pin_mandatory_set') == before
    assert _cli('set-model', tid, '--no-mandatory') == 0
    with kbc.connect() as conn:
        assert kb.get_task(conn, tid).model_pin_mandatory is None
    assert _cli('set-model', tid, 'class:deep') == 0
    with kbc.connect() as conn:
        assert kb.get_task(conn, tid).model_override == 'class:deep'


def test_cli_create_pin_mandatory_is_refused_without_write(board, all_assignees_spawnable):
    assert _cli('create', 'cli pin', '--assignee', 'a', '--model', PIN[0], '--provider', PIN[1], '--pin-mandatory') == 2
    with kbc.connect() as conn:
        assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == 0


def test_kanban_create_tool_refuses_mandatory_before_write(board):
    import json
    from tools import kanban_tools as kt
    with kbc.connect() as conn:
        before = conn.execute('SELECT count(*) FROM tasks').fetchone()[0]
        out = json.loads(kt._handle_create({'title': 'tool pin', 'assignee': 'a', 'model': PIN[0],
                                          'provider': PIN[1], 'pin_mandatory': True, 'requires': []}))
        assert 'model-neutral' in out['error']
        assert conn.execute('SELECT count(*) FROM tasks').fetchone()[0] == before


def test_board_sync_carries_legacy_evidence_without_dispatch_authority(board, monkeypatch):
    assert 'model_pin_mandatory' in kbsync.SYNCED_COLUMNS['tasks']
    _deep(monkeypatch)
    with kbc.connect() as conn:
        tid = _pinned_card(conn, mandatory=True)
        assert tier.resolve_dispatch_tier(conn, kb.get_task(conn, tid), lane='ready') == (
            'class:standard', 'auto', 'default')
        assert kb.get_task(conn, tid).model_pin_mandatory == 1
