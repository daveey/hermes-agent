"""Every todo-entry source freezes one administrative deadline; links never renew it. [daveey]"""
import json

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect
from hermes_cli.kanban_actionability_cli import report
from hermes_cli.kanban_actionability_sources import describe

T0 = 1_800_000_000


def _clock(monkeypatch, at):
    monkeypatch.setattr('time.time', lambda: at)


def _event(conn, tid, kind):
    row = conn.execute('SELECT * FROM task_events WHERE task_id=? AND kind=? ORDER BY id DESC LIMIT 1',
                       (tid, kind)).fetchone()
    return row, json.loads(row['payload'] or 'null')


def _rows(conn, tid, now):
    return (describe(conn, tid, now=now), next(r for r in report(conn, now=now)['rows'] if r['task_id'] == tid))


def _ref(event):
    """Carried provenance: the synced uid, or the local id on an unsynced fixture board."""
    return event['uid'] if 'uid' in event.keys() and event['uid'] else str(event['id'])


def _refs(event):
    return [_ref(event)]


def test_link_to_waiting_todo_child_keeps_its_frozen_deadline(tmp_path, monkeypatch):
    """Observed: a dependency_wait stamped at creation, then an unstamped `linked` replaced it."""
    _clock(monkeypatch, T0)
    with connect(tmp_path / 'kanban.db') as conn:
        first = kb.create_task(conn, title='first prerequisite', assignee='default')
        tid = kb.create_task(conn, title='waiting child', assignee='default', parents=[first])
        wait, data = _event(conn, tid, 'dependency_wait')
        frozen = data['actionability_intent']['deadline_at']
        assert frozen == T0 + 86400
        second = kb.create_task(conn, title='second prerequisite', assignee='default')
        _clock(monkeypatch, T0 + 5000)
        kb.link_tasks(conn, second, tid)
        linked, data = _event(conn, tid, 'linked')
        intent = data['actionability_intent']
        assert intent['deadline_at'] == frozen  # the new edge does not renew the obligation
        assert intent['status'] == 'todo' and intent['execution_authority'] is False
        assert data['actionability_carried_from'] == _ref(wait)
        assert second in intent['blocker']['trigger'] and first in intent['blocker']['trigger']
        for row in _rows(conn, tid, T0 + 5000):
            assert row['source_refs'] == _refs(linked)
            assert row['escalation']['at'] == frozen
            assert 'escalation_deadline_missing' not in row['violations']


def test_repeated_and_overdue_links_never_renew(tmp_path, monkeypatch):
    _clock(monkeypatch, T0)
    with connect(tmp_path / 'kanban.db') as conn:
        parent = kb.create_task(conn, title='prerequisite', assignee='default')
        tid = kb.create_task(conn, title='waiting child', assignee='default', parents=[parent])
        frozen = T0 + 86400
        other = kb.create_task(conn, title='later prerequisite', assignee='default')
        _clock(monkeypatch, frozen + 100)
        kb.link_tasks(conn, other, tid)
        kb.link_tasks(conn, other, tid)  # readback/idempotent relink
        _, data = _event(conn, tid, 'linked')
        assert data['actionability_intent']['deadline_at'] == frozen
        assert data['actionability_intent']['invalid_reason'] is None
        for row in _rows(conn, tid, frozen + 100):
            assert row['escalation']['at'] == frozen
            assert 'escalation_overdue' in row['violations']
            assert 'escalation_deadline_missing' not in row['violations']


def test_link_that_demotes_a_ready_child_gets_a_fresh_deadline(tmp_path, monkeypatch):
    _clock(monkeypatch, T0)
    with connect(tmp_path / 'kanban.db') as conn:
        tid = kb.create_task(conn, title='ready child', assignee='default')
        parent = kb.create_task(conn, title='new prerequisite', assignee='default')
        _clock(monkeypatch, T0 + 50)
        assert kb.link_tasks(conn, parent, tid) is True
        wait, wait_data = _event(conn, tid, 'dependency_wait')
        _, data = _event(conn, tid, 'linked')
        assert data['actionability_intent']['deadline_at'] == T0 + 50 + 86400
        assert data['actionability_carried_from'] == _ref(wait)
        assert wait_data['actionability_intent']['deadline_at'] == T0 + 50 + 86400
        for row in _rows(conn, tid, T0 + 50):
            assert row['escalation']['at'] == T0 + 50 + 86400
            assert 'escalation_deadline_missing' not in row['violations']


def test_carry_does_not_cross_a_completed_todo_episode(tmp_path, monkeypatch):
    _clock(monkeypatch, T0)
    with connect(tmp_path / 'kanban.db') as conn:
        first = kb.create_task(conn, title='first prerequisite', assignee='default')
        tid = kb.create_task(conn, title='child', assignee='default', parents=[first])
        _clock(monkeypatch, T0 + 1000)
        kb.complete_task(conn, first, summary='done')
        kb.recompute_ready(conn)
        assert kb.get_task(conn, tid).status == 'ready'
        second = kb.create_task(conn, title='second prerequisite', assignee='default')
        _clock(monkeypatch, T0 + 2000)
        assert kb.link_tasks(conn, second, tid) is True
        _, data = _event(conn, tid, 'linked')
        assert data['actionability_intent']['deadline_at'] == T0 + 2000 + 86400


def test_carry_ignores_a_todo_source_from_an_earlier_episode(tmp_path, monkeypatch):
    """A todo re-entry by a path that writes no todo source must not inherit the old obligation."""
    _clock(monkeypatch, T0)
    with connect(tmp_path / 'kanban.db') as conn:
        first = kb.create_task(conn, title='first prerequisite', assignee='default')
        tid = kb.create_task(conn, title='child', assignee='default', parents=[first])
        _clock(monkeypatch, T0 + 1000)
        kb.complete_task(conn, first, summary='done')
        kb.recompute_ready(conn)
        assert kb.get_task(conn, tid).status == 'ready'
        conn.execute("UPDATE tasks SET status='todo' WHERE id=?", (tid,))  # an unstamped re-entry
        second = kb.create_task(conn, title='second prerequisite', assignee='default')
        _clock(monkeypatch, T0 + 2000)
        kb.link_tasks(conn, second, tid)
        _, data = _event(conn, tid, 'linked')
        assert 'actionability_carried_deadline' not in data
        assert data['actionability_intent']['deadline_at'] == T0 + 2000 + 86400


def test_unblock_into_todo_freezes_a_deadline(tmp_path, monkeypatch):
    """Observed: scheduled -> linked (unstamped, still scheduled) -> unblocked {status: todo}."""
    _clock(monkeypatch, T0)
    with connect(tmp_path / 'kanban.db') as conn:
        tid = kb.create_task(conn, title='parked child', assignee='default')
        assert kb.schedule_task(conn, tid, reason='SCHEDULED_UNTIL=2027-01-15T08:01:40+00:00',
                                clock_only=True, awaiting={'kind': 'clock', 'why': 'fixture window'})
        parent = kb.create_task(conn, title='late prerequisite', assignee='default')
        kb.link_tasks(conn, parent, tid)
        assert kb.get_task(conn, tid).status == 'scheduled'
        _clock(monkeypatch, T0 + 300)
        assert kb.unblock_task(conn, tid)
        assert kb.get_task(conn, tid).status == 'todo'
        source, data = _event(conn, tid, 'unblocked')
        intent = data['actionability_intent']
        assert intent['deadline_at'] == T0 + 300 + 86400 and intent['status'] == 'todo'
        assert parent in intent['blocker']['trigger']
        for row in _rows(conn, tid, T0 + 300):
            assert row['source_refs'] == _refs(source)
            assert row['escalation']['at'] == T0 + 300 + 86400
            assert 'escalation_deadline_missing' not in row['violations']


def test_unblock_to_ready_and_link_to_parked_child_stay_unstamped(tmp_path, monkeypatch):
    _clock(monkeypatch, T0)
    with connect(tmp_path / 'kanban.db') as conn:
        tid = kb.create_task(conn, title='parked child', assignee='default')
        assert kb.schedule_task(conn, tid, reason='SCHEDULED_UNTIL=2027-01-15T08:01:40+00:00',
                                clock_only=True, awaiting={'kind': 'clock', 'why': 'fixture window'})
        parent = kb.create_task(conn, title='prerequisite', assignee='default')
        kb.link_tasks(conn, parent, tid)
        _, linked = _event(conn, tid, 'linked')
        assert 'actionability_intent' not in linked  # the card still waits on its own schedule
        kb.unlink_tasks(conn, parent, tid)
        assert kb.unblock_task(conn, tid)
        assert kb.get_task(conn, tid).status == 'ready'
        _, data = _event(conn, tid, 'unblocked')
        assert not isinstance(data, dict) or 'actionability_intent' not in data


def test_tampered_carried_deadline_is_invalid(tmp_path, monkeypatch):
    _clock(monkeypatch, T0)
    with connect(tmp_path / 'kanban.db') as conn:
        first = kb.create_task(conn, title='first prerequisite', assignee='default')
        tid = kb.create_task(conn, title='waiting child', assignee='default', parents=[first])
        second = kb.create_task(conn, title='second prerequisite', assignee='default')
        kb.link_tasks(conn, second, tid)
        linked, data = _event(conn, tid, 'linked')
        data['actionability_carried_deadline'] = T0 + 100  # no longer the deadline the intent froze
        conn.execute('UPDATE task_events SET payload=? WHERE id=?', (json.dumps(data), linked['id']))
        row = describe(conn, tid, now=T0)
        assert row['escalation']['at'] is None
        assert 'creation_intent_invalid' in row['violations']
