"""Review continuation carries author evidence without importing runnable foreign cards."""
import json
import sqlite3

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
    payload = json.loads(event[0])
    assert payload['review_independence'] == 'separate_execution'
    assert 'implementer_models' not in payload
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


def test_replica_author_pin_is_used_without_rewriting_pin(gws, monkeypatch):
    """The replica still carries the author, but a mandatory reviewer pin on the author's model is admitted as-is."""
    pool._attest(monkeypatch)
    _, down, _ = _carried(gws)
    _, worker = gws
    worker.conn.execute('UPDATE tasks SET model_override=? WHERE id=?', (AUTHOR, down))
    worker.conn.commit()
    assert review.implementer_models(worker.conn, kb.get_task(worker.conn, down)) == {AUTHOR}
    worker.tick()
    routes = pool._route(worker.conn, down)
    assert len(routes) == 1 and routes[-1]['model'] == AUTHOR and routes[-1]['source'] == 'pin'
    assert kb.get_task(worker.conn, down).model_override == AUTHOR


def test_local_source_wins_over_the_replica_copy(gws, monkeypatch):
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
    # The replica copy still has no override: only the local copy can name gpt-6-astra.
    replica = wn.open_replica(worker.conn)
    try:
        assert kb.get_task(replica, source).model_override in (None, '')
    finally:
        replica.close()
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


@pytest.mark.parametrize('fail_lookup', [False, True])
def test_replica_snapshot_reused_and_closed(gws, monkeypatch, fail_lookup):
    from hermes_cli.kanban_review_sources import source_reader
    source, down, _ = _carried(gws)
    _, worker = gws
    original = wn.open_replica
    opened = []

    def tracked(conn):
        replica = original(conn)
        opened.append(replica)
        return replica

    monkeypatch.setattr(wn, 'open_replica', tracked)
    with source_reader(worker.conn) as lookup:
        source_conn, task = lookup(source)
        assert task.id == source and source_conn is opened[0]
        assert source_conn.in_transaction
        again, task = lookup(source)
        assert again is source_conn and len(opened) == 1
        local_conn, task = lookup(down)
        assert local_conn is worker.conn and task.id == down
        if fail_lookup:
            with pytest.raises(review.ReviewModelUnavailable, match='t_missing'):
                lookup('t_missing')
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        source_conn.execute('SELECT 1')
    assert worker.conn.execute('SELECT 1').fetchone()[0] == 1


def test_sqlite_read_error_translates_and_closes(gws, monkeypatch):
    from hermes_cli.kanban_review_sources import source_reader
    source, _, _ = _carried(gws)
    _, worker = gws
    replica = wn.open_replica(worker.conn)
    assert replica is not None
    monkeypatch.setattr(wn, 'open_replica', lambda conn: replica)
    original = kb.get_task

    def broken(conn, task_id):
        if conn is replica:
            raise sqlite3.OperationalError('controlled read failure')
        return original(conn, task_id)

    monkeypatch.setattr(kb, 'get_task', broken)
    with pytest.raises(review.ReviewModelUnavailable, match='unreadable'):
        with source_reader(worker.conn) as lookup:
            lookup(source)
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        replica.execute('SELECT 1')


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


def _linked(gws, *, link_after_pickup=False):
    """A review card whose only implementer evidence is its link to the source (no creator, no declaration)."""
    hub, worker = gws
    hub.enter()
    source = kb.create_task(hub.conn, title='Implement cache', assignee='default')
    hub.round()
    claim = kb.claim_task(hub.conn, source, claimer=kb._claimer_id())
    assert claim
    kb._append_event(hub.conn, source, 'worker_result', {'route_model': AUTHOR}, run_id=claim.current_run_id)
    assert kb.complete_task(hub.conn, source, result='implemented', expected_run_id=claim.current_run_id)
    down = kb.create_task(hub.conn, title='Independent review: cache', assignee='default')
    if not link_after_pickup:
        kb.link_tasks(hub.conn, down, source)
    hub.conn.commit()
    hub.round()
    worker.round()
    worker.enter()
    replica = wn.open_replica(worker.conn)
    assert replica is not None
    try:
        assert wn.import_cards(worker.conn, replica, [down]) == [down]
    finally:
        replica.close()
    if link_after_pickup:
        hub.enter()
        kb.link_tasks(hub.conn, down, source)
        hub.conn.commit()
        hub.round()
        worker.round()
        worker.enter()
    assert kb.get_task(worker.conn, source) is None
    return source, down


@pytest.mark.parametrize('link_after_pickup', [False, True])
def test_picked_up_review_reads_its_source_links_from_the_replica(gws, monkeypatch, link_after_pickup):
    """Worker refusals 2026-11-19: pickup copied the review card but not its links to the source, so the worker
    found no implementer and refused the review. A link the Board adds after pickup never reaches the live file
    either (the feed keeps a link only when its child is local)."""
    pool._attest(monkeypatch)
    source, down = _linked(gws, link_after_pickup=link_after_pickup)
    _, worker = gws
    assert not worker.conn.execute('SELECT 1 FROM task_links WHERE parent_id=?', (down,)).fetchone()
    before = worker.conn.total_changes
    assert review.implementer_models(worker.conn, kb.get_task(worker.conn, down)) == {AUTHOR}
    assert worker.conn.total_changes == before
    assert kb.get_task(worker.conn, source) is None


def test_linked_review_without_a_replica_still_resolves_a_route(gws, monkeypatch):
    """Missing author evidence is attribution only: the collector reports no authors, the review still routes, and
    only a genuinely unavailable route (nothing configured) refuses."""
    pool._attest(monkeypatch)
    _, down = _linked(gws)
    _, worker = gws
    monkeypatch.setattr(wn, 'open_replica', lambda conn: None)
    task = kb.get_task(worker.conn, down)
    assert review.implementer_models(worker.conn, task) == set()
    model, _, reason = review.resolve_review_model(worker.conn, task, {'strong_model': pool.PIN['model']})
    assert model == pool.PIN['model'] and reason == 'review_unpinned'
    with pytest.raises(review.ReviewModelsExhausted, match='No eligible deep or standard review route'):
        review.resolve_review_model(worker.conn, task, {})
