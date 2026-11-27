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


def test_unreadable_courier_is_not_absence(pending):
    task, run, path, _ = pending
    path.write_bytes(b'not a database')
    with kbc.connect_closing() as conn:
        with pytest.raises(ValueError, match='origin_forward_evidence_unavailable'):
            continuation.continue_task(conn, task, checkpoint='next', expected_run_id=run)
        assert kb.get_task(conn, task).status == 'running'
