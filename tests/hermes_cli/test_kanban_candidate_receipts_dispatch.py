"""Real dispatcher and pickup integration for bounded candidate receipts."""
from unittest.mock import Mock

import pytest

from hermes_cli import kanban_candidate_receipts as receipts
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_worker_node as node
from tests.hermes_cli import test_kanban_runs_on_dispatch as support

gws = support.gws
svc = support.svc


def test_real_tick_records_capacity_and_suppresses_claimed(gws):
    hub, _ = gws
    hub.enter()
    high = kb.create_task(hub.conn, title='first', assignee='default', priority=900)
    low = kb.create_task(hub.conn, title='later', assignee='default', priority=100)
    hub.round()
    result = hub.tick(max_in_progress=1)
    assert support._spawned(result) == [high]
    rows = receipts.read(hub.conn)['receipts']
    by_id = {r['task_id']: r for r in rows}
    assert high not in by_id
    assert by_id[low]['gate'] == 'capacity'
    assert by_id[low]['score_status'] == 'observed'
    assert by_id[low]['score'] is not None
    # Next pass has no slots; capacity observation still covers local ready cards.
    hub.tick(max_in_progress=1)
    assert receipts.read(hub.conn, task_id=low)['receipts'][0]['stage'] == 'global'


def test_score_tap_does_not_rerank(monkeypatch):
    from hermes_cli import kanban_admission_score as score
    from hermes_cli import kanban_dispatch_priority as priority
    rows = [dict(id='x')]
    rank = Mock(return_value=[(rows[0], Mock(total=123.0))])
    monkeypatch.setattr(score, 'rank', rank)
    with receipts.collect(worker='a1') as capture:
        assert priority.order_with_remote_waiters(None, rows) == rows
        receipts.note('x', 'prefilter', 'test', stage='claim')
    rank.assert_called_once_with(None, rows)
    assert capture.ranked == {'x': 123.0}


def test_pool_nonpinned_prefilter_and_capacity(monkeypatch):
    from hermes_cli import kanban_class_walls as walls
    # This receipt seam deliberately has no replica; real admission is covered with two gateways.
    monkeypatch.setattr(walls, 'pickup_refusal', lambda *a: None)
    from hermes_cli import kanban_db_dispatch as dispatch
    from hermes_cli import kanban_dispatch_priority as priority
    from hermes_cli import kanban_pickup_held as held
    cards = [dict(id='coach', status='ready', assignee='missing', runs_on=['pool:worker']),
             dict(id='pool-full', status='ready', assignee='default', runs_on=['pool:worker'])]
    monkeypatch.setattr(node, '_unimported_pinned', lambda *a: set())
    monkeypatch.setattr(node, 'registry', lambda: dict(machines={}))
    monkeypatch.setattr(node, 'candidates', lambda *a: cards)
    monkeypatch.setattr(dispatch, 'review_dispatch_enabled', lambda: True)
    monkeypatch.setattr(priority, 'order_with_remote_waiters', lambda conn, rows: rows)
    monkeypatch.setattr(node, '_skip_reason', lambda conn, card, *a, **kw:
                        ('profile_ineligible', 'missing profile') if card['id'] == 'coach' else None)
    for name in ('_quota_held', '_review_walled', '_paced'):
        monkeypatch.setattr(node, name, lambda *a: None)
    monkeypatch.setattr(held, 'board_hold', lambda *a: None)
    with receipts.collect(worker='a1') as capture:
        picked, skips = node._evaluate(None, None, 'a1', mode='pool', limit=1,
                                      profile_exists=None, full_detail='full')
    assert picked == ['pool-full'] and skips == {}
    assert capture.records['coach']['gate'] == 'prefilter'
    with receipts.collect(worker='a1') as capture:
        picked, skips = node._evaluate(None, None, 'a1', mode='pool', limit=0,
                                      profile_exists=None, full_detail='full')
    assert not picked
    assert {r['gate'] for r in capture.records.values()} == {'capacity'}


@pytest.mark.parametrize('bucket,entry,gate', [
    ('skipped_nonspawnable', 'x', 'prefilter'), ('capsule_refused', ('x', 'missing'), 'capability'),
    ('no_progress_held', ('x', 'spend'), 'budget/pacing'), ('pinned_quota_held', ('x', 'quota'), 'budget/pacing'),
    ('llm_paced', ('x', 'wall'), 'budget/pacing'), ('skipped_per_profile_capped', ('x', 'default', 1), 'capacity'),
    ('respawn_guarded', ('x', 'cooldown'), 'prefilter'), ('owner_down', ('x', 'default', 'down'), 'prefilter'),
    ('quiet_deferred', ('x', 'quiet'), 'capacity'), ('claim_deferred', ('x', 'labels_mismatch'), 'labels'),
    ('claim_deferred', ('x', 'containment'), 'containment'), ('claim_deferred', ('x', 'source_missing'), 'provenance'),
    ('claim_deferred', ('x', 'capacity'), 'capacity'), ('claim_deferred', ('x', 'capability_missing'), 'capability'),
])
def test_actual_lane_result_classification(bucket, entry, gate):
    from hermes_cli.kanban_db_dispatch import DispatchResult
    result = DispatchResult()
    getattr(result, bucket).append(entry)
    with receipts.collect(worker='a1') as capture:
        receipts.lane_refused('x', result, status='ready')
        receipts.lane_refused('review', result, status='review')
    assert capture.records['x']['gate'] == gate
    assert 'review' not in capture.records


def test_dry_run_and_write_failure_are_nonintrusive(monkeypatch):
    import sqlite3
    from hermes_cli import kanban_worker_node
    monkeypatch.setattr(kanban_worker_node, '_own', lambda _: 'a1')
    conn = sqlite3.connect(':memory:')
    with receipts.dispatch_pass(conn, dry_run=True):
        receipts.note('x', 'labels', 'no', stage='pickup')
    assert receipts.read(conn)['passes'] == []
    def fail(*args):
        raise sqlite3.OperationalError('read only')
    monkeypatch.setattr(receipts, 'persist', fail)
    with receipts.dispatch_pass(conn):
        receipts.note('x', 'labels', 'no', stage='pickup')
