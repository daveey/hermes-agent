"""Recorder and local report must select the same ordinary lifecycle source."""
import json

import pytest

from hermes_cli.kanban_actionability_creation import validated_intent

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect
from hermes_cli.kanban_actionability_sources import describe
from hermes_cli.kanban_actionability_observation import observe
from tests.hermes_cli.test_kanban_actionability_observation import Hub, inventory


def test_todo_latest_link_is_shared_source(tmp_path):
    with connect(tmp_path / 'kanban.db') as conn:
        tid = kb.create_task(conn, title='ordinary dependency wait', assignee='default')
        conn.execute("UPDATE tasks SET status='todo' WHERE id=?", (tid,))
        kb._append_event(conn, tid, 'scheduled', {'wake_at': 100})
        kb._append_event(conn, tid, 'linked', {'parent_id': 'historical-parent'})
        events = [dict(row) for row in conn.execute(
            'SELECT * FROM task_events WHERE task_id=? ORDER BY id', (tid,))]
        hub = Hub()
        data = inventory(tid, 'todo')
        data['events'] = [{**event, 'uid': event.get('uid') or str(event['id']),
                           'ord': index, 'version': 1, 'deleted_at': None}
                          for index, event in enumerate(events, 1)]
        hub.data[tid] = data
        expected = observe(hub, 'default', tid)['facts']['source_uids'][0]['uid']
        before = list(conn.iterdump())
        actual = describe(conn, tid)
        assert actual['source_refs'] == [expected]
        assert actual['action'] != 'execute_current_scope'
        assert list(conn.iterdump()) == before
        assert json.loads(events[-1]['payload'])['parent_id'] == 'historical-parent'


@pytest.mark.parametrize('status,kind', [('todo', 'linked'), ('running', 'claimed')])
def test_valid_park_intent_is_not_reported_corrupt_after_new_source(tmp_path, monkeypatch, status, kind):
    """Live census shape: valid scheduled bytes survive a later link/claim in history."""
    monkeypatch.setattr('time.time', lambda: 1_800_000_000)
    with connect(tmp_path / 'kanban.db') as conn:
        tid = kb.create_task(conn, title='park then advance', assignee='default')
        conn.execute("UPDATE tasks SET status='scheduled' WHERE id=?", (tid,))
        kb._append_event(conn, tid, 'scheduled', {
            'wake_at': 1_800_000_100, 'awaiting': {'kind': 'clock'}})
        event = dict(conn.execute("SELECT * FROM task_events WHERE task_id=? AND kind='scheduled'", (tid,)).fetchone())
        event['data'] = json.loads(event['payload'])
        task = dict(conn.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone())
        assert validated_intent(task, event, parent_ids=[]) is not None
        conn.execute('UPDATE tasks SET status=? WHERE id=?', (status, tid))
        kb._append_event(conn, tid, kind, {})
        latest = dict(conn.execute('SELECT * FROM task_events WHERE task_id=? ORDER BY id DESC LIMIT 1', (tid,)).fetchone())
        before = list(conn.iterdump())
        row = describe(conn, tid)
        assert 'creation_intent_invalid' not in row['violations']
        assert row['source_refs'] == [latest.get('uid') or str(latest['id'])]
        assert row['escalation']['at'] is None  # no inherited or renewed deadline
        assert list(conn.iterdump()) == before
