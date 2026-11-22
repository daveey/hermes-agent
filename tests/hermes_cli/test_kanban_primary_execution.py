"""PRIMARY reads retain executor evidence without acquiring local authority."""
import sqlite3
from dataclasses import make_dataclass

import pytest

from hermes_cli import kanban_session_activity as activity
from hermes_cli.kanban_queue_position import _work_observations
from tests.hermes_cli import test_kanban_source_snapshot as fixtures

kanban_root = fixtures.kanban_root
board = fixtures.board


@pytest.fixture
def primary(board):
    with sqlite3.connect(board) as conn:
        conn.execute("CREATE TABLE kanban_sync_state (mode TEXT)")
        conn.execute("INSERT INTO kanban_sync_state VALUES ('primary')")
        for column in ('uid TEXT', 'status TEXT', 'last_heartbeat_at INTEGER', 'ended_at INTEGER'):
            conn.execute('ALTER TABLE task_runs ADD COLUMN ' + column)
        conn.execute("INSERT INTO task_runs VALUES (7,'card',900,'portable-run','running',990,NULL)")
        conn.execute("ALTER TABLE task_events ADD COLUMN uid TEXT")
        conn.execute("INSERT INTO task_events (task_id,run_id,kind,created_at,uid) VALUES ('card',7,'heartbeat',990,'beat-current')")
        conn.execute("UPDATE tasks SET status='running',current_run_id=7")
    return board


def read_all(path):
    row = {'id': 'session'}
    activity.annotate_kanban_activity([row])
    listed = activity.session_tasks(['session'])[0]['execution_observations'][0]
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        task_type = make_dataclass('Task', [('id', str), ('status', str), ('assignee', str)])
        detailed = _work_observations(conn, task_type('card', 'running', 'agent'), 'default')[0]
    return listed, row['execution_observations'][0], detailed


def test_primary_missing_legacy_schema_stays_unknown(primary):
    with sqlite3.connect(primary) as conn:
        conn.execute('ALTER TABLE task_events DROP COLUMN uid')
    assert all(value['state'] == 'unknown' for value in read_all(primary))


def test_primary_ask_survives_in_list_and_counts(primary):
    import json
    request = {'action': 'Approve', 'why_human': 'Owner needed', 'interaction': 'approval',
               'scope': 'fixture', 'evidence': 'fixture', 'resolved_check': 'private-probe'}
    with sqlite3.connect(primary) as conn:
        conn.execute('ALTER TABLE tasks ADD COLUMN human_request TEXT')
        conn.execute('UPDATE tasks SET human_request=?', (json.dumps(request),))
        # Production PRIMARY rows do not replicate these heartbeat columns.
        conn.execute('UPDATE task_runs SET last_heartbeat_at=NULL')
    listed, counted, _ = read_all(primary)
    assert listed == counted
    assert listed['state'] == 'running'
    assert listed['human_request']['action'] == 'Approve'
    assert 'private-probe' not in repr(listed)


def test_primary_adapter_failure_does_not_fallback_to_local_authority(primary, monkeypatch):
    from hermes_cli import kanban_primary_execution
    def fail(*args, **kwargs):
        raise sqlite3.OperationalError('unreadable')
    monkeypatch.setattr(kanban_primary_execution, 'materialize', fail)
    assert all(value['state'] == 'unknown' for value in read_all(primary))


def test_primary_count_derivation_runs_after_close(primary, monkeypatch):
    from hermes_cli import kanban_primary_execution as adapter

    opened, derived = [], []
    original_open, original_derive = activity._open_ro, adapter.attach_materialized

    def open_one(path):
        conn = original_open(path)
        opened.append(conn)
        return conn

    def derive(values, rows, **scope):
        assert len(opened) == 1
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            opened[0].execute('SELECT 1')
        result = original_derive(values, rows, **scope)
        derived.append(result)
        return result

    monkeypatch.setattr(activity, '_open_ro', open_one)
    monkeypatch.setattr(adapter, 'attach_materialized', derive)
    reads, complete = activity._read_board(primary, parked=True, stalled=True)
    assert complete and derived
    assert reads['execution']['card']['state'] == 'running'
    assert reads['execution']['card']['observed_at'] == 990


def test_primary_current_run_projects_running_in_every_consumer(primary):
    values = read_all(primary)
    for value in values:
        assert value['state'] == 'running', values
        assert value['identity']['run_id'] == 'portable-run'
        assert value['observed_at'] == 990
        assert value['source'] == 'kanban.replica_run'
        assert value['last_progress'] is None
    assert values[0] == values[1]
    assert values[0]['identity'] == values[2]['identity']


@pytest.mark.parametrize('sql', [
    "DELETE FROM task_events WHERE kind='heartbeat'",
    "UPDATE task_events SET run_id=8 WHERE kind='heartbeat'",
    "UPDATE task_events SET task_id='other' WHERE kind='heartbeat'",
    "UPDATE task_events SET uid=NULL WHERE kind='heartbeat'",
    "UPDATE task_events SET created_at=NULL WHERE kind='heartbeat'",
    "UPDATE task_events SET kind='commented' WHERE kind='heartbeat'",
    "UPDATE task_runs SET id=8",
    "UPDATE task_runs SET uid=NULL",
    "UPDATE task_runs SET task_id='other'",
    "UPDATE task_runs SET ended_at=995",
    "UPDATE task_runs SET status='released'",
    "UPDATE tasks SET current_run_id=NULL",
    "UPDATE tasks SET status='ready'",
    "UPDATE kanban_sync_state SET mode='unrecognized'",
])
def test_incomplete_primary_evidence_remains_unknown(primary, sql):
    with sqlite3.connect(primary) as conn:
        conn.execute(sql)
    for value in read_all(primary):
        assert value['state'] == 'unknown'


def test_primary_preserves_local_row_routing_shape(primary):
    primary_row = activity.session_tasks(['session'])[0]
    with sqlite3.connect(primary) as conn:
        conn.execute("UPDATE kanban_sync_state SET mode='off'")
    local_row = activity.session_tasks(['session'])[0]
    assert set(primary_row) == set(local_row)
    assert 'gateway' not in primary_row
    assert 'owner_profile' not in primary_row
    assert primary_row.get('remote') == local_row.get('remote')


def test_primary_uses_latest_beat_of_current_run_only(primary):
    with sqlite3.connect(primary) as conn:
        conn.executemany(
            'INSERT INTO task_events (task_id,run_id,kind,created_at,uid) VALUES (?,?,?,?,?)',
            [('card', 7, 'heartbeat', 985, 'older-current'),
             ('card', 7, 'heartbeat', 995, 'newest-current'),
             ('card', 6, 'heartbeat', 999, 'earlier-run')],
        )
    for value in read_all(primary):
        assert value['observed_at'] == 995
        assert value['source_revision'] == 'event:newest-current'
        assert value['identity']['run_id'] == 'portable-run'


def test_primary_clock_expires_without_rejuvenation(primary, monkeypatch):
    monkeypatch.setattr(activity.time, 'time', lambda: 1200)
    for value in read_all(primary):
        assert value['state'] == 'unknown'
        assert value['observed_at'] == 990
        assert value['unknown_reason'] == 'source_expired'
