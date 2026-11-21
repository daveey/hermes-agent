"""A review route on a pacing-held provider moves to the next reviewer free of holds. [daveey]

2026-11-15 (t_01a1e15ae11c8d41): Claude-implemented reviews resolved to gpt-6-astra on openai-codex, pacing-``held``
until 11-20 22:40Z. ``paced_route`` moved an unpinned reviewer (#3362) but not an explicit handoff ``review_model``,
and a review run's receipt did not say which hold it left or until when. Both entry paths are covered: a card in
the review lane and a ready-lane card carrying ``sdlc-review``.

Fixture (``test_kanban_pin_reroute``): the implementer ran claude-opus-5-5 (also the strong tier).
Anthropic is explicitly held in review fallback probes: authorship no longer makes a route unavailable.
Deep routes are opus (anthropic), gpt-6-astra (openai-codex), grok-5 (xai-oauth).
"""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_model_tier as tier
from hermes_cli import kanban_review_model as review
from tests.hermes_cli import test_kanban_model_tier_paced_reroute as paced_tests
from tests.hermes_cli import test_kanban_pin_reroute as pin_tests
from tests.hermes_cli.test_kanban_quota_episode import board as board  # noqa: PLC0414 -- the self-alias is load-bearing: it suppresses F811 for the pytest fixture parameter shadowing this import

clock = paced_tests.clock
PIN, PEER, GROK = pin_tests.PIN, pin_tests.PEER, pin_tests.GROK  # astra/codex, opus/anthropic (implementer), grok
HELD_FAR = paced_tests.HELD_FAR
GOVERNED = {'llm_pacing': {'providers': ['anthropic', 'openai-codex', 'xai-oauth']}}
ENTRIES = ['review_lane', 'ready_skill']


def _review_pools(clock, **states):
    """Hold strong's pool explicitly so these probes exercise the remaining policy routes."""
    states.setdefault('anthropic', ('held', HELD_FAR))
    paced_tests._pools(clock, **states)


def _card(conn, entry, *, override=None):
    """A Claude-implemented card under review on *entry*; ``override`` = the handoff's review_model/provider."""
    tid = kb.create_task(conn, title='review me', assignee='a', model_override=PEER[0], provider_override=PEER[1],
                         skills=['sdlc-review'] if entry == 'ready_skill' else None)
    if override is not None:
        kb.claim_task(conn, tid)
        assert kb.request_review(conn, tid, metadata={'review_model': override[0], 'review_provider': override[1]})
    status = 'review' if entry == 'review_lane' else 'ready'
    conn.execute('UPDATE tasks SET status=? WHERE id=?', (status, tid))
    conn.commit()
    return tid


def _lane(entry):
    return 'review' if entry == 'review_lane' else 'ready'


def _route(conn, tid, entry):
    return tier.resolve_dispatch_tier(conn, kb.get_task(conn, tid), lane=_lane(entry))


def _apply(conn, tid, entry):
    """One real dispatcher tick (claim from the card's column, admission, spawn): the spawned route + receipts."""
    seen = pin_tests._tick(conn)
    assert len(seen) == 1, seen
    tier_event = pin_tests._events(conn, tid, 'model_tier')[-1]
    route_event = pin_tests._events(conn, tid, 'run_route')[-1]
    assert route_event['lane'] == _lane(entry)
    return tier_event['reason'], seen[0], tier_event, route_event


@pytest.mark.parametrize('entry', ENTRIES)
def test_a_held_reviewer_moves_to_the_next_one_free_of_holds(board, clock, monkeypatch, all_assignees_spawnable, entry):
    """(a) + (e): the base resolver never picks a held pool: with codex held the unpinned reviewer is grok, concrete
    from the start, so there is no move (and no ``paced`` block) to record."""
    paced_tests._deep(monkeypatch, GOVERNED, grok=True)
    _review_pools(clock, openai_codex=('held', HELD_FAR), anthropic=('held', HELD_FAR))
    with kbc.connect() as conn:
        tid = _card(conn, entry)
        assert tier.resolve_model_tier(conn, kb.get_task(conn, tid), lane=_lane(entry)) == (*GROK, 'review_unpinned')
        assert _route(conn, tid, entry) == (*GROK, 'review_unpinned')
        reason, route, tier_event, route_event = _apply(conn, tid, entry)
    assert reason == 'review_unpinned' and route == GROK
    assert 'paced' not in tier_event
    assert tier_event['review_independence'] == 'separate_execution'
    assert 'implementer_models' not in tier_event
    assert (route_event['model'], route_event['provider'], route_event['tier_reason']) == (
        *GROK, 'review_unpinned')


@pytest.mark.parametrize('entry', ENTRIES)
def test_a_held_pool_with_no_known_reset_records_until_none(board, clock, monkeypatch, all_assignees_spawnable, entry):
    paced_tests._deep(monkeypatch, GOVERNED, grok=True)
    _review_pools(clock, openai_codex=('held', None))
    with kbc.connect() as conn:
        reason, route, tier_event, _ = _apply(conn, _card(conn, entry), entry)
    assert (reason, route) == ('review_unpinned', GROK)  # a held pool with no known reset is held, never chosen
    assert 'paced' not in tier_event


@pytest.mark.parametrize('entry', ENTRIES)
@pytest.mark.parametrize('state', ['paced', 'open'])
def test_a_reviewer_whose_provider_is_only_paced_keeps_its_route(board, clock, monkeypatch, all_assignees_spawnable, entry, state):
    """(b): a paced pool still admits (limit >= 1): the review waits for a slot there, never rerouted."""
    paced_tests._deep(monkeypatch, GOVERNED, grok=True)
    _review_pools(clock, openai_codex=(state, HELD_FAR))
    with kbc.connect() as conn:
        tid = _card(conn, entry)
        assert _route(conn, tid, entry) == (*PIN, 'review_unpinned')
        reason, route, tier_event, _ = _apply(conn, tid, entry)
    assert (reason, route) == ('review_unpinned', PIN)
    assert 'paced' not in tier_event


@pytest.mark.parametrize('entry', ENTRIES)
def test_a_reviewer_on_a_held_pool_with_a_near_reset_is_still_skipped(board, clock, monkeypatch, all_assignees_spawnable, entry):
    """``reroute_after_s`` (3600 s) is the pace reroute's threshold for default routes; the review resolver skips
    every held pool, near reset or far, so a review never waits for an unavailable pool to reopen."""
    paced_tests._deep(monkeypatch, GOVERNED, grok=True)
    _review_pools(clock, openai_codex=('held', 1800))
    with kbc.connect() as conn:
        assert _route(conn, _card(conn, entry), entry) == (*GROK, 'review_unpinned')


@pytest.mark.parametrize('entry', ENTRIES)
@pytest.mark.parametrize('fleet', ['two_reviewers_held', 'every_reviewer_held'])
def test_no_reviewer_free_of_holds_is_unavailable(
        board, clock, monkeypatch, all_assignees_spawnable, entry, fleet):
    """(c): every configured review pool, including the author's, is held; nothing may spawn."""
    paced_tests._deep(monkeypatch, GOVERNED, grok=fleet == 'every_reviewer_held')
    pools = {'openai_codex': ('held', HELD_FAR), 'anthropic': ('held', HELD_FAR)}
    if fleet == 'every_reviewer_held':
        pools['xai_oauth'] = ('held', HELD_FAR)
    _review_pools(clock, **pools)
    with kbc.connect() as conn:
        tid = _card(conn, entry)
        with pytest.raises(review.ReviewModelUnavailable):
            _route(conn, tid, entry)
        assert pin_tests._tick(conn) == []  # nothing spawns onto a held pool
        assert pin_tests._events(conn, tid, 'model_tier') == []
        assert pin_tests._events(conn, tid, 'run_route') == []


@pytest.mark.parametrize('entry', ENTRIES)
def test_a_held_review_override_is_not_taken_by_the_fast_return(board, clock, monkeypatch, all_assignees_spawnable, entry):
    """(d): the handoff asked for astra on held codex. The explicit override used to return before the held/walled
    check; with anthropic also held the review runs on grok (reason ``review_override``)."""
    paced_tests._deep(monkeypatch, GOVERNED, grok=True)
    _review_pools(clock, openai_codex=('held', HELD_FAR), anthropic=('held', HELD_FAR))
    with kbc.connect() as conn:
        tid = _card(conn, entry, override=PIN)
        assert tier.resolve_model_tier(conn, kb.get_task(conn, tid), lane=_lane(entry)) == (*GROK, 'review_override')
        assert _route(conn, tid, entry) == (*GROK, 'review_override')
        reason, route, tier_event, route_event = _apply(conn, tid, entry)
        stored = kb.get_task(conn, tid)
    assert (reason, route) == ('review_override', GROK)
    assert 'paced' not in tier_event
    assert route_event['tier_reason'] == 'review_override'
    assert (stored.model_override, stored.provider_override) == PEER  # the row never moves


@pytest.mark.parametrize('entry', ENTRIES)
@pytest.mark.parametrize('case', ['paced_only', 'no_free_peer', 'unregistered_peer', 'require_model', 'class'])
def test_a_review_override_on_a_held_pool_moves_only_to_a_registered_free_peer(
        board, clock, monkeypatch, all_assignees_spawnable, entry, case):
    """(b)/(c) for overrides: a paced (not held) pool keeps the requested route; on a held pool the override moves
    to a free peer this host's registry accepts, else the review is refused. ``require_model`` on the
    implementation card is the implementer's pin, so it does not keep the reviewer on the held pool. A class
    override resolves to a concrete reviewer."""
    paced_tests._deep(monkeypatch, GOVERNED, grok=case != 'no_free_peer')
    _review_pools(clock, openai_codex=('paced' if case == 'paced_only' else 'held', HELD_FAR),
                       anthropic=('held', HELD_FAR))
    if case == 'unregistered_peer':
        real = review.registry_refusal
        monkeypatch.setattr(review, 'registry_refusal',
                            lambda m, p, a: RuntimeError('not registered') if m == GROK[0] else real(m, p, a))
    override = ('class:deep', 'auto') if case == 'class' else PIN
    with kbc.connect() as conn:
        tid = _card(conn, entry, override=override)
        if case == 'require_model':
            conn.execute('UPDATE tasks SET body=? WHERE id=?', (pin_tests.walls.REQUIRE_MODEL_MARKER, tid))
            conn.commit()
        if case in ('no_free_peer', 'unregistered_peer'):
            with pytest.raises(review.ReviewModelUnavailable):
                _route(conn, tid, entry)
            return
        expected = {'paced_only': (*PIN, 'review_override'), 'require_model': (*GROK, 'review_override'),
                    'class': (*GROK, 'review_override')}[case]
        assert _route(conn, tid, entry) == expected


def test_a_registry_refusal_only_gates_override_fallbacks(board, clock, monkeypatch, all_assignees_spawnable):
    """The unpinned reviewer's candidates are the registry's own deep routes (``independent_routes``), so the
    override-only registry check does not apply to it."""
    paced_tests._deep(monkeypatch, GOVERNED, grok=True)
    _review_pools(clock, openai_codex=('held', HELD_FAR))
    monkeypatch.setattr(review, 'registry_refusal', lambda m, p, a: RuntimeError('refused') if m == GROK[0] else None)
    with kbc.connect() as conn:
        assert _route(conn, _card(conn, 'review_lane'), 'review_lane') == (*GROK, 'review_unpinned')


def test_a_held_refused_override_substitute_moves_too(board, clock, monkeypatch, all_assignees_spawnable):
    """``review_override_refused``: the registry refused the handoff's model; the substitute is chosen among the free
    registered reviewers (astra and opus are held, so grok), and the receipt still names the requested model."""
    paced_tests._deep(monkeypatch, GOVERNED, grok=True)
    _review_pools(clock, openai_codex=('held', HELD_FAR))
    real = review.registry_refusal

    def refuse_fable(model, provider, assignee):
        if model == 'claude-fable-5-2':
            return type('Refusal', (), {'code': 'model_pin_not_in_routing_registry', 'allowed': [PIN[0]]})()
        return real(model, provider, assignee)
    monkeypatch.setattr(review, 'registry_refusal', refuse_fable)
    with kbc.connect() as conn:
        tid = _card(conn, 'review_lane', override=('claude-fable-5-2', 'anthropic'))
        assert tier.resolve_model_tier(conn, kb.get_task(conn, tid), lane='review') == (
            *GROK, 'review_override_refused')
        reason, route, tier_event, _ = _apply(conn, tid, 'review_lane')
    assert (reason, route) == ('review_override_refused', GROK)
    assert tier_event['requested']['model'] == 'claude-fable-5-2'
    assert 'paced' not in tier_event


def test_a_failed_paced_receipt_keeps_the_reroute_and_its_reason(board, clock, monkeypatch, all_assignees_spawnable):
    """The ``paced`` block is detail: a fault reading it never refuses admission or undoes a taken pace offer (the
    governor's move still reaches a review run through ``kanban_pace_reroute``)."""
    from hermes_cli import kanban_pace_reroute as pace_offer
    from hermes_cli import kanban_paced_reroute as paced

    def boom(*_a, **_k):
        raise RuntimeError('state unreadable')
    paced_tests._deep(monkeypatch, GOVERNED, grok=True)
    _review_pools(clock, openai_codex=('held', HELD_FAR))
    monkeypatch.setattr(paced, 'receipt', boom)
    with kbc.connect() as conn:
        tid = _card(conn, 'review_lane')
        task = kb.get_task(conn, tid)
        assert task is not None
        pace_offer.offer(tid, _route(conn, tid, 'review_lane'), (PIN[0], PIN[1], 'xai-oauth'))
        try:
            reason = tier.apply_model_tier(conn, task, lane='review')
        finally:
            pace_offer.clear()
        tier_event = pin_tests._events(conn, tid, 'model_tier')[-1]
    assert reason == 'review_unpinned+paced:xai-oauth' and (task.model_override, task.provider_override) == PIN
    assert 'paced' not in tier_event


def test_only_review_receipts_carry_the_paced_block(board, clock, monkeypatch, all_assignees_spawnable):
    """A default route moved off the same hold keeps its existing receipt shape (``tier_reason`` only)."""
    paced_tests._deep(monkeypatch)
    paced_tests._pools(clock, openai_codex=('held', HELD_FAR))
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='default card', assignee='a')
        assert pin_tests._tick(conn) == [PEER]
        tier_event = pin_tests._events(conn, tid, 'model_tier')[-1]
    assert tier_event['reason'] == 'default+paced:openai-codex' and 'paced' not in tier_event


@pytest.mark.parametrize('reason', ['review_unpinned', 'review_override', 'default', 'pinned+pin_quota_reroute'])
def test_a_route_that_did_not_move_off_a_hold_has_no_paced_receipt(board, clock, monkeypatch, reason):
    from hermes_cli import kanban_paced_reroute as paced
    _, cfg = paced_tests._deep(monkeypatch, GOVERNED, grok=True)
    _review_pools(clock, openai_codex=('held', HELD_FAR))
    with kbc.connect() as conn:
        task = kb.get_task(conn, _card(conn, 'review_lane'))
        assert paced.receipt(conn, task, reason, lane='review', cfg=cfg) is None
