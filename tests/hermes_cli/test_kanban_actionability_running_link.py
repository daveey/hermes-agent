"""The owning run's link-before-park is a dependency wait with a frozen deadline. [daveey]"""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect
from tests.hermes_cli.test_kanban_actionability_todo_sources import T0, _clock, _event, _ref, _refs, _rows


def _running(conn, title='running child'):
    tid = kb.create_task(conn, title=title, assignee='default')
    task = kb.claim_task(conn, tid)
    assert task is not None and task.status == 'running'
    return tid, task.current_run_id


def test_owning_run_link_before_park_freezes_a_deadline(tmp_path, monkeypatch):
    """Observed 2026-11-25: claimed 1795589619, linked 1795589999, parked 1795590053; census between."""
    _clock(monkeypatch, T0)
    with connect(tmp_path / 'kanban.db') as conn:
        tid, run = _running(conn)
        parent = kb.create_task(conn, title='review', assignee='default')
        _clock(monkeypatch, T0 + 300)
        kb.link_tasks(conn, parent, tid, expected_child_run_id=run)
        linked, data = _event(conn, tid, 'linked')
        intent = data['actionability_intent']
        assert intent['status'] == 'running' and intent['deadline_at'] == T0 + 300 + 86400
        assert intent['action'] == 'satisfy_dependency' and intent['execution_authority'] is False
        for row in _rows(conn, tid, T0 + 400):
            assert row['status'] == 'running' and row['action'] == 'satisfy_dependency'
            assert row['blocker'] and row['dependencies'][0]['task_id'] == parent  # still a wait, not exempt
            assert row['source_refs'] == _refs(linked)
            assert row['escalation']['at'] == T0 + 300 + 86400
            assert row['escalation']['state'] != 'not_required'
            assert 'escalation_deadline_missing' not in row['violations']
            assert 'creation_intent_invalid' not in row['violations']
        for row in _rows(conn, tid, T0 + 300 + 86400):
            assert 'escalation_overdue' in row['violations']


def test_relink_in_the_same_run_never_renews(tmp_path, monkeypatch):
    _clock(monkeypatch, T0)
    with connect(tmp_path / 'kanban.db') as conn:
        tid, run = _running(conn)
        first = kb.create_task(conn, title='first', assignee='default')
        second = kb.create_task(conn, title='second', assignee='default')
        kb.link_tasks(conn, first, tid, expected_child_run_id=run)
        source, _ = _event(conn, tid, 'linked')
        frozen = T0 + 86400
        _clock(monkeypatch, frozen + 50)
        kb.link_tasks(conn, second, tid, expected_child_run_id=run)
        linked, data = _event(conn, tid, 'linked')
        assert data['actionability_intent']['deadline_at'] == frozen
        assert data['actionability_carried_from'] == _ref(source)
        for row in _rows(conn, tid, frozen + 60):
            assert row['source_refs'] == _refs(linked)
            assert row['escalation']['at'] == frozen
            assert 'escalation_overdue' in row['violations']
        # An unlink keeps the remaining wait's clock; removing the last parent ends the wait.
        kb.unlink_tasks(conn, first, tid)
        _, data = _event(conn, tid, 'unlinked')
        assert data['actionability_intent']['deadline_at'] == frozen
        kb.unlink_tasks(conn, second, tid)
        _, data = _event(conn, tid, 'unlinked')
        assert 'actionability_intent' not in data
        for row in _rows(conn, tid, frozen + 70):
            assert row['action'] == 'execute_current_scope' and row['dependencies'] == []
            assert row['escalation']['state'] == 'not_required'


def test_unlink_cannot_start_a_running_clock(tmp_path, monkeypatch):
    """unlink_tasks has no owning-run check, so it only ever preserves a handoff obligation."""
    _clock(monkeypatch, T0)
    with connect(tmp_path / 'kanban.db') as conn:
        tid, run = _running(conn)
        first = kb.create_task(conn, title='first', assignee='default')
        second = kb.create_task(conn, title='second', assignee='default')
        for parent in (first, second):
            kb.link_tasks(conn, parent, tid, expected_child_run_id=run)
        conn.execute("UPDATE task_events SET payload='{}' WHERE task_id=? AND kind='linked'", (tid,))
        kb.unlink_tasks(conn, first, tid)
        _, data = _event(conn, tid, 'unlinked')
        assert 'actionability_intent' not in data
        for row in _rows(conn, tid, T0 + 10):
            assert row['action'] == 'satisfy_dependency'
            assert 'escalation_deadline_missing' in row['violations']


@pytest.mark.parametrize('damage', ['parents', 'intent', 'assignee', 'human'])
def test_damaged_or_stale_handoff_stays_a_violation(tmp_path, monkeypatch, damage):
    _clock(monkeypatch, T0)
    with connect(tmp_path / 'kanban.db') as conn:
        tid, run = _running(conn)
        parent = kb.create_task(conn, title='review', assignee='default')
        other = kb.create_task(conn, title='other', assignee='default')
        kb.link_tasks(conn, parent, tid, expected_child_run_id=run)
        if damage == 'parents':  # an edge that arrived without its event
            conn.execute('INSERT INTO task_links(parent_id, child_id) VALUES (?,?)', (other, tid))
        elif damage == 'intent':
            conn.execute("UPDATE task_events SET payload=json_set(payload,'$.actionability_intent.deadline_at',?) "
                         "WHERE task_id=? AND kind='linked'", (T0 + 10 * 86400, tid))
        elif damage == 'assignee':
            conn.execute("UPDATE tasks SET assignee='someone-else' WHERE id=?", (tid,))
        else:
            conn.execute("UPDATE tasks SET human_request=? WHERE id=?", ('{"action": "new ask"}', tid))
        for row in _rows(conn, tid, T0 + 10):
            assert row['escalation']['at'] is None
            assert row['escalation']['state'] != 'not_required'
            assert {'creation_intent_invalid', 'escalation_deadline_missing'} & set(row['violations'])
            assert row['action'] != 'execute_current_scope'


def test_next_run_does_not_inherit_the_previous_runs_handoff(tmp_path, monkeypatch):
    _clock(monkeypatch, T0)
    with connect(tmp_path / 'kanban.db') as conn:
        tid, run = _running(conn)
        parent = kb.create_task(conn, title='review', assignee='default')
        kb.link_tasks(conn, parent, tid, expected_child_run_id=run)
        # Replicated claim of a later run arrives while the edge is still present.
        _clock(monkeypatch, T0 + 500)
        kb._append_event(conn, tid, 'claimed', {'lock': 'later-run'})
        claimed, _ = _event(conn, tid, 'claimed')
        for row in _rows(conn, tid, T0 + 600):
            assert row['source_refs'] == _refs(claimed)
            assert row['escalation']['at'] is None
            assert 'escalation_deadline_missing' in row['violations']
        other = kb.create_task(conn, title='other', assignee='default')
        _clock(monkeypatch, T0 + 700)
        kb.link_tasks(conn, other, tid, expected_child_run_id=run)
        _, data = _event(conn, tid, 'linked')
        assert 'actionability_carried_from' not in data  # a new claim is a new episode
        assert data['actionability_intent']['deadline_at'] == T0 + 700 + 86400


def test_foreign_link_to_running_child_is_still_refused(tmp_path, monkeypatch):
    _clock(monkeypatch, T0)
    with connect(tmp_path / 'kanban.db') as conn:
        tid, _run = _running(conn)
        parent = kb.create_task(conn, title='review', assignee='default')
        with pytest.raises(ValueError):
            kb.link_tasks(conn, parent, tid)
        assert conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='linked'", (tid,)).fetchone() is None


def test_park_after_handoff_uses_the_park_source(tmp_path, monkeypatch):
    _clock(monkeypatch, T0)
    with connect(tmp_path / 'kanban.db') as conn:
        tid, run = _running(conn)
        parent = kb.create_task(conn, title='review', assignee='default')
        kb.link_tasks(conn, parent, tid, expected_child_run_id=run)
        _clock(monkeypatch, T0 + 60)
        assert kb.block_task(conn, tid, reason='waiting on review', kind='dependency')
        task = kb.get_task(conn, tid)
        assert task is not None and task.status != 'running'
        for row in _rows(conn, tid, T0 + 70):
            assert row['escalation']['at'] is not None
            assert 'escalation_deadline_missing' not in row['violations']
            assert 'creation_intent_invalid' not in row['violations']


def test_todo_stamped_link_sorting_after_the_claim_is_not_a_running_handoff(tmp_path, monkeypatch):
    """A replicated todo-episode `linked` whose clock sorts after the claim: the claim stays the source."""
    _clock(monkeypatch, T0)
    with connect(tmp_path / 'kanban.db') as conn:
        first = kb.create_task(conn, title='first', assignee='default')
        tid = kb.create_task(conn, title='waiting child', assignee='default', parents=[first])
        second = kb.create_task(conn, title='second', assignee='default')
        _clock(monkeypatch, T0 + 5000)
        kb.link_tasks(conn, second, tid)  # stamped while the child waits in todo
        linked, data = _event(conn, tid, 'linked')
        assert data['actionability_intent']['status'] == 'todo'
        # The card is running now, and its claim carries an earlier clock than the skewed edge event.
        _clock(monkeypatch, T0 + 100)
        kb._append_event(conn, tid, 'claimed', {'lock': 'run'})
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (tid,))
        claimed, _ = _event(conn, tid, 'claimed')
        assert (linked['created_at'], linked['id']) > (claimed['created_at'], 0)
        for row in _rows(conn, tid, T0 + 6000):
            assert row['source_refs'] == _refs(claimed)  # never the todo edge
            assert row['escalation']['at'] is None
            assert row['violations'] == ['escalation_deadline_missing']
