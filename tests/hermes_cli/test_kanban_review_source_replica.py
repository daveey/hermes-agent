"""Review continuation carries author evidence without importing runnable foreign cards."""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_continuation as continuation
from hermes_cli import kanban_model_tier as tier
from hermes_cli import kanban_review_model as review
from hermes_cli import kanban_worker_node as wn
from tests.hermes_cli import test_kanban_mandatory_pin as pool

svc = pool.svc
gws = pool.gws
AUTHOR = 'claude-sonnet-5-5'


def _carried(gws):
    hub, worker = gws
    hub.enter()
    source = kb.create_task(hub.conn, title='Implement cache', assignee='default')
    hub.round()
    claim = kb.claim_task(hub.conn, source, claimer=kb._claimer_id())
    assert claim
    kb._append_event(hub.conn, source, 'worker_result', {'route_model': AUTHOR}, run_id=claim.current_run_id)
    assert kb.complete_task(hub.conn, source, result='implemented', expected_run_id=claim.current_run_id)
    down = kb.create_task(hub.conn, title='Independent review: cache', assignee='default',
                          creator_task_id=source, model_override=pool.PIN['model'],
                          provider_override='anthropic')
    hub.conn.execute('UPDATE tasks SET model_pin_mandatory=1 WHERE id=?', (down,))
    hub.conn.commit()
    hub.round()
    claim = kb.claim_task(hub.conn, down, claimer=kb._claimer_id())
    assert claim
    assert continuation.continue_task(hub.conn, down, checkpoint='14 tests passed; CI pending',
                                      expected_run_id=claim.current_run_id)['ok']
    landing = kb.create_task(hub.conn, title='Land cache', assignee='default', parents=[down])
    hub.round()
    worker.round()
    worker.enter()
    replica = wn.open_replica(worker.conn)
    assert replica is not None
    try:
        assert wn.import_cards(worker.conn, replica, [down]) == [down]
    finally:
        replica.close()
    assert kb.get_task(worker.conn, down)
    assert kb.get_task(worker.conn, source) is None
    return source, down, landing


def test_continued_review_resolves_replica_author_at_real_dispatch(gws, monkeypatch):
    pool._attest(monkeypatch)
    source, down, landing = _carried(gws)
    _, worker = gws
    before = worker.conn.total_changes
    task = kb.get_task(worker.conn, down)
    assert review.implementer_models(worker.conn, task) == {AUTHOR}
    assert tier.resolve_dispatch_tier(worker.conn, task, lane='ready') == (
        pool.PIN['model'], 'anthropic', 'review_unpinned')
    assert worker.conn.total_changes == before
    assert kb.get_task(worker.conn, source) is None
    worker.tick()
    receipt = pool._route(worker.conn, down)[-1]
    assert receipt['model'] == pool.PIN['model'] and receipt['model'] != AUTHOR
    event = worker.conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='model_tier' "
                                "ORDER BY id DESC LIMIT 1", (down,)).fetchone()
    assert json.loads(event[0])['implementer_models'] == [AUTHOR]
    with wn.open_replica(worker.conn) as replica:
        assert replica.execute('SELECT 1 FROM task_links WHERE parent_id=? AND child_id=?',
                               (down, landing)).fetchone()
    checkpoints = worker.conn.execute("SELECT metadata FROM task_runs WHERE task_id=? AND outcome='continued'",
                                      (down,)).fetchall()
    assert any(json.loads(row[0])['checkpoint'] == '14 tests passed; CI pending' for row in checkpoints)


@pytest.mark.parametrize('fault', ['missing', 'unreadable'])
def test_absent_replica_remains_fail_closed(gws, monkeypatch, fault):
    pool._attest(monkeypatch)
    _, down, _ = _carried(gws)
    _, worker = gws
    def unavailable(conn):
        if fault == 'unreadable':
            raise OSError('replica unavailable')
    monkeypatch.setattr(wn, 'open_replica', unavailable)
    with pytest.raises(review.ReviewModelUnavailable):
        review.implementer_models(worker.conn, kb.get_task(worker.conn, down))
    worker.tick()
    assert pool._route(worker.conn, down) == []


def test_replica_author_pin_is_refused_without_rewriting_pin(gws, monkeypatch):
    pool._attest(monkeypatch)
    _, down, _ = _carried(gws)
    _, worker = gws
    worker.conn.execute('UPDATE tasks SET model_override=? WHERE id=?', (AUTHOR, down))
    worker.conn.commit()
    worker.tick()
    assert pool._route(worker.conn, down) == []
    assert kb.get_task(worker.conn, down).model_override == AUTHOR


def test_local_source_wins_and_no_replica_is_opened(gws, monkeypatch):
    pool._attest(monkeypatch)
    source, down, _ = _carried(gws)
    _, worker = gws
    replica = wn.open_replica(worker.conn)
    assert replica is not None
    try:
        assert wn.import_cards(worker.conn, replica, [source]) == [source]
    finally:
        replica.close()
    worker.conn.execute('UPDATE tasks SET model_override=? WHERE id=?', ('gpt-6-astra', source))
    worker.conn.commit()
    def unexpected(conn):
        pytest.fail('local source must not open replica')
    monkeypatch.setattr(wn, 'open_replica', unexpected)
    assert review.implementer_models(worker.conn, kb.get_task(worker.conn, down)) == {AUTHOR, 'gpt-6-astra'}


def test_missing_source_in_both_stores_is_refused(gws, monkeypatch):
    pool._attest(monkeypatch)
    _, down, _ = _carried(gws)
    _, worker = gws
    worker.conn.execute("UPDATE task_events SET payload=? WHERE task_id=? AND kind='created'",
                        (json.dumps({'creator_task_id': 't_missing'}), down))
    worker.conn.commit()
    with pytest.raises(review.ReviewModelUnavailable, match='t_missing'):
        review.implementer_models(worker.conn, kb.get_task(worker.conn, down))


def test_unsynced_board_never_reads_a_replica(gws, monkeypatch):
    from hermes_cli import kanban_db_sync as sync
    pool._attest(monkeypatch)
    _, down, _ = _carried(gws)
    _, worker = gws
    sync.set_mode(worker.conn, 'off')
    def unexpected(conn):
        pytest.fail('off board must not read replica')
    monkeypatch.setattr(wn, 'open_replica', unexpected)
    with pytest.raises(review.ReviewModelUnavailable):
        review.implementer_models(worker.conn, kb.get_task(worker.conn, down))
