"""Real preclaim pace selection with frozen refusal history; no provider traffic."""
import json
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_llm_pacing_dispatch as pd
from hermes_cli import kanban_model_tier as tier
from hermes_cli import kanban_pace_reroute as pace
from hermes_cli import kanban_refusal_fallback as refusal
from hermes_cli import kanban_review_model as review
from tests.hermes_cli import test_kanban_refusal_boundary as fixtures

board = fixtures.board
pools = fixtures.pools


def refuse(conn, tid, provider):
    with kb.write_txn(conn):
        conn.execute("INSERT INTO task_runs(task_id,status,started_at,ended_at,outcome,metadata) "
                     "VALUES (?, 'safety_refused', 1, 2, 'safety_refused', ?)",
                     (tid, json.dumps({'refused_provider': provider})))


@pytest.mark.parametrize('third', [True, False])
@pytest.mark.parametrize('third_unserved', [False, True])
def test_real_dispatch_pace_excludes_refused_and_preserves_hold(board, pools, monkeypatch, third, third_unserved):
    conn, context = board
    base_cfg = tier.load_tier_config()
    monkeypatch.setattr(tier, 'load_tier_config',
                        lambda: {**base_cfg, 'llm_pacing': {'enabled': True, 'providers': ['openai-codex']}})
    std = pools['model_routing']['routes']['standard']
    if third:
        std.append(dict(std[1], provider='xai-oauth', model='grok-4.6'))
    if third_unserved:
        fixtures.fixtures.UNSERVED.add('xai-oauth')
    # Only the usage refresh is replaced: real stored reserve, governor and dispatch.
    monkeypatch.setattr(pd, 'pace_for', lambda *a, **k: None)
    now = time.time()
    path = pd.state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'v2': {'pools': {'openai-codex': {
        'at_reserve': True, 'read_at': now, 'until': now + 7200, 'accounts': [{'id': 'a'}]}}}}))
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='done', current_run_id=NULL, claim_lock=NULL WHERE id=?",
                     (context['task_id'],))
    assignee = kb.get_task(conn, context['task_id']).assignee
    tid = kb.create_task(conn, title='fixture pace exclusion', assignee=assignee)
    refuse(conn, tid, 'anthropic')
    original_runs = conn.execute('SELECT COUNT(*) FROM task_runs WHERE task_id=?', (tid,)).fetchone()[0]
    spawned = []
    pace.clear()
    try:
        for _ in range(3):
            result = dispatch.dispatch_once(conn, failure_limit=3, spawn_fn=lambda task, ws: spawned.append(
                (task.id, task.model_override, task.provider_override)))
            task = kb.get_task(conn, tid)
            ours = [s for s in spawned if s[0] == tid]
            assert task.consecutive_failures == 0
            assert all(s[2] != 'anthropic' for s in ours)
            assert not conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind IN "
                                    "('spawn_failed','gave_up')", (tid,)).fetchall()
            if third and not third_unserved:
                assert ours == [(tid, 'grok-4.6', 'xai-oauth')]
                assert task.status == 'running'
                assert not [p for p in result.llm_paced if p[0] == tid]
                break
            assert ours == []
            assert task.status == 'ready' and task.current_run_id is None and task.claim_lock is None
            assert [p for p in result.llm_paced if p[0] == tid]
            assert conn.execute('SELECT COUNT(*) FROM task_runs WHERE task_id=?', (tid,)).fetchone()[0] == original_runs
        assert refusal.refused_providers(conn, tid) == ['anthropic']
    finally:
        pace.clear()


def test_review_routing_disabled_refusal_is_structural_not_transient(board, pools):
    conn, context = board
    tid = context['task_id']
    refuse(conn, tid, 'anthropic')
    pools['model_routing']['enabled'] = False
    kb.request_review(conn, tid)
    task = kb.claim_review_task(conn, tid, claimer=kb._claimer_id())
    with pytest.raises(review.ReviewModelsExhausted) as exc:
        tier.apply_model_tier(conn, task, lane='review')
    infrastructure, exhausted, extra = review.admission_refusal(conn, tid, exc.value)
    assert not infrastructure and exhausted
    assert extra == {'admission_refusal': 'review_models_exhausted', 'infrastructure': True}


def test_review_pace_peer_excludes_refused_with_otherwise_qualified_routes(board, pools):
    from hermes_cli.kanban_paced_reroute import _review_peer
    conn, context = board
    tid = context['task_id']
    refuse(conn, tid, 'anthropic')
    task = kb.get_task(conn, tid)
    now = time.time()
    moved = _review_peer(conn, task, tier.load_tier_config(), {'openai-codex': now + 7200}, now)
    assert moved == ('grok-4.7', 'xai-oauth')
    assert _review_peer(conn, task, tier.load_tier_config(),
                        {'openai-codex': now + 7200, 'xai-oauth': now + 7200}, now) is None
