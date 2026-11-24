"""A remaining dependency retains its original deadline after unlink. [daveey]"""
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect
from tests.hermes_cli.test_kanban_actionability_todo_sources import T0, _clock, _event, _ref, _refs, _rows


def test_unlink_remaining_dependency_carries_overdue_deadline(tmp_path, monkeypatch):
    _clock(monkeypatch, T0)
    with connect(tmp_path / 'kanban.db') as conn:
        first = kb.create_task(conn, title='first prerequisite', assignee='default')
        second = kb.create_task(conn, title='second prerequisite', assignee='default')
        tid = kb.create_task(conn, title='waiting child', assignee='default', parents=[first, second])
        source, initial = _event(conn, tid, 'dependency_wait')
        frozen = initial['actionability_intent']['deadline_at']
        _clock(monkeypatch, frozen + 100)
        assert kb.unlink_tasks(conn, first, tid)
        task = kb.get_task(conn, tid)
        assert task is not None and task.status == 'todo'
        unlinked, data = _event(conn, tid, 'unlinked')
        assert data['actionability_intent']['deadline_at'] == frozen
        assert data['actionability_carried_from'] == _ref(source)
        assert data['actionability_parent_ids'] == [second]
        for row in _rows(conn, tid, frozen + 100):
            assert row['source_refs'] == _refs(unlinked)
            assert row['escalation']['at'] == frozen
            assert 'escalation_overdue' in row['violations']
            assert 'creation_intent_invalid' not in row['violations']
            assert 'escalation_deadline_missing' not in row['violations']
        third = kb.create_task(conn, title='replacement prerequisite', assignee='default')
        _clock(monkeypatch, frozen + 200)
        kb.link_tasks(conn, third, tid)
        linked, data = _event(conn, tid, 'linked')
        assert data['actionability_carried_from'] == _ref(unlinked)
        assert data['actionability_intent']['deadline_at'] == frozen
        kb.unlink_tasks(conn, second, tid)
        _, data = _event(conn, tid, 'unlinked')
        assert data['actionability_carried_from'] == _ref(linked)
        assert data['actionability_intent']['deadline_at'] == frozen
        kb.unlink_tasks(conn, third, tid)
        _, data = _event(conn, tid, 'unlinked')
        assert 'actionability_intent' not in data
        kb.recompute_ready(conn)
        task = kb.get_task(conn, tid)
        assert task is not None and task.status == 'ready'


def test_unlink_parked_child_preserves_schedule_source(tmp_path, monkeypatch):
    _clock(monkeypatch, T0)
    with connect(tmp_path / 'kanban.db') as conn:
        tid = kb.create_task(conn, title='parked child', assignee='default')
        assert kb.schedule_task(conn, tid, reason='SCHEDULED_UNTIL=2027-01-15T08:01:40+00:00',
                                clock_only=True, awaiting={'kind': 'clock', 'why': 'fixture window'})
        scheduled, initial = _event(conn, tid, 'scheduled')
        first = kb.create_task(conn, title='first prerequisite', assignee='default')
        second = kb.create_task(conn, title='second prerequisite', assignee='default')
        kb.link_tasks(conn, first, tid)
        kb.link_tasks(conn, second, tid)
        kb.unlink_tasks(conn, first, tid)
        _, data = _event(conn, tid, 'unlinked')
        assert 'actionability_intent' not in data
        task = kb.get_task(conn, tid)
        assert task is not None and task.status == 'scheduled'
        assert _event(conn, tid, 'scheduled')[1] == initial
        assert scheduled['kind'] == 'scheduled'
