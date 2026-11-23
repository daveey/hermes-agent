"""A provider safety refusal retries the same card, unchanged, on another provider of its class. [daveey]

Real failed interpreter -> dispatcher stop (as ``test_kanban_refusal_stop``), no model/provider call.
Standard class: anthropic + openai-codex. Pinned/deep class: anthropic + openai-codex + xai-oauth.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_model_tier as tier
from hermes_cli import kanban_provider_walls as walls
from hermes_cli import kanban_refusal_fallback as fb
from hermes_cli import kanban_spend_decision as decision
from hermes_cli.kanban_refusal_receipt import restricted
from tests.hermes_cli import test_kanban_failed_result_reconcile as fixtures
from tests.hermes_cli import test_kanban_sync_run_rows as transport
from tests.hermes_cli.test_kanban_quota_episode import routing

board = fixtures.board
kanban_home = transport.kanban_home
svc = transport.svc
live = transport.live
UNSERVED: set = set()
CHILD = fixtures._CHILD  # pristine: each refusal rebuilds its child from it


@pytest.fixture
def pools(monkeypatch):
    config, _cfg = routing(monkeypatch)
    deep = config['model_routing']['routes']['deep'][0]
    config['model_routing']['routes']['deep'] = [
        dict(deep, provider='anthropic', model='claude-opus-5-5'),
        dict(deep, provider='openai-codex', model='gpt-6-astra'),
        dict(deep, provider='xai-oauth', model='grok-4.7')]
    config['providers'] = {}
    UNSERVED.clear()
    monkeypatch.setattr(walls, 'active_walls', lambda **_k: {})
    monkeypatch.setattr(walls, 'live_quota_wall', lambda *_a, **_k: None)
    monkeypatch.setattr(walls, 'live_pool_serving', lambda provider, model=None: provider not in UNSERVED)
    return config


def refuse(conn, context, monkeypatch, provider):
    """The claimed run ends in a verified provider safety refusal from ``provider`` (worker_result route)."""
    source = CHILD.replace(
        "_record_worker_result(context, 1, 'fixture worker_receipt_timeout')",
        "_record_worker_result(context, 1, 'fixture refusal', "
        "{'failed': True, 'failure_reason': 'content_policy_blocked', 'failure_retryable': False}, "
        f"{{'route_provider': {provider!r}}})")
    assert source != CHILD
    monkeypatch.setattr(fixtures, '_CHILD', source)
    fixtures._failed_child(conn, context)
    dispatch.detect_crashed_workers(conn)


def reclaim(conn, tid):
    task = kb.claim_task(conn, tid, claimer=kb._claimer_id())
    assert task is not None
    return task, {'db': str(kb.kanban_db_path(board='default')), 'task_id': tid, 'run_id': task.current_run_id,
                  'claim_lock': task.claim_lock}


def events(conn, tid, kind):
    return [json.loads(r[0]) for r in conn.execute(
        'SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id', (tid, kind))]


def test_one_refusal_returns_card_ready_and_next_claim_uses_another_provider(board, pools, monkeypatch):
    conn, context = board
    tid = context['task_id']
    before = kb.get_task(conn, tid)
    refuse(conn, context, monkeypatch, 'anthropic')
    task = kb.get_task(conn, tid)
    assert task.status == 'ready' and task.current_run_id is None and task.claim_lock is None
    assert task.consecutive_failures == 0
    assert not restricted(conn, tid) and not decision.pending(conn, tid)
    assert fb.refused_providers(conn, tid) == ['anthropic']
    (fallback,) = events(conn, tid, 'safety_refusal_fallback')
    assert (fallback['refused_provider'], fallback['next_provider']) == ('anthropic', 'openai-codex')
    assert fallback['refused_providers'] == ['anthropic']
    assert fallback['message'] == 'Provider anthropic refused; retrying unchanged on openai-codex'
    assert not events(conn, tid, 'spend_budget_triaged') and not events(conn, tid, 'decision_hold_restricted')
    # The next claim is routed off the refused provider, and the receipt says why.
    claimed, _ = reclaim(conn, tid)
    reason = tier.apply_model_tier(conn, claimed, lane='ready')
    assert reason.endswith('+refusal:anthropic')
    assert (claimed.model_override, claimed.provider_override) == ('gpt-test', 'openai-codex')
    route = [e for e in events(conn, tid, 'run_route') if e['tier_reason'].endswith('+refusal:anthropic')]
    assert route and route[-1]['provider'] == 'openai-codex'
    assert before.body == claimed.body and before.title == claimed.title


def test_refusal_record_and_events_preserved_after_fallback(board, pools, monkeypatch):
    conn, context = board
    tid, run_id = context['task_id'], context['run_id']
    refuse(conn, context, monkeypatch, 'anthropic')
    run = conn.execute('SELECT * FROM task_runs WHERE id=?', (run_id,)).fetchone()
    assert (run['outcome'], run['status']) == ('safety_refused', 'safety_refused') and run['ended_at'] is not None
    meta = json.loads(run['metadata'])
    assert meta['refused_provider'] == 'anthropic' and meta['failure_reason'] == 'content_policy_blocked'
    assert meta.get('cause') != 'safety_refusal'
    (refused,) = events(conn, tid, 'safety_refused')
    assert refused['refused_provider'] == 'anthropic' and 'cause' not in refused
    fallback = conn.execute("SELECT run_id FROM task_events WHERE task_id=? AND kind='safety_refusal_fallback'",
                            (tid,)).fetchone()
    assert fallback['run_id'] == run_id  # the event belongs to the refused run


def test_retried_run_receives_identical_card_and_no_refusal_note(board, pools, monkeypatch):
    conn, context = board
    tid = context['task_id']
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET body=? WHERE id=?", ('Do the exact thing.\nSecond line \u00e9.', tid))
    first = kb.get_task(conn, tid)
    refuse(conn, context, monkeypatch, 'anthropic')
    comments = conn.execute("SELECT COUNT(*) FROM task_comments WHERE task_id=?", (tid,)).fetchone()[0]
    claimed, _ = reclaim(conn, tid)
    tier.apply_model_tier(conn, claimed, lane='ready')
    seen = [(claimed.title, claimed.body, list(claimed.skills or []), claimed.model_override, claimed.provider_override)]
    again = kb.get_task(conn, tid)
    assert seen[0][0].encode() == first.title.encode() and seen[0][1].encode() == first.body.encode()
    assert (again.title, again.body, again.skills) == (first.title, first.body, first.skills)
    assert conn.execute("SELECT COUNT(*) FROM task_comments WHERE task_id=?", (tid,)).fetchone()[0] == comments
    assert seen[0][4] != 'anthropic'


def test_every_eligible_provider_refused_triages_with_safety_restriction(board, pools, monkeypatch):
    conn, context = board
    tid = context['task_id']
    refuse(conn, context, monkeypatch, 'anthropic')
    claimed, second = reclaim(conn, tid)
    tier.apply_model_tier(conn, claimed, lane='ready')
    refuse(conn, second, monkeypatch, 'openai-codex')
    task = kb.get_task(conn, tid)
    assert task.status == 'triage' and restricted(conn, tid) and decision.pending(conn, tid)
    stop = decision.pending_episode(conn, tid)
    payload = json.loads(stop['payload'])
    assert payload['cause'] == 'safety_refusal'
    assert payload['refused_providers'] == ['anthropic', 'openai-codex'] and payload['refused_provider'] == 'openai-codex'
    assert len(events(conn, tid, 'safety_refusal_fallback')) == 1  # only the first refusal fell back
    outcomes = [r[0] for r in conn.execute('SELECT outcome FROM task_runs WHERE task_id=? ORDER BY id', (tid,))]
    assert outcomes == ['safety_refused', 'safety_refused']
    notice = events(conn, tid, decision.NOTICE)[-1]
    assert notice['cause'] == 'safety_refusal' and notice['refused_providers'] == ['anthropic', 'openai-codex']
    text = decision.notice(notice)
    assert 'anthropic, openai-codex' in text and 'retried unchanged' in text and 'Do not rerun the refused work' in text
    assert not any('retrying unchanged' in json.dumps(e) for e in events(conn, tid, decision.NOTICE))
    ok, reason = kb.promote_task(conn, tid, actor='fixture', reason='administrative promotion')
    assert not ok and 'safety' in reason.lower()


def test_no_unrefused_provider_left_at_claim_time_fails_closed(board, pools, monkeypatch):
    conn, context = board
    tid = context['task_id']
    refuse(conn, context, monkeypatch, 'anthropic')
    assert kb.get_task(conn, tid).status == 'ready'
    UNSERVED.add('openai-codex')  # the only peer lost its live pool before the next claim
    result = SimpleNamespace(skipped_ineligible=[])
    assert fb.admission(conn, tid, result, dry_run=False) is True
    assert result.skipped_ineligible == [(tid, 'refusal_exhausted')]
    assert kb.get_task(conn, tid).status == 'triage' and restricted(conn, tid)
    payload = json.loads(decision.pending_episode(conn, tid)['payload'])
    assert payload['cause'] == 'safety_refusal' and payload['refused_providers'] == ['anthropic']
    # A peer that is still serving keeps the card claimable.


def test_admission_leaves_card_with_an_unrefused_peer_alone(board, pools, monkeypatch):
    conn, context = board
    tid = context['task_id']
    refuse(conn, context, monkeypatch, 'anthropic')
    result = SimpleNamespace(skipped_ineligible=[])
    assert fb.admission(conn, tid, result, dry_run=False) is False
    assert kb.get_task(conn, tid).status == 'ready' and not result.skipped_ineligible


def test_pinned_card_falls_back_to_a_class_peer(board, pools, monkeypatch):
    conn, context = board
    tid = context['task_id']
    with kb.write_txn(conn):  # historical raw pin seed, as the model-neutral routing tests do
        conn.execute("UPDATE tasks SET model_override='claude-opus-5-5', provider_override='anthropic' WHERE id=?", (tid,))
        kb._append_event(conn, tid, 'run_route', {'model': 'claude-opus-5-5', 'provider': 'anthropic', 'lane': 'ready',
                                                  'source': 'pin', 'tier_reason': 'pinned'}, run_id=context['run_id'])
    # No worker route_provider: the provider comes from the run_route receipt.
    source = CHILD.replace(
        "_record_worker_result(context, 1, 'fixture worker_receipt_timeout')",
        "_record_worker_result(context, 1, 'fixture refusal', "
        "{'failed': True, 'failure_reason': 'content_policy_blocked', 'failure_retryable': False})")
    monkeypatch.setattr(fixtures, '_CHILD', source)
    fixtures._failed_child(conn, context)
    dispatch.detect_crashed_workers(conn)
    assert kb.get_task(conn, tid).status == 'ready' and not restricted(conn, tid)
    (fallback,) = events(conn, tid, 'safety_refusal_fallback')
    assert (fallback['refused_provider'], fallback['next_provider']) == ('anthropic', 'openai-codex')
    task = kb.get_task(conn, tid)
    model, provider, reason = tier.resolve_dispatch_tier(conn, task, lane='ready')
    assert (model, provider) == ('gpt-6-astra', 'openai-codex') and reason.endswith('+refusal:anthropic')


def test_refusing_provider_unproven_keeps_todays_stop(board, pools, monkeypatch):
    conn, context = board
    tid = context['task_id']
    source = CHILD.replace(
        "_record_worker_result(context, 1, 'fixture worker_receipt_timeout')",
        "_record_worker_result(context, 1, 'fixture refusal', "
        "{'failed': True, 'failure_reason': 'content_policy_blocked', 'failure_retryable': False})")
    monkeypatch.setattr(fixtures, '_CHILD', source)
    fixtures._failed_child(conn, context)
    dispatch.detect_crashed_workers(conn)
    assert kb.get_task(conn, tid).status == 'triage' and restricted(conn, tid)
    assert not events(conn, tid, 'safety_refusal_fallback')


def test_non_refusal_failure_is_untouched(board, pools, monkeypatch):
    conn, context = board
    tid = context['task_id']
    fixtures._failed_child(conn, context)  # plain exit-1 failure, no content_policy_blocked
    dispatch.detect_crashed_workers(conn)
    assert not events(conn, tid, 'safety_refusal_fallback') and not fb.refused_providers(conn, tid)
    assert kb.get_task(conn, tid).status != 'triage' or not restricted(conn, tid)


def test_primary_board_queues_release_naming_provider_and_fallback(live, kanban_home, svc, pools, monkeypatch):
    from hermes_cli.kanban_sync_wire import translate
    monkeypatch.setenv('HERMES_KANBAN_CRASH_GRACE_SECONDS', '0')
    tid, _uid = transport._claimed(live, kanban_home, svc, 'refused interpreter transport')
    task = kb.get_task(live, tid)
    context = {'db': str(kanban_home / 'kanban.db'), 'task_id': tid, 'run_id': task.current_run_id,
               'claim_lock': task.claim_lock, 'board': transport.BOARD}
    refuse(live, context, monkeypatch, 'anthropic')
    (release,) = live.execute("SELECT * FROM kanban_sync_outbox WHERE op='release'").fetchall()
    diagnostic = translate(dict(release), '0000000000000-0000-fixture')['diagnostic']
    assert diagnostic['refused_provider'] == 'anthropic' and diagnostic['refused_providers'] == ['anthropic']
    assert diagnostic['safety_fallback'] == {'next_model': 'gpt-test', 'next_provider': 'openai-codex'}
    assert kb.get_task(live, tid).status == 'running'  # the Board, not this replica, closes a primary run
