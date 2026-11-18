"""A review never runs on any model that implemented the card (``implementer_models`` / ``is_review_run``).

Before: ``_implementer_model`` named ONE model (the latest ``review_requested`` handoff run, else the pin), so a card
implemented by several models (a pin quota reroute, a crashed retry on a peer, an upstream creator card) could be
reviewed by an earlier implementer, and the pace governor / paced reroute / ``_apply_model_tier`` could move a review
onto an implementer's model. Behavioural tests on a real SQLite board; pools, walls and the routing registry are
the fixtures the neighbouring review tests use.

Fixture: strong tier = opus (anthropic); deep routes opus, astra (openai-codex), grok (xai-oauth).
"""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_llm_pacing_dispatch as lpd
from hermes_cli import kanban_model_tier as tier
from hermes_cli import kanban_pace_reroute as pace_offer
from hermes_cli import kanban_paced_reroute as paced
from hermes_cli import kanban_provider_walls as walls
from hermes_cli import kanban_review_model as review
from tests.hermes_cli import test_kanban_model_tier_paced_reroute as paced_tests
from tests.hermes_cli import test_kanban_pin_reroute as pin_tests
from tests.hermes_cli.test_kanban_quota_episode import board as board  # noqa: PLC0414 -- the self-alias is load-bearing: it suppresses F811 for the pytest fixture parameter shadowing this import

clock = paced_tests.clock
OPUS, ASTRA, GROK = pin_tests.PEER[0], pin_tests.PIN[0], pin_tests.GROK[0]
PROVIDER = {OPUS: 'anthropic', ASTRA: 'openai-codex', GROK: 'xai-oauth'}
GOVERNED = {'llm_pacing': {'providers': ['anthropic', 'openai-codex', 'xai-oauth']}}
HELD_FAR = paced_tests.HELD_FAR


def _end_run(conn, run_id):
    conn.execute("UPDATE task_runs SET status='crashed', outcome='crashed', ended_at=1 WHERE id=?", (run_id,))
    conn.commit()


def _run(conn, tid, model=None, *, receipt=None, lane='ready'):
    """One finished run on *tid*. ``model`` = its ``worker_result.route_model`` (the live agent's model);
    ``receipt`` = its ``run_route.model``. Leaves the card in the ``ready`` column, unclaimed."""
    conn.execute("UPDATE tasks SET status=?, claim_lock=NULL, current_run_id=NULL WHERE id=?",
                 ('review' if lane == 'review' else 'ready', tid))
    conn.commit()
    claimed = (kb.claim_review_task if lane == 'review' else kb.claim_task)(conn, tid, claimer=kb._claimer_id())
    assert claimed is not None
    if receipt:
        kb._append_event(conn, tid, 'run_route', {'model': receipt, 'provider': PROVIDER.get(receipt),
                         'source': 'test', 'tier_reason': 'default', 'lane': lane}, run_id=claimed.current_run_id)
    if model:
        kb._append_event(conn, tid, 'worker_result', {'exit_code': 0, 'route_model': model},
                         run_id=claimed.current_run_id)
    _end_run(conn, claimed.current_run_id)
    conn.execute("UPDATE tasks SET status='ready', claim_lock=NULL, current_run_id=NULL WHERE id=?", (tid,))
    conn.commit()
    return claimed.current_run_id


def _card(conn, pin=None, *, title='implement it', creator=None, status='ready'):
    tid = kb.create_task(conn, title=title, assignee='a', creator_task_id=creator,
                         model_override=pin, provider_override=PROVIDER.get(pin))
    conn.execute('UPDATE tasks SET status=? WHERE id=?', (status, tid))
    conn.commit()
    return tid


def _models(conn, tid):
    task = kb.get_task(conn, tid)
    if hasattr(review, 'implementer_models'):
        return set(review.implementer_models(conn, task))
    # Exercise the baseline's single-model behavior, rather than failing on a
    # missing new helper before reaching a provenance assertion.
    model = review._implementer_model(conn, task)
    return {model} if model else set()


def _is_review(conn, task, lane):
    if hasattr(review, 'is_review_run'):
        return review.is_review_run(conn, task, lane)
    return lane == 'review' or 'sdlc-review' in (task.skills or [])


def _review_route(conn, tid, lane='review'):
    return tier.resolve_dispatch_tier(conn, kb.get_task(conn, tid), lane=lane)


@pytest.mark.parametrize('source', ['creator', 'child'])
def test_downstream_unpinned_route_excludes_source(board, clock, monkeypatch, source):
    _routing(monkeypatch)
    with kbc.connect() as conn:
        impl = _card(conn)
        _run(conn, impl, model=ASTRA)
        down = _card(conn, title='Independent exact-head review', creator=impl if source == 'creator' else None)
        if source == 'child':
            kb.link_tasks(conn, down, impl)
        assert _review_route(conn, down, 'ready')[:2] == (OPUS, 'anthropic')


def test_paced_route_rejects_unsafe_probe(board, clock, monkeypatch):
    _routing(monkeypatch)
    monkeypatch.setattr(paced, '_probe', lambda *a: (ASTRA, 'openai-codex', 'anthropic'))
    with kbc.connect() as conn:
        tid = _handoff_card(conn, None, [ASTRA])
        assert paced.paced_route(conn, kb.get_task(conn, tid), (OPUS, 'anthropic', 'review_unpinned'),
                                 lane='review', cfg=tier.load_tier_config()) is None


def test_governor_rejects_unsafe_review_peer(board, clock, monkeypatch):
    _routing(monkeypatch)
    from hermes_cli import kanban_llm_pacing as lp
    monkeypatch.setattr(paced, '_review_peer', lambda *a, **k: (ASTRA, 'openai-codex'))
    cfg = lp.config_from({'providers': ['anthropic', 'openai-codex'], 'enabled': True})
    with kbc.connect() as conn:
        tid = _handoff_card(conn, None, [ASTRA])
        assert lpd._pace_alternate_route(conn, kb.get_task(conn, tid), (OPUS, 'anthropic', 'review_unpinned'),
                                         'review', cfg, conn, [{'provider': 'anthropic',
                                                               'until': clock[0] + HELD_FAR}]) is None


# --- implementer_models: what counts as an implementer ------------------------------------------------------


def test_every_non_review_run_contributes_its_concrete_model(board):
    with kbc.connect() as conn:
        tid = _card(conn)
        _run(conn, tid, model=OPUS)
        _run(conn, tid, model=ASTRA)
        assert _models(conn, tid) == {OPUS, ASTRA}


def test_worker_result_route_model_beats_the_run_route_receipt_of_the_same_run(board):
    with kbc.connect() as conn:
        tid = _card(conn)
        _run(conn, tid, model=ASTRA, receipt=OPUS)  # the receipt named opus; the live agent ran astra
        assert _models(conn, tid) == {ASTRA}


def test_run_route_receipt_is_used_when_the_run_left_no_worker_result(board):
    with kbc.connect() as conn:
        tid = _card(conn)
        _run(conn, tid, receipt=GROK)
        assert _models(conn, tid) == {GROK}


def test_concrete_pin_joins_the_set_but_a_class_pin_does_not(board):
    with kbc.connect() as conn:
        pinned, classed = _card(conn, ASTRA), _card(conn)
        conn.execute("UPDATE tasks SET model_override='class:deep', provider_override='auto' WHERE id=?", (classed,))
        _run(conn, pinned, model=OPUS)
        _run(conn, classed, model=OPUS)
        conn.commit()
        assert _models(conn, pinned) == {OPUS, ASTRA}
        assert _models(conn, classed) == {OPUS}


def test_class_selector_receipts_are_not_concrete_models(board):
    with kbc.connect() as conn:
        tid = _card(conn)
        _run(conn, tid, model='class:deep', receipt='class:deep')
        assert _models(conn, tid) == set()


def test_review_runs_never_count_as_implementers(board):
    with kbc.connect() as conn:
        tid = _card(conn)
        _run(conn, tid, model=OPUS)
        _run(conn, tid, model=GROK, receipt=GROK, lane='review')  # a past review run
        assert _models(conn, tid) == {OPUS}


def test_is_review_run_by_lane_skill_and_card_column(board):
    with kbc.connect() as conn:
        plain, skilled = _card(conn), kb.create_task(conn, title='r', assignee='a', skills=['sdlc-review'])
        assert _is_review(conn, kb.get_task(conn, plain), 'review') is True
        assert _is_review(conn, kb.get_task(conn, plain), 'ready') is False
        assert _is_review(conn, kb.get_task(conn, skilled), 'ready') is True


def test_independent_title_collects_creator_and_children_models(board):
    """A downstream ``Independent ... review`` card was created by the implementer card; the cards linked under it
    (the work it gates) are implementers too."""
    with kbc.connect() as conn:
        impl = _card(conn, title='implement it')
        _run(conn, impl, model=OPUS)
        other = _card(conn, title='sibling fix')
        _run(conn, other, model=ASTRA)
        down = _card(conn, title='Independent code review of the thing', creator=impl)
        kb.link_tasks(conn, down, other)  # ``other`` is a child of the downstream card
        _run(conn, down, model=GROK, lane='review')  # its own review run: ignored
        assert _models(conn, down) == {OPUS, ASTRA}


def test_creator_is_ignored_for_a_title_that_is_not_an_independent_review(board):
    with kbc.connect() as conn:
        impl = _card(conn)
        _run(conn, impl, model=OPUS)
        child = _card(conn, title='follow-up work', creator=impl)
        assert _models(conn, child) == set()


def test_unreadable_creator_receipt_is_not_a_fault(board):
    with kbc.connect() as conn:
        down = _card(conn, title='Independent review of x')
        conn.execute("UPDATE task_events SET payload='{not json' WHERE task_id=? AND kind='created'", (down,))
        conn.commit()
        assert _models(conn, down) == set()


# --- resolution: a known set resolves a concrete independent route ----------------------------------------------


def _routing(monkeypatch, *, grok=True):
    paced_tests._deep(monkeypatch, GOVERNED, grok=grok)


def _handoff_card(conn, pin, ran):
    """A card pinned to *pin* whose finished runs used *ran*, now waiting in review."""
    tid = _card(conn, pin)
    for model in ran:
        _run(conn, tid, model=model)
    conn.execute("UPDATE tasks SET status='review' WHERE id=?", (tid,))
    conn.commit()
    return tid


def test_single_implementer_review_uses_the_strong_tier_when_it_is_independent(board, clock, monkeypatch):
    _routing(monkeypatch)
    with kbc.connect() as conn:
        tid = _handoff_card(conn, ASTRA, [ASTRA])
        assert _review_route(conn, tid) == (OPUS, 'anthropic', 'review_unpinned')


def test_multi_model_card_excludes_every_implementer(board, clock, monkeypatch):
    """opus implemented, then a reroute ran astra: neither may review; the strong tier (opus) is skipped for
    the next deep route, grok."""
    _routing(monkeypatch)
    with kbc.connect() as conn:
        tid = _handoff_card(conn, ASTRA, [OPUS, ASTRA])
        assert _review_route(conn, tid) == (GROK, 'xai-oauth', 'review_unpinned')
        reason = tier.apply_model_tier(conn, kb.get_task(conn, tid), lane='review')
        assert reason == 'review_unpinned'
        event = pin_tests._events(conn, tid, 'model_tier')[-1]
        assert event['model'] == GROK


def test_earlier_implementer_with_a_class_routed_last_run_still_excluded(board, clock, monkeypatch):
    """The pin names astra, the last run was class-routed with no concrete receipt, an earlier run ran opus."""
    _routing(monkeypatch)
    with kbc.connect() as conn:
        tid = _handoff_card(conn, ASTRA, [OPUS])
        _run(conn, tid, model='class:deep')
        conn.execute("UPDATE tasks SET status='review' WHERE id=?", (tid,))
        conn.commit()
        assert _review_route(conn, tid)[:2] == (GROK, 'xai-oauth')


def test_every_route_an_implementer_ran_on_is_unavailable_raises(board, clock, monkeypatch):
    _routing(monkeypatch, grok=False)
    with kbc.connect() as conn:
        tid = _handoff_card(conn, ASTRA, [OPUS, ASTRA])
        with pytest.raises(review.ReviewModelUnavailable):
            _review_route(conn, tid)
        with pytest.raises(review.ReviewModelUnavailable):
            tier.apply_model_tier(conn, kb.get_task(conn, tid), lane='review')
        assert pin_tests._events(conn, tid, 'model_tier') == []


def test_a_walled_independent_route_is_skipped(board, clock, monkeypatch):
    _routing(monkeypatch)
    monkeypatch.setattr(walls, 'active_walls', lambda **_k: {'xai-oauth': float(clock[0] + 99999)})
    with kbc.connect() as conn:
        tid = _handoff_card(conn, ASTRA, [ASTRA])
        # strong (opus) is not walled and independent of astra
        assert _review_route(conn, tid)[:2] == (OPUS, 'anthropic')
        # opus + astra implemented and grok is walled: nobody independent is left
        both = _handoff_card(conn, ASTRA, [OPUS, ASTRA])
        with pytest.raises(review.ReviewModelUnavailable):
            _review_route(conn, both)


def test_a_held_independent_route_is_skipped_by_the_base_resolver(board, clock, monkeypatch):
    """opus + astra implemented; grok is the only independent reviewer and it is held. The BASE resolver skips held
    providers (a kept held route was the old, wrong expectation): there is no independent reviewer, so the review
    is refused, never kept on a held pool and never moved onto an implementer's pool. A paced move from a held
    base route lands nowhere either."""
    _routing(monkeypatch)
    paced_tests._pools(clock, anthropic=('paced', None), openai_codex=('paced', None), xai_oauth=('held', HELD_FAR))
    with kbc.connect() as conn:
        tid = _handoff_card(conn, ASTRA, [OPUS, ASTRA])
        with pytest.raises(review.ReviewModelUnavailable):
            _review_route(conn, tid)
        moved = paced.paced_route(conn, kb.get_task(conn, tid), (GROK, 'xai-oauth', 'review_unpinned'),
                                  lane='review', cfg=tier.load_tier_config())
        assert moved is None


def test_unknown_implementer_keeps_the_existing_behaviour(board, clock, monkeypatch):
    """A class pin with no concrete receipt anywhere: the review is refused, exactly as before."""
    _routing(monkeypatch)
    with kbc.connect() as conn:
        tid = _card(conn)
        conn.execute("UPDATE tasks SET model_override='class:deep', provider_override='auto', status='review' "
                     "WHERE id=?", (tid,))
        conn.commit()
        assert _models(conn, tid) == set()
        with pytest.raises(review.ReviewModelUnavailable):
            _review_route(conn, tid)


def test_unpinned_review_without_any_receipt_is_still_a_legacy_review(board, clock, monkeypatch):
    _routing(monkeypatch)
    with kbc.connect() as conn:
        tid = _card(conn, status='review')
        assert _models(conn, tid) == set()
        assert _review_route(conn, tid)[2] != 'review_unpinned'


# --- paced reroute / governor / OfferLost -------------------------------------------------------------------------


def test_paced_route_never_lands_on_an_earlier_implementer(board, clock, monkeypatch):
    """astra is held (far); opus and astra both implemented, grok free: the move is to grok. With grok held too
    nothing moves (opus is free but implemented the card)."""
    _routing(monkeypatch)
    paced_tests._pools(clock, openai_codex=('held', HELD_FAR))
    with kbc.connect() as conn:
        tid = _handoff_card(conn, ASTRA, [OPUS, ASTRA])
        task = kb.get_task(conn, tid)
        base = (ASTRA, 'openai-codex', 'review_unpinned')
        cfg = tier.load_tier_config()
        moved = paced.paced_route(conn, task, base, lane='review', cfg=cfg)
        assert moved is not None and moved[:2] == (GROK, 'xai-oauth')
        paced_tests._pools(clock, openai_codex=('held', HELD_FAR), xai_oauth=('held', HELD_FAR))
        assert paced.paced_route(conn, task, base, lane='review', cfg=cfg) is None


def test_paced_route_for_a_review_override_also_excludes_the_full_set(board, clock, monkeypatch):
    _routing(monkeypatch)
    paced_tests._pools(clock, openai_codex=('held', HELD_FAR), xai_oauth=('held', HELD_FAR))
    with kbc.connect() as conn:
        tid = _handoff_card(conn, ASTRA, [OPUS, ASTRA])
        moved = paced.paced_route(conn, kb.get_task(conn, tid), (ASTRA, 'openai-codex', 'review_override'),
                                  lane='review', cfg=tier.load_tier_config())
        assert moved is None  # anthropic is free but opus implemented the card


@pytest.mark.parametrize('implementers, expect', [([ASTRA], (OPUS, 'anthropic')), ([OPUS, ASTRA], (GROK, 'xai-oauth'))])
def test_governor_pace_alternate_skips_implementer_models(board, clock, monkeypatch, implementers, expect):
    _routing(monkeypatch)
    paced_tests._pools(clock, openai_codex=('held', HELD_FAR))
    from hermes_cli import kanban_llm_pacing as lp
    cfg = lp.config_from({'providers': ['anthropic', 'openai-codex', 'xai-oauth'], 'enabled': True})
    verdicts = [{'provider': 'openai-codex', 'until': clock[0] + HELD_FAR}]
    candidates = [(OPUS, 'anthropic'), (ASTRA, 'openai-codex'), (GROK, 'xai-oauth')]
    seen_walls = []

    def probe(conn, task, walls_, base, now):
        seen_walls.append(set(walls_))
        for model, provider in candidates:
            if provider not in walls_:
                return model, provider
        return None

    monkeypatch.setattr('hermes_cli.kanban_quota_episode.alternate_route', probe)
    with kbc.connect() as conn:
        tid = _handoff_card(conn, ASTRA, implementers)
        got = lpd._pace_alternate_route(conn, kb.get_task(conn, tid), (ASTRA, 'openai-codex', 'review_unpinned'),
                                        'review', cfg, conn, verdicts)
    assert got is not None and got[:2] == expect, (got, seen_walls)


def test_governor_pace_alternate_refuses_when_only_implementers_have_room(board, clock, monkeypatch):
    _routing(monkeypatch, grok=False)
    paced_tests._pools(clock, openai_codex=('held', HELD_FAR))
    from hermes_cli import kanban_llm_pacing as lp
    cfg = lp.config_from({'providers': ['anthropic', 'openai-codex'], 'enabled': True})
    verdicts = [{'provider': 'openai-codex', 'until': clock[0] + HELD_FAR}]
    monkeypatch.setattr('hermes_cli.kanban_quota_episode.alternate_route',
                        lambda conn, task, walls_, base, now: None if 'anthropic' in walls_ else (OPUS, 'anthropic'))
    with kbc.connect() as conn:
        tid = _handoff_card(conn, ASTRA, [OPUS, ASTRA])
        assert lpd._pace_alternate_route(conn, kb.get_task(conn, tid), (ASTRA, 'openai-codex', 'review_unpinned'),
                                         'review', cfg, conn, verdicts) is None


def test_apply_model_tier_raises_offer_lost_when_the_take_lands_on_an_implementer(board, clock, monkeypatch):
    """An offer judged on the review's route but moving it onto an implementer is unsafe: the launch is refused
    (OfferLost, the card requeues) and nothing is recorded."""
    _routing(monkeypatch)
    with kbc.connect() as conn:
        tid = _handoff_card(conn, ASTRA, [OPUS, ASTRA])
        task = kb.get_task(conn, tid)
        judged = _review_route(conn, tid)  # grok
        pace_offer.offer(tid, judged, (OPUS, 'anthropic', 'openai-codex'))  # the move targets implementer opus
        try:
            with pytest.raises(pace_offer.OfferLost):
                tier.apply_model_tier(conn, task, lane='review')
        finally:
            pace_offer.clear()
        assert pin_tests._events(conn, tid, 'model_tier') == []
        assert (task.model_override, task.provider_override) == (ASTRA, 'openai-codex')


def test_apply_model_tier_still_takes_a_safe_offer(board, clock, monkeypatch):
    _routing(monkeypatch)
    with kbc.connect() as conn:
        tid = _handoff_card(conn, ASTRA, [ASTRA])
        task = kb.get_task(conn, tid)
        judged = _review_route(conn, tid)  # opus
        pace_offer.offer(tid, judged, (GROK, 'xai-oauth', 'anthropic'))
        try:
            reason = tier.apply_model_tier(conn, task, lane='review')
        finally:
            pace_offer.clear()
        assert reason.endswith('+paced:anthropic')
        assert (task.model_override, task.provider_override) == (GROK, 'xai-oauth')


def test_offer_lost_is_requeued_not_spawned_end_to_end(board, clock, monkeypatch, all_assignees_spawnable):
    """Through a real dispatcher tick: an unsafe offer spawns nothing and does not spend a retry."""
    _routing(monkeypatch)
    from hermes_cli import kanban_db_dispatch as dispatch
    seen = []
    with kbc.connect() as conn:
        tid = _handoff_card(conn, ASTRA, [OPUS, ASTRA])
        real_take = pace_offer.take

        def unsafe_take(task_id, resolved):
            real_take(task_id, resolved)
            return (OPUS, 'anthropic', 'openai-codex')

        monkeypatch.setattr('hermes_cli.kanban_pace_reroute.take', unsafe_take)
        pace_offer.offer(tid, _review_route(conn, tid), (GROK, 'xai-oauth', 'openai-codex'))
        dispatch.dispatch_once(conn, dry_run=False, spawn_fn=lambda *a: seen.append(a))
        stored = kb.get_task(conn, tid)
        pace_offer.clear()
        assert seen == []
        assert stored.status == 'review' and stored.consecutive_failures == 0 and stored.claim_lock is None


@pytest.mark.parametrize('held', [False, True])
def test_unpinned_astra_implementation_gets_concrete_independent_reviewer(board, clock, monkeypatch, held):
    _routing(monkeypatch)
    paced_tests._pools(clock, anthropic=('held' if held else 'paced', HELD_FAR if held else None))
    with kbc.connect() as conn:
        tid = _handoff_card(conn, None, [ASTRA])
        route = _review_route(conn, tid)
        assert route[:2] == ((GROK, 'xai-oauth') if held else (OPUS, 'anthropic'))
        assert not route[0].startswith('class:')


@pytest.mark.parametrize('title', ['Independent review', '**Independent code REVIEW**', 'Independent\nreview'])
def test_downstream_reviewer_pin_is_not_an_implementer(board, clock, monkeypatch, title):
    _routing(monkeypatch)
    with kbc.connect() as conn:
        impl = _card(conn, ASTRA)
        down = _card(conn, OPUS, title=title, creator=impl)
        task = kb.get_task(conn, down)
        if '\n' in title:
            assert _is_review(conn, task, 'ready') is False
            assert _models(conn, down) == {OPUS}
        else:
            conn.execute('UPDATE tasks SET body=?, model_pin_mandatory=1 WHERE id=?', (walls.REQUIRE_MODEL_MARKER, down))
            conn.commit()
            assert _models(conn, down) == {ASTRA}
            assert _is_review(conn, task, 'ready') is True
            assert _review_route(conn, down, 'ready')[:2] == (OPUS, 'anthropic')
            tier.apply_model_tier(conn, task, lane='ready')
            event = pin_tests._events(conn, down, 'model_tier')[-1]
            assert event['implementer_models'] == [ASTRA]
            assert event['implementer_model'] is None


def test_receipt_names_concrete_handoff_and_sorted_full_set(board, clock, monkeypatch):
    _routing(monkeypatch)
    with kbc.connect() as conn:
        tid = _handoff_card(conn, OPUS, [OPUS])
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        conn.commit()
        claim = kb.claim_task(conn, tid, claimer=kb._claimer_id())
        assert claim is not None
        kb._append_event(conn, tid, 'worker_result', {'route_model': ASTRA}, run_id=claim.current_run_id)
        assert kb.request_review(conn, tid)
        tier.apply_model_tier(conn, kb.get_task(conn, tid), lane='review')
        event = pin_tests._events(conn, tid, 'model_tier')[-1]
        assert event['implementer_model'] == ASTRA
        assert event['implementer_models'] == sorted([OPUS, ASTRA])


def test_review_lane_model_tier_without_run_route_is_not_an_author(board):
    with kbc.connect() as conn:
        tid = _card(conn)
        run = _run(conn, tid, model=GROK)
        kb._append_event(conn, tid, 'model_tier', {'lane': 'review'}, run_id=run)
        assert _models(conn, tid) == set()


# --- unpinned astra implementation with anthropic held: the third route, concrete --------------------------------


@pytest.mark.parametrize('reset', [600, HELD_FAR, None], ids=['near_reset', 'far_reset', 'unknown_reset'])
def test_unpinned_astra_with_anthropic_held_reviews_on_the_third_concrete_route(
        board, clock, monkeypatch, all_assignees_spawnable, reset):
    """Unpinned card implemented by astra (openai-codex); the strong tier (opus, anthropic) is pacing-held. The review
    must run on the third route, grok (xai-oauth): concrete (never ``class:deep``), neither the implementer
    nor the held pool, and a real dispatcher tick must spawn it there with the full receipt."""
    _routing(monkeypatch)
    paced_tests._pools(clock, anthropic=('held', reset))
    with kbc.connect() as conn:
        tid = _card(conn)
        claim = kb.claim_task(conn, tid, claimer=kb._claimer_id())
        assert claim is not None
        kb._append_event(conn, tid, 'worker_result', {'exit_code': 0, 'route_model': ASTRA}, run_id=claim.current_run_id)
        assert kb.request_review(conn, tid)  # a real handoff: the receipt's implementer_model reads it
        task = kb.get_task(conn, tid)
        assert task is not None and task.model_override is None
        assert _review_route(conn, tid) == (GROK, 'xai-oauth', 'review_unpinned')
        spawned = pin_tests._tick(conn)
        assert spawned == [(GROK, 'xai-oauth')]
        receipt = pin_tests._events(conn, tid, 'model_tier')[-1]
        assert (receipt['model'], receipt['provider'], receipt['reason']) == (GROK, 'xai-oauth', 'review_unpinned')
        assert receipt['implementer_model'] == ASTRA and receipt['implementer_models'] == [ASTRA]
        assert not receipt['model'].startswith('class:') and 'paced' not in receipt


def test_unpinned_astra_with_anthropic_and_xai_held_has_no_independent_reviewer(board, clock, monkeypatch):
    _routing(monkeypatch)
    paced_tests._pools(clock, anthropic=('held', HELD_FAR), xai_oauth=('held', HELD_FAR))
    with kbc.connect() as conn:
        tid = _handoff_card(conn, None, [ASTRA])
        with pytest.raises(review.ReviewModelUnavailable):  # astra authored, the other two pools are held
            _review_route(conn, tid)


# --- the explicit review_model override honors held/walled pools (the fast return used to skip the check) --------


def _override_card(conn, pin, override):
    tid = _card(conn, pin)
    assert kb.claim_task(conn, tid, claimer=kb._claimer_id()) is not None
    assert kb.request_review(conn, tid, metadata={'review_model': override, 'review_provider': PROVIDER[override]})
    return tid


@pytest.mark.parametrize('block', ['held', 'walled', 'live_wall'])
def test_independent_review_override_on_a_held_or_walled_pool_moves_to_the_next_independent_route(
        board, clock, monkeypatch, block):
    _routing(monkeypatch)
    if block == 'held':
        paced_tests._pools(clock, xai_oauth=('held', 600))
    elif block == 'walled':
        monkeypatch.setattr(walls, 'active_walls', lambda **_k: {'xai-oauth': float(clock[0] + 99999)})
    else:
        monkeypatch.setattr(walls, 'live_quota_wall', lambda p, m: float(clock[0] + 99999) if p == 'xai-oauth' else None)
    with kbc.connect() as conn:
        tid = _override_card(conn, ASTRA, GROK)  # astra authored, the handoff asks for grok on the blocked pool
        assert _review_route(conn, tid) == (OPUS, 'anthropic', 'review_override')


def test_independent_review_override_on_a_free_pool_is_honored(board, clock, monkeypatch):
    _routing(monkeypatch)
    paced_tests._pools(clock, openai_codex=('held', HELD_FAR))
    with kbc.connect() as conn:
        tid = _override_card(conn, ASTRA, GROK)
        assert _review_route(conn, tid) == (GROK, 'xai-oauth', 'review_override')


def test_unknown_implementer_keeps_an_override_on_a_held_pool_as_before(board, clock, monkeypatch):
    """No author evidence: today's route is kept (the exclusion has nothing to act on), held pool included."""
    _routing(monkeypatch)
    paced_tests._pools(clock, xai_oauth=('held', HELD_FAR))
    with kbc.connect() as conn:
        tid = _override_card(conn, None, GROK)
        assert _models(conn, tid) == set()
        assert _review_route(conn, tid) == (GROK, 'xai-oauth', 'review_override')


# --- mandatory reviewer pins never silently move to another model ---------------------------------------------------


def _mandatory_downstream(conn, *, pin=OPUS, ran=ASTRA):
    impl = _card(conn, ran)
    _run(conn, impl, model=ran)
    down = _card(conn, pin, title='Independent code review of the thing', creator=impl)
    conn.execute('UPDATE tasks SET model_pin_mandatory=1 WHERE id=?', (down,))
    conn.commit()
    return down


@pytest.mark.parametrize('block', ['held', 'walled', 'live_wall'])
def test_mandatory_downstream_reviewer_pin_is_never_moved_off_its_pool(board, clock, monkeypatch, block):
    _routing(monkeypatch)
    if block == 'held':
        paced_tests._pools(clock, anthropic=('held', HELD_FAR))
    elif block == 'walled':
        monkeypatch.setattr(walls, 'active_walls', lambda **_k: {'anthropic': float(clock[0] + 99999)})
    else:
        monkeypatch.setattr(walls, 'live_quota_wall', lambda p, m: float(clock[0] + 99999) if p == 'anthropic' else None)
    with kbc.connect() as conn:
        down = _mandatory_downstream(conn)
        task = kb.get_task(conn, down)
        assert _is_review(conn, task, 'ready') is True
        assert _review_route(conn, down, 'ready') == (OPUS, 'anthropic', 'review_unpinned')
        assert tier.resolve_dispatch_tier(conn, task, lane='ready') == (OPUS, 'anthropic', 'review_unpinned')
        # the pace governor's probe is not even consulted for it
        monkeypatch.setattr('hermes_cli.kanban_quota_episode.alternate_route', lambda *a: (GROK, 'xai-oauth'))
        from hermes_cli import kanban_llm_pacing as lp
        cfg = lp.config_from({'providers': ['anthropic', 'openai-codex', 'xai-oauth'], 'enabled': True})
        assert lpd._pace_alternate_route(conn, task, (OPUS, 'anthropic', 'review_unpinned'), 'ready', cfg, conn,
                                         [{'provider': 'anthropic', 'until': clock[0] + HELD_FAR}]) is None
        assert paced.paced_route(conn, task, (OPUS, 'anthropic', 'review_unpinned'), lane='ready',
                                 cfg=tier.load_tier_config()) is None


def test_mandatory_downstream_reviewer_pin_that_authored_the_work_is_refused(board, clock, monkeypatch):
    """The mandatory pin names the implementer too (e.g. via a child card): it cannot be swapped silently, so the
    review is unavailable instead of running on some other model."""
    _routing(monkeypatch)
    with kbc.connect() as conn:
        down = _mandatory_downstream(conn, pin=OPUS, ran=OPUS)
        with pytest.raises(review.ReviewModelUnavailable):
            _review_route(conn, down, 'ready')


def test_non_mandatory_downstream_reviewer_pin_on_a_held_pool_yields_to_the_next_route(board, clock, monkeypatch):
    _routing(monkeypatch)
    paced_tests._pools(clock, anthropic=('held', HELD_FAR))
    with kbc.connect() as conn:
        impl = _card(conn, ASTRA)
        _run(conn, impl, model=ASTRA)
        down = _card(conn, OPUS, title='Independent code review of the thing', creator=impl)
        assert _review_route(conn, down, 'ready') == (GROK, 'xai-oauth', 'review_unpinned')

