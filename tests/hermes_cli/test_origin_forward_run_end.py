"""Voluntary parking must not revoke a queued forward's admission authority."""
import json
import sqlite3

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_continuation as continuation
from tests.hermes_cli.test_kanban_continuation import home as home  # noqa: PLC0414 -- pytest fixture reexport


@pytest.fixture
def pending(home, monkeypatch):
    from hermes_cli import kanban_capability_registry as registry
    path = home / 'courier.db'
    monkeypatch.setattr(registry, 'hub_db_path', lambda: path)
    with kbc.connect_closing() as conn:
        task = kb.create_task(conn, title='creator')
        assert kb.claim_task(conn, task) is not None
        run = kb._current_run_id(conn, task)
        # Portable run IDs are a synced-board feature. Keep the rest of the real
        # local transition path and transaction machinery unchanged.
        conn.execute('ALTER TABLE task_runs ADD COLUMN uid TEXT')
        conn.execute('UPDATE task_runs SET uid=? WHERE id=?', ('run-current', run))
        conn.commit()
    body = {'id': 'placement:worker:pending', 'from': 'gw:worker', 'coll': 'sessions',
            'action': 'origin-forward', 'args': {'run_uid': 'run-current',
                'creator': {'gateway': 'hub', 'profile': 'default', 'board': 'default', 'task_id': task}}}
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE sent(id TEXT PRIMARY KEY, state TEXT, result TEXT, body TEXT)')
        db.execute('INSERT INTO sent VALUES(?,?,?,?)', (body['id'], 'sent', '{}', json.dumps(body)))
    return task, run, path, body


def test_pending_forward_refuses_creator_park_atomically(pending):
    task, run, _, _ = pending
    with kbc.connect_closing() as conn:
        before = tuple(conn.execute('SELECT * FROM tasks WHERE id=?', (task,)).fetchone())
        with pytest.raises(ValueError, match='origin_forward_pending'):
            kb.schedule_task(conn, task, reason='wait for CI', expected_run_id=run, clock_only=True,
                             awaiting={'kind': 'clock', 'why': 'bounded CI window'})
        assert tuple(conn.execute('SELECT * FROM tasks WHERE id=?', (task,)).fetchone()) == before
        assert kb.latest_run(conn, task).ended_at is None


def test_pending_forward_refuses_creator_continuation(pending):
    task, run, _, _ = pending
    with kbc.connect_closing() as conn:
        with pytest.raises(ValueError, match='origin_forward_pending'):
            continuation.continue_task(conn, task, checkpoint='wait', expected_run_id=run)
        assert kb.get_task(conn, task).status == 'running'
        assert kb._current_run_id(conn, task) == run


@pytest.mark.parametrize('state', ['done', 'rejected'])
def test_settled_forward_allows_original_park(pending, state):
    task, run, path, _ = pending
    with sqlite3.connect(path) as db:
        db.execute('UPDATE sent SET state=?', (state,))
    with kbc.connect_closing() as conn:
        assert kb.schedule_task(conn, task, reason='pure calendar window', expected_run_id=run, clock_only=True,
                                awaiting={'kind': 'clock', 'why': 'calendar window window'})
        assert kb.get_task(conn, task).status == 'scheduled'
        assert kb.latest_run(conn, task).ended_at is not None


@pytest.mark.parametrize('change', ['run', 'task', 'action', 'coll'])
def test_unrelated_forward_does_not_hold_creator(pending, change):
    task, run, path, body = pending
    if change == 'run':
        body['args']['run_uid'] = 'old-run'
    elif change == 'task':
        body['args']['creator']['task_id'] = 'other-task'
    else:
        body[change] = 'unrelated'
    with sqlite3.connect(path) as db:
        db.execute('UPDATE sent SET body=?', (json.dumps(body),))
    with kbc.connect_closing() as conn:
        assert continuation.continue_task(conn, task, checkpoint='next', expected_run_id=run)['ok']


@pytest.mark.parametrize('state', ['local', 'queued', 'sent', 'failed', 'expired'])
def test_unresolved_forward_cannot_be_completed(pending, state):
    task, run, path, _ = pending
    with sqlite3.connect(path) as db:
        db.execute('UPDATE sent SET state=?', (state,))
    with kbc.connect_closing() as conn:
        with pytest.raises(ValueError, match='origin_forward_pending'):
            kb.complete_task(conn, task, summary='finished', expected_run_id=run)
        assert kb.get_task(conn, task).status == 'running'
        assert kb.latest_run(conn, task).ended_at is None


def test_kernel_reclaim_remains_possible(pending):
    task, run, _, _ = pending
    with kbc.connect_closing() as conn, kb.write_txn(conn):
        assert kb._end_run(conn, task, outcome='reclaimed') == run
        assert kb.latest_run(conn, task).ended_at is not None


def test_kernel_forced_park_remains_possible(pending):
    from hermes_cli.kanban_workspace_publication import involuntary_park
    task, run, _, _ = pending
    with kbc.connect_closing() as conn:
        assert involuntary_park(task, kb.schedule_task, conn, task,
                                expected_run_id=run, reason='calendar window', clock_only=True,
                                awaiting={'kind': 'clock', 'why': 'calendar window'})
        assert kb.latest_run(conn, task).ended_at is not None


def test_forward_reserves_before_enqueue(pending, monkeypatch):
    from hermes_cli import kanban_placement as placement
    task, run, path, body = pending
    with sqlite3.connect(path) as db:
        db.execute('DELETE FROM sent')
    monkeypatch.setattr(placement, 'hub_carries_options', lambda cli: True)
    monkeypatch.setattr(placement, 'remote_state', lambda rid: None)
    calls = []
    with kbc.connect_closing() as conn:
        def enqueue(argv):
            # Enqueue is paused BEFORE its courier row exists. The durable
            # board reservation must already prevent a concurrent lifecycle.
            with kbc.connect_closing() as other:
                with pytest.raises(ValueError, match='origin_forward_pending'):
                    continuation.continue_task(other, task, checkpoint='next', expected_run_id=run)
            calls.append(argv)
            return 3, '', 'queued offline'
        monkeypatch.setattr(placement, 'hub_runner', enqueue)
        out = placement.create_remote({'title': 'child', 'assignee': 'default'}, target='hub', self_gw='worker',
                                      origin_session='chat', origin_profile='default', origin_gateway='hub',
                                      creator=body['args']['creator'], run_uid='run-current', cfg={}, conn=conn)
        assert out['ok'], out
        assert len(calls) == 1


@pytest.mark.parametrize('missing', ['row', 'store'])
def test_reservation_survives_missing_courier_evidence(pending, missing):
    from hermes_cli.kanban_origin_forward_run_end import reserve
    task, run, path, body = pending
    with kbc.connect_closing() as conn:
        reserve(conn, task, 'run-current', body['id'])
        reserve(conn, task, 'run-current', body['id'])
        assert conn.execute("SELECT COUNT(*) FROM task_events WHERE kind='origin_forward_reserved'").fetchone()[0] == 1
    if missing == 'store':
        path.unlink()
    else:
        with sqlite3.connect(path) as db:
            db.execute('DELETE FROM sent')
    with kbc.connect_closing() as conn:
        with pytest.raises(ValueError, match='origin_forward_pending'):
            continuation.continue_task(conn, task, checkpoint='next', expected_run_id=run)
        assert kb.get_task(conn, task).status == 'running'


def test_ending_wins_before_submission(pending, monkeypatch):
    from hermes_cli import kanban_placement as placement
    task, run, path, body = pending
    with sqlite3.connect(path) as db:
        db.execute('DELETE FROM sent')
    monkeypatch.setattr(placement, 'hub_carries_options', lambda cli: True)
    monkeypatch.setattr(placement, 'remote_state', lambda rid: None)
    monkeypatch.setattr(placement, 'hub_runner', lambda argv: pytest.fail('ended creator must not enqueue'))
    with kbc.connect_closing() as conn:
        assert continuation.continue_task(conn, task, checkpoint='next', expected_run_id=run)['ok']
        with pytest.raises(ValueError, match='origin_forward_submit_run_ended'):
            placement.create_remote({'title': 'child', 'assignee': 'default'}, target='hub', self_gw='worker',
                                    origin_session='chat', origin_profile='default', origin_gateway='hub',
                                    creator=body['args']['creator'], run_uid='run-current', cfg={}, conn=conn)


@pytest.mark.parametrize('reason', [
    'canonical_home_schema_unavailable: core validation refused; nothing queued',
    'origin_request_invalid: invalid qualified identity or request ID',
    'origin_assignee_required: supply --assignee for tracked creation',
    'origin_forward_creator_required: foreign origins require creator owner and run',
    'create_option_unsupported: unsupported option',
])
def test_prequeue_refusal_releases_reservation(pending, monkeypatch, reason):
    from hermes_cli import kanban_placement as placement
    task, run, path, body = pending
    with sqlite3.connect(path) as db:
        db.execute('DELETE FROM sent')
    monkeypatch.setattr(placement, 'hub_carries_options', lambda cli: True)
    monkeypatch.setattr(placement, 'remote_state', lambda rid: None)
    monkeypatch.setattr(placement, 'hub_runner', lambda argv: (2, '', reason))
    with kbc.connect_closing() as conn:
        result = placement.create_remote({'title': 'child', 'assignee': 'default'}, target='hub', self_gw='worker',
                                         origin_session='chat', origin_profile='default', origin_gateway='hub',
                                         creator=body['args']['creator'], run_uid='run-current', cfg={}, conn=conn)
        assert not result['ok']
        assert kb.complete_task(conn, task, summary='submit clearly refused', expected_run_id=run)


def test_exec_failure_releases_reservation(pending, monkeypatch):
    from hermes_cli import kanban_placement as placement
    task, run, path, body = pending
    with sqlite3.connect(path) as db:
        db.execute('DELETE FROM sent')
    monkeypatch.setattr(placement, 'hub_carries_options', lambda cli: True)
    monkeypatch.setattr(placement, 'remote_state', lambda rid: None)
    with kbc.connect_closing() as conn:
        out = placement.create_remote({'title': 'child', 'assignee': 'default'}, target='hub', self_gw='worker',
                                      origin_session='chat', origin_profile='default', origin_gateway='hub',
                                      creator=body['args']['creator'], run_uid='run-current', cfg={}, conn=conn,
                                      hub_cli=str(path.parent / 'missing-hub-cli'))
        assert not out['ok'] and out['rc'] == 127
        assert kb.complete_task(conn, task, summary='exec never started', expected_run_id=run)


@pytest.mark.parametrize('case', ['timeout', 'unknown', 'queued', 'unreadable', 'concurrent', 'retry'])
def test_uncertain_or_other_submission_still_holds_run(pending, monkeypatch, case):
    from hermes_cli import kanban_placement as placement
    from hermes_cli.kanban_origin_forward_run_end import reserve
    task, run, path, body = pending
    with sqlite3.connect(path) as db:
        db.execute('DELETE FROM sent')
    monkeypatch.setattr(placement, 'hub_carries_options', lambda cli: True)
    monkeypatch.setattr(placement, 'remote_state', lambda rid: None)
    reason = 'origin_request_invalid: invalid qualified identity or request ID'
    def courier(argv):
        rid = argv[argv.index('--id') + 1]
        if case == 'queued':
            with sqlite3.connect(path) as db:
                db.execute('INSERT INTO sent VALUES(?,?,?,?)', (rid, 'sent', '{}', json.dumps(body)))
        if case == 'unreadable':
            path.write_bytes(b'not a database')
        if case == 'concurrent':
            with kbc.connect_closing() as other:
                reserve(other, task, 'run-current', rid, submission_id='another-attempt')
        return (127, '', 'timed out') if case == 'timeout' else (2, '', 'unknown' if case == 'unknown' else reason)
    monkeypatch.setattr(placement, 'hub_runner', courier)
    kwargs = dict(target='hub', self_gw='worker', origin_session='chat', origin_profile='default',
                  origin_gateway='hub', creator=body['args']['creator'], run_uid='run-current', cfg={})
    with kbc.connect_closing() as conn:
        placement.create_remote({'title': 'child', 'assignee': 'default'}, conn=conn, **kwargs)
        if case == 'retry':
            monkeypatch.setattr(placement, 'hub_runner', lambda argv: (3, '', 'queued offline'))
            placement.create_remote({'title': 'child', 'assignee': 'default'}, conn=conn, **kwargs)
        with pytest.raises(ValueError, match='origin_forward_(pending|evidence_unavailable)'):
            kb.complete_task(conn, task, summary='must remain alive', expected_run_id=run)
        assert kb.latest_run(conn, task).ended_at is None


def test_unreadable_courier_is_not_absence(pending):
    task, run, path, _ = pending
    path.write_bytes(b'not a database')
    with kbc.connect_closing() as conn:
        with pytest.raises(ValueError, match='origin_forward_evidence_unavailable'):
            continuation.continue_task(conn, task, checkpoint='next', expected_run_id=run)
        assert kb.get_task(conn, task).status == 'running'
