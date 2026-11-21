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
    return kb.create_task(conn, title=title, body=body, assignee='a', model_override=PIN[0],
                          provider_override=PIN[1], pin_mandatory=mandatory)


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


def test_far_wall_reroutes_a_pin_in_class_and_records_it(board, monkeypatch, all_assignees_spawnable):
    clock = _clock(monkeypatch)
    _, cfg = _deep(monkeypatch)
    with kbc.connect() as conn:
        tid = _walled_card(conn, cfg, clock)
        assert dispatch.check_respawn_guard(conn, tid) is None
        assert _tick(conn) == [PEER]
        task = kb.get_task(conn, tid)
        assert (task.model_override, task.provider_override) == PIN  # I2: row never written
        route = _events(conn, tid, 'run_route')[-1]
        assert (route['model'], route['provider']) == PEER
        assert route['tier_reason'] == 'pinned+pin_quota_reroute:openai-codex'
        assert tier.owner_model_pin(conn, task) == PEER  # the owner spawns the reroute's -m/--provider
        [event] = _events(conn, tid, 'model_reroute')
        assert event == {'from_model': PIN[0], 'from_provider': PIN[1], 'to_model': PEER[0],
                         'to_provider': PEER[1], 'walled_provider': 'openai-codex',
                         'resets_at': float(clock[0] - 301 + 55 * 3600),
                         'episode_id': episode(conn, tid)['episode_id'], 'run_id': task.current_run_id}
        [comment] = _comments(conn, tid)
        assert 'gpt-6-astra (openai-codex) is out until' in comment and 'claude-opus-5-5 (anthropic)' in comment
        assert f'set-model {tid} gpt-6-astra --provider openai-codex --mandatory' in comment


@pytest.mark.parametrize('why', ['flag', 'marker'])
def test_mandatory_pin_is_never_rerouted(board, monkeypatch, all_assignees_spawnable, why):
    clock = _clock(monkeypatch)
    _, cfg = _deep(monkeypatch)
    with kbc.connect() as conn:
        tid = _walled_card(conn, cfg, clock, mandatory=why == 'flag',
                           body='must run on astra\nrequire_model: true' if why == 'marker' else None)
        assert dispatch.check_respawn_guard(conn, tid) == 'subscription_quota_reset'
        assert _tick(conn) == []
        assert _events(conn, tid, 'model_reroute') == [] and _comments(conn, tid) == []


@pytest.mark.parametrize('hours, min_hours, moved', [(2, None, False), (5.9, None, False), (6.2, None, True),
                                                      (2, 1, True), (2, 'bogus', False), (2, -1, False), (55, 100, False)])
def test_wall_shorter_than_the_threshold_holds(board, monkeypatch, all_assignees_spawnable, hours, min_hours, moved):
    clock = _clock(monkeypatch)
    _, cfg = _deep(monkeypatch, cfg={} if min_hours is None else {'pin_quota_reroute_min_hours': min_hours})
    with kbc.connect() as conn:
        tid = _pinned_card(conn)
        reap(conn, tid, admit=cfg, **_codex_wall(clock[0] + int(hours * 3600)))
        clock[0] += 301
        assert _tick(conn) == ([PEER] if moved else [])


def test_review_lane_selects_independent_model_despite_author_wall(board, monkeypatch):
    clock = _clock(monkeypatch)
    _deep(monkeypatch)
    with kbc.connect() as conn:
        tid = _pinned_card(conn)
        conn.execute("UPDATE tasks SET status='review' WHERE id=?", (tid,))
        conn.commit()
        reap(conn, tid, lane='review', provider='openai-codex', **_codex_wall(clock[0] + 55 * 3600))
        clock[0] += 301
        assert dispatch.check_respawn_guard(conn, tid, lane='review') is None
        task = kb.get_task(conn, tid)
        assert tier.resolve_dispatch_tier(conn, task, lane='review') == (*PEER, 'review_unpinned')
        assert (task.model_override, task.provider_override) == PIN
        assert _events(conn, tid, 'model_reroute') == []


def test_review_lane_after_a_closed_episode_allows_the_author_model(board, monkeypatch, all_assignees_spawnable):
    """A prior author run does not disqualify an otherwise available strong reviewer."""
    clock = _clock(monkeypatch)
    _, cfg = _deep(monkeypatch)
    with kbc.connect() as conn:
        tid = _walled_card(conn, cfg, clock)
        assert _tick(conn) == [PEER]
        kb.reclaim_task(conn, tid, reason='test')
        task = kb.get_task(conn, tid)
        assert tier.resolve_dispatch_tier(conn, task, lane='ready')[:2] == PEER
        assert tier.resolve_dispatch_tier(conn, task, lane='review') == (*PEER, 'review_unpinned')
        assert (task.model_override, task.provider_override) == PIN
        # Only the preceding ready run was quota-rerouted; review is its own policy.
        assert len(_events(conn, tid, 'model_reroute')) == 1


@pytest.mark.parametrize('classes', [0, 2])
def test_pin_in_no_class_or_two_classes_holds(board, monkeypatch, all_assignees_spawnable, classes):
    clock = _clock(monkeypatch)
    config, cfg = _deep(monkeypatch)
    routes = config['model_routing']['routes']
    if classes == 0:
        routes['deep'] = [r for r in routes['deep'] if r['model'] != PIN[0]]
    else:
        routes['standard'] = [dict(routes['deep'][1]), dict(routes['deep'][0], model='claude-sonnet-5-5')]
    with kbc.connect() as conn:
        tid = _walled_card(conn, cfg, clock)
        assert _tick(conn) == []
        assert _events(conn, tid, 'model_reroute') == []


def _block_peer(blocked, monkeypatch, config, clock):
    """Take the anthropic class peer out of reach the ``blocked`` way."""
    until = float(clock[0] + 3600)
    {'provider_file': lambda: monkeypatch.setattr(walls, 'active_walls', lambda **_k: {'anthropic': until}),
     'live_pool': lambda: monkeypatch.setattr(
         walls, 'live_quota_wall', lambda provider, model: until if provider == 'anthropic' else None),
     'disabled': lambda: config['providers'].__setitem__('anthropic', {'enabled': False}),
     'routing_off': lambda: config['model_routing'].__setitem__('enabled', False),
     'none': lambda: None}[blocked]()


@pytest.mark.parametrize('blocked', ['provider_file', 'live_pool', 'disabled', 'routing_off', 'none'])
def test_no_eligible_peer_holds_and_never_falls_back_to_tier_models(
        board, monkeypatch, all_assignees_spawnable, blocked):
    """Only the class peer counts: with it walled, disabled or routing off the pin waits, even though
    the tier's strong model (also opus here) is not walled; ``none`` is the moving control."""
    clock = _clock(monkeypatch)
    config, cfg = _deep(monkeypatch)
    with kbc.connect() as conn:
        _walled_card(conn, cfg, clock)
        _block_peer(blocked, monkeypatch, config, clock)
        assert _tick(conn) == ([PEER] if blocked == 'none' else [])


def test_live_walled_first_peer_is_skipped_for_the_next(board, monkeypatch, all_assignees_spawnable):
    clock = _clock(monkeypatch)
    config, cfg = _deep(monkeypatch)
    sonnet = dict(config['model_routing']['routes']['deep'][0], model='grok-5', provider='xai-oauth')
    config['model_routing']['routes']['deep'].append(sonnet)
    monkeypatch.setattr(walls, 'live_quota_wall',
                        lambda provider, model: float(clock[0] + 3600) if provider == 'anthropic' else None)
    with kbc.connect() as conn:
        tid = _walled_card(conn, cfg, clock)
        assert _tick(conn) == [('grok-5', 'xai-oauth')]
        assert _events(conn, tid, 'model_reroute')[0]['to_provider'] == 'xai-oauth'


def test_an_expired_earlier_wall_does_not_rule_out_its_provider(board, monkeypatch, all_assignees_spawnable):
    """The card once hit anthropic (1 h, long over); now codex is out 55 h: anthropic is a free peer."""
    clock = _clock(monkeypatch)
    _, cfg = _deep(monkeypatch)
    with kbc.connect() as conn:
        tid = _pinned_card(conn)
        reap(conn, tid, admit=cfg, text='Anthropic usage limit resets in ~1h', provider='anthropic',
             fields={'route_provider': 'anthropic', 'route_model': 'claude-opus-5-5'})
        clock[0] += 2 * 3600
        reap(conn, tid, admit=cfg, **_codex_wall(clock[0] + 55 * 3600))
        clock[0] += 301
        assert _tick(conn) == [PEER]


def test_pin_walled_only_by_another_cards_wall_holds(board, monkeypatch, all_assignees_spawnable):
    """The card's live episode walled anthropic; codex is walled only by another card's wall. That known
    codex wall counts for rule 1, but the only class peer is anthropic, behind the card's own wall: the pin
    waits, never into a second wall."""
    clock = _clock(monkeypatch)
    _deep(monkeypatch)
    with kbc.connect() as conn:
        tid = _pinned_card(conn)
        reap(conn, tid, text='Anthropic usage limit resets in ~11h', provider='anthropic',
             fields={'route_provider': 'anthropic', 'route_model': 'claude-opus-5-5'})
        monkeypatch.setattr(walls, 'active_walls', lambda **_k: {'openai-codex': float(clock[0] + 55 * 3600)})
        clock[0] += 301
        assert dispatch.check_respawn_guard(conn, tid) == 'subscription_quota_reset'
        assert _tick(conn) == []


def test_each_run_records_a_reroute_and_one_comment_per_episode(board, monkeypatch, all_assignees_spawnable):
    clock = _clock(monkeypatch)
    _, cfg = _deep(monkeypatch)
    with kbc.connect() as conn:
        tid = _walled_card(conn, cfg, clock)
        assert _tick(conn) == [PEER]
        # The rerouted run is reclaimed (not a wall: the episode continues); the next tick reroutes again.
        kb.reclaim_task(conn, tid, reason='test')
        assert _tick(conn) == [PEER]
        reroutes = _events(conn, tid, 'model_reroute')
        assert len(reroutes) == 2 and len({r['run_id'] for r in reroutes}) == 2
        assert len({r['episode_id'] for r in reroutes}) == 1
        assert len(_comments(conn, tid)) == 1


def test_after_the_reset_the_run_is_back_on_the_pin(board, monkeypatch, all_assignees_spawnable):
    clock = _clock(monkeypatch)
    _, cfg = _deep(monkeypatch)
    with kbc.connect() as conn:
        tid = _walled_card(conn, cfg, clock)
        clock[0] = int(episode(conn, tid)['until']) + 1
        assert dispatch.check_respawn_guard(conn, tid) is None
        assert _tick(conn) == [PIN]
        assert _events(conn, tid, 'run_route')[-1]['tier_reason'] == 'pinned'
        assert _events(conn, tid, 'model_reroute') == []


def test_preview_matches_admission_and_writes_nothing(board, monkeypatch, all_assignees_spawnable):
    clock = _clock(monkeypatch)
    _, cfg = _deep(monkeypatch)
    with kbc.connect() as conn:
        tid = _walled_card(conn, cfg, clock)
        before = conn.total_changes
        preview = task_route(conn, tid)
        assert conn.total_changes == before
        assert _events(conn, tid, 'model_reroute') == []
        assert _tick(conn) == [preview[:2]] == [PEER]


def test_failed_record_keeps_the_pin(board, monkeypatch, all_assignees_spawnable):
    """I3: a reroute that cannot be recorded does not happen; the run stays on its pin."""
    clock = _clock(monkeypatch)
    _, cfg = _deep(monkeypatch)

    def broken(*_a, **_k):
        raise RuntimeError('disk full')
    with kbc.connect() as conn:
        tid = _walled_card(conn, cfg, clock)
        monkeypatch.setattr(pr, 'record', broken)
        assert _tick(conn) == [PIN]
        assert _events(conn, tid, 'run_route')[-1]['tier_reason'] == 'pinned+pin_quota_reroute_unrecorded'
        assert _events(conn, tid, 'model_tier')[-1]['reason'] != 'pinned+pin_quota_reroute:openai-codex'


# --- a fresh card on a live-pool wall (kanban-dd62aef001) -----------------------------------------

def _live_codex_wall(monkeypatch, until):
    monkeypatch.setattr(walls, 'live_quota_wall', lambda p, m: until[0] if (m, p) == PIN else None)


def test_fresh_pin_on_a_far_live_wall_reroutes_on_its_first_run(board, monkeypatch, all_assignees_spawnable):
    """Review 5871917933: a fresh pin has no run to put the wall in its episode, so a pre-claim hold on the
    live wall used to wait the whole wall out. The live wall now stands in for rule 1 on a fresh card."""
    clock = _clock(monkeypatch)
    _deep(monkeypatch)
    until = [float(clock[0] + 55 * 3600)]
    _live_codex_wall(monkeypatch, until)
    with kbc.connect() as conn:
        tid = _pinned_card(conn)
        assert _tick(conn) == [PEER]  # no exit-75 run into the wall first
        assert _events(conn, tid, 'dispatch_skipped') == []
        task = kb.get_task(conn, tid)
        assert (task.model_override, task.provider_override) == PIN
        [event] = _events(conn, tid, 'model_reroute')
        assert (event['walled_provider'], event['resets_at'], event['episode_id']) == ('openai-codex', until[0], None)
        [comment] = _comments(conn, tid)
        assert 'gpt-6-astra (openai-codex) is out until' in comment
        # Its rerouted run ends: the card is no longer fresh, and its own reroute keeps it off the wall.
        kb.reclaim_task(conn, tid, reason='test')
        clock[0] += 3600
        assert _tick(conn) == [PEER]
        assert len(_events(conn, tid, 'model_reroute')) == 2 and len(_comments(conn, tid)) == 1
        # After the reset the pin is back, even though the card still carries the reroute history.
        kb.reclaim_task(conn, tid, reason='test')
        clock[0] = int(until[0]) + 1
        until[0] = None
        assert _tick(conn) == [PIN]


@pytest.mark.parametrize('case', ['mandatory', 'near_wall', 'no_peer'])
def test_fresh_pin_with_no_reroute_is_held_on_its_live_wall(board, monkeypatch, all_assignees_spawnable, case):
    from hermes_cli.kanban_diagnostics import compute_task_diagnostics
    clock = _clock(monkeypatch)
    config, _ = _deep(monkeypatch)
    if case == 'no_peer':
        _block_peer('disabled', monkeypatch, config, clock)
    until = [float(clock[0] + (4 if case == 'near_wall' else 55) * 3600)]
    _live_codex_wall(monkeypatch, until)
    with kbc.connect() as conn:
        tid = _pinned_card(conn, mandatory=case == 'mandatory')
        for _ in range(4):
            assert _tick(conn) == []
            clock[0] += 1800
        [skip] = _events(conn, tid, 'dispatch_skipped')
        assert (skip['reason'], skip['until']) == ('pinned_quota_wall', int(until[0]))
        assert _events(conn, tid, 'model_reroute') == []
        # Board diagnostics read the deliberate hold as held_in_ready (warning), never stranded_in_ready.
        diags = compute_task_diagnostics(kb.get_task(conn, tid), kb.list_events(conn, tid),
                                         kb.list_runs(conn, tid), now=clock[0])
        assert [(d.kind, d.severity) for d in diags] == [('held_in_ready', 'warning')]
        assert diags[0].data['hold_reason'] == 'pinned_quota_wall' and diags[0].data['hold_until'] == int(until[0])


# --- a non-fresh pin on a wall the host already knows (t_c21c170e, 2026-11-12) ---------------------------

def _known_codex_wall(monkeypatch, evidence, until):
    if evidence == 'provider_file':
        monkeypatch.setattr(walls, 'active_walls', lambda **_k: {'openai-codex': until})
    else:
        monkeypatch.setattr(walls, 'live_quota_wall', lambda p, m: until if (m, p) == PIN else None)


def _card_with_an_earlier_run(conn, **kw):
    """A pinned card whose earlier run ended without any wall (it ran on its pin, then was reclaimed)."""
    tid = _pinned_card(conn, **kw)
    assert _tick(conn) == [PIN]
    kb.reclaim_task(conn, tid, reason='test')
    assert kb.get_task(conn, tid).status == 'ready'
    assert conn.execute('SELECT COUNT(*) FROM task_runs WHERE task_id=? AND ended_at IS NOT NULL',
                        (tid,)).fetchone()[0] == 1
    return tid


@pytest.mark.parametrize('evidence', ['provider_file', 'live_pool'])
def test_non_fresh_pin_on_a_known_wall_reroutes_before_its_next_spawn(
        board, monkeypatch, all_assignees_spawnable, evidence):
    """MBP run 13181: a pinned card with earlier runs, woken while provider_walls.json held codex, spawned on
    codex and exited 75; only run 13184 rerouted. The known wall on the pin's provider now counts for
    rule 1 whatever the card's history, so no run pays the wall to learn it."""
    clock = _clock(monkeypatch)
    _deep(monkeypatch)
    with kbc.connect() as conn:
        tid = _card_with_an_earlier_run(conn)
        until = float(clock[0] + 55 * 3600)
        _known_codex_wall(monkeypatch, evidence, until)
        clock[0] += 60
        assert _tick(conn) == [PEER]
        task = kb.get_task(conn, tid)
        assert (task.model_override, task.provider_override) == PIN
        assert _events(conn, tid, 'run_route')[-1]['tier_reason'] == 'pinned+pin_quota_reroute:openai-codex'
        [event] = _events(conn, tid, 'model_reroute')
        assert (event['walled_provider'], event['resets_at'], event['episode_id']) == ('openai-codex', until, None)
        assert len(_comments(conn, tid)) == 1


@pytest.mark.parametrize('case', ['flag', 'marker', 'peer_walled', 'near_wall'])
def test_non_fresh_pin_on_a_known_wall_controls_never_move(board, monkeypatch, all_assignees_spawnable, case):
    """Controls: a mandatory pin (column or ``require_model: true`` line), a walled only peer, or a wall under
    the min-hours threshold keep the pin exactly as before."""
    clock = _clock(monkeypatch)
    _deep(monkeypatch)
    with kbc.connect() as conn:
        tid = _card_with_an_earlier_run(conn, mandatory=case == 'flag',
                                        body='must run on astra\nrequire_model: true' if case == 'marker' else None)
        hours = 2 if case == 'near_wall' else 55
        monkeypatch.setattr(walls, 'active_walls', lambda **_k: {'openai-codex': float(clock[0] + hours * 3600)})
        if case == 'peer_walled':
            monkeypatch.setattr(walls, 'live_quota_wall',
                                lambda p, m: float(clock[0] + 3600) if p == 'anthropic' else None)
        clock[0] += 60
        assert _tick(conn) == [PIN]
        assert _events(conn, tid, 'model_reroute') == [] and _comments(conn, tid) == []


@pytest.mark.parametrize('own_hours, file_hours', [(2, 55), (55, 2)])
def test_a_short_own_wall_never_hides_a_longer_known_wall(
        board, monkeypatch, all_assignees_spawnable, own_hours, file_hours):
    """Review nit on PR #1514: rule 1 read the provider file only when the card had no wall of its own on the
    pin's provider, so a 2 h own wall hid a 55 h provider-file wall and the pin waited out the 2 h only to
    spawn into the long wall. The later of the two now decides (and is the recorded reset); the card's own
    episode id is kept so the one-comment-per-episode rule still holds."""
    clock = _clock(monkeypatch)
    _, cfg = _deep(monkeypatch)
    with kbc.connect() as conn:
        start = clock[0]
        tid = _walled_card(conn, cfg, clock, hours=own_hours)
        file_until = float(start + file_hours * 3600)
        monkeypatch.setattr(walls, 'active_walls', lambda **_k: {'openai-codex': file_until})
        assert _tick(conn) == [PEER]
        [event] = _events(conn, tid, 'model_reroute')
        assert event['resets_at'] == float(start + max(own_hours, file_hours) * 3600)
        live = episode(conn, tid)
        assert live and live['episode_id'] and event['episode_id'] == live['episode_id']
        assert len(_comments(conn, tid)) == 1


@pytest.mark.parametrize('file_hours, live_hours', [(3, 55), (55, 3)])
def test_known_wall_is_the_later_of_the_provider_file_and_the_live_pool(
        board, monkeypatch, all_assignees_spawnable, file_hours, live_hours):
    """PR #1535 verification gap G1: ``known_wall`` folds the provider file and the live pool into one reset,
    the later of the two. Taking the earlier one would let a 3 h file wall (under the 6 h threshold) hide a
    55 h live wall, and the fresh pin would spawn into it; either order must reroute and record +55 h."""
    clock = _clock(monkeypatch)
    _deep(monkeypatch)
    start = clock[0]
    monkeypatch.setattr(walls, 'active_walls', lambda **_k: {'openai-codex': float(start + file_hours * 3600)})
    monkeypatch.setattr(walls, 'live_quota_wall',
                        lambda p, m: float(start + live_hours * 3600) if (p, m) == (PIN[1], PIN[0]) else None)
    later = float(start + 55 * 3600)
    with kbc.connect() as conn:
        tid = _pinned_card(conn)
        assert _tick(conn) == [PEER]
        [event] = _events(conn, tid, 'model_reroute')
        assert event['resets_at'] == later
    assert pr.known_wall(PIN, start) == {'openai-codex': later}


# --- the review lane on a known wall (hub run 174730, 2026-11-12) ---------------------------------------

GROK = ('grok-5', 'xai-oauth')


def _review_card(conn, body=None):
    """A card in review whose implementer ran opus; opus is also its eligible strong reviewer."""
    tid = kb.create_task(conn, title='review me', body=body, assignee='a', model_override=PEER[0],
                         provider_override=PEER[1])
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


# --- the mandatory mark: create, set-model, tool, sync ---------------------------------------------

def test_create_flag_requires_a_pin_and_persists(board):
    with kbc.connect() as conn:
        with pytest.raises(ValueError, match='pin_mandatory requires a model pin'):
            kb.create_task(conn, title='no pin', assignee='a', pin_mandatory=True)
        tid = _pinned_card(conn, mandatory=True)
        assert kb.get_task(conn, tid).model_pin_mandatory == 1
        assert kb.get_task(conn, _pinned_card(conn)).model_pin_mandatory is None


def _cli(*args):
    from hermes_cli import kanban
    from hermes_cli.kanban_parser import build_parser
    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers())
    return kanban.kanban_command(parser.parse_args(['kanban', *args]))


def _set_model(tid, model=None, provider=None, mandatory=None):
    args = ['set-model', tid] + ([model] if model else []) + (['--provider', provider] if provider else [])
    return _cli(*args, *({True: ['--mandatory'], False: ['--no-mandatory']}.get(mandatory, [])))


def test_set_model_mandatory_flags(board, capsys, monkeypatch):
    monkeypatch.setattr('hermes_cli.kanban_model_assignment.validate_task_pin',
                        lambda conn, tid, model, provider: (model, provider))
    with kbc.connect() as conn:
        tid = _pinned_card(conn)
    assert _set_model(tid, mandatory=True) == 0
    with kbc.connect() as conn:
        task = kb.get_task(conn, tid)
        assert task.model_pin_mandatory == 1 and (task.model_override, task.provider_override) == PIN
        assert _events(conn, tid, 'model_pin_mandatory_set') == [{'mandatory': True}]
    assert 'mandatory' in capsys.readouterr().out
    assert _set_model(tid, *PEER) == 0  # a new pin keeps the card's mark
    with kbc.connect() as conn:
        assert kb.get_task(conn, tid).model_pin_mandatory == 1
    assert _set_model(tid, mandatory=False) == 0
    with kbc.connect() as conn:
        assert kb.get_task(conn, tid).model_pin_mandatory is None
    assert _set_model(tid, *PIN, mandatory=True) == 0
    assert _set_model(tid, 'none') == 0  # clearing the pin clears its mark
    with kbc.connect() as conn:
        task = kb.get_task(conn, tid)
        assert task.model_override is None and task.model_pin_mandatory is None
    assert _set_model(tid, mandatory=True) == 2  # nothing to mark
    assert 'needs a model pin' in capsys.readouterr().err


def test_cli_create_pin_mandatory(board, monkeypatch, all_assignees_spawnable):
    assert _cli('create', 'cli pin', '--assignee', 'a', '--model', PIN[0], '--provider', PIN[1],
                '--pin-mandatory') == 0
    with kbc.connect() as conn:
        tid = conn.execute("SELECT id FROM tasks WHERE title='cli pin'").fetchone()[0]
        assert kb.get_task(conn, tid).model_pin_mandatory == 1


def test_kanban_create_tool_passes_pin_mandatory(board, monkeypatch):
    from tools import kanban_tools as kt
    seen = {}
    real = kb.create_task

    def spy(conn, **kw):
        seen.update(kw)
        return real(conn, **kw)
    monkeypatch.setattr(kb, 'create_task', spy)
    import json
    out = json.loads(kt._handle_create({'title': 'tool pin', 'assignee': 'a', 'model': PIN[0],
                                        'provider': PIN[1], 'pin_mandatory': True, 'requires': []}))
    assert seen.get('pin_mandatory') is True, out
    from tools.kanban_tools_schemas import KANBAN_CREATE_SCHEMA
    assert KANBAN_CREATE_SCHEMA['parameters']['properties']['pin_mandatory']['type'] == 'boolean'


def test_board_sync_carries_the_mark_and_a_synced_mandatory_copy_holds(board, monkeypatch, all_assignees_spawnable):
    assert 'model_pin_mandatory' in kbsync.SYNCED_COLUMNS['tasks']
    clock = _clock(monkeypatch)
    _, cfg = _deep(monkeypatch)
    with kbc.connect() as conn:
        tid = _walled_card(conn, cfg, clock)
        # A pull from another machine lands the column as written there.
        conn.execute('UPDATE tasks SET model_pin_mandatory=1 WHERE id=?', (tid,))
        conn.commit()
        assert _tick(conn) == []
