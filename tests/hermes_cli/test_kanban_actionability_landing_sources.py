"""A wake that lands in todo freezes a deadline; a todo unblock is never a ready card's source. [daveey]"""
import json

import pytest

from hermes_cli import kanban_continuation as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_wake_check as kwc
from hermes_cli.kanban_actionability_cli import report
from hermes_cli.kanban_actionability_sources import describe
from hermes_cli.kanban_db_connect import connect
from tests.hermes_cli import test_kanban_containment_actor as support

board = support.board
T0 = 1_800_000_000
CLOCK = {'kind': 'clock', 'why': 'the release window opens then'}


def _clock(monkeypatch, at):
    monkeypatch.setattr('time.time', lambda: at)


def _event(conn, tid, kind):
    row = conn.execute('SELECT * FROM task_events WHERE task_id=? AND kind=? ORDER BY id DESC LIMIT 1',
                       (tid, kind)).fetchone()
    return row, json.loads(row['payload'] or 'null')


def _rows(conn, tid, now):
    return (describe(conn, tid, now=now), next(r for r in report(conn, now=now)['rows'] if r['task_id'] == tid))


def _ref(event):
    return event['uid'] if 'uid' in event.keys() and event['uid'] else str(event['id'])


def _parked_with_late_parent(conn, monkeypatch):
    """Observed: a card parks, an orchestrator links a prerequisite to the parked card, then the wake fires."""
    _clock(monkeypatch, T0)
    tid = kb.create_task(conn, title='parked card', assignee='default')
    assert kb.schedule_task(conn, tid, reason='SCHEDULED_UNTIL=2099-01-01T00:00:00Z release window', awaiting=CLOCK,
                            clock_only=True)
    parent = kb.create_task(conn, title='late prerequisite', assignee='default')
    _clock(monkeypatch, T0 + 100)
    kb.link_tasks(conn, parent, tid)
    assert 'actionability_intent' not in _event(conn, tid, 'linked')[1]  # a parked card keeps its park source
    return tid, parent


def _wake_probe(conn, tid):
    with kb.write_txn(conn):
        assert kwc._wake(conn, tid, kwc.CONDITION_MET_EVENT, {'wake_check': None, 'output': 'done'})
    return kwc.CONDITION_MET_EVENT


def _wake_timer(conn, tid):
    assert kc.wake_due_continuations(conn, now=5_000_000_000) == [tid]
    return kc.CONTINUATION_WOKEN_EVENT


@pytest.mark.parametrize('wake', [_wake_probe, _wake_timer])
def test_wake_into_todo_freezes_a_deadline(tmp_path, monkeypatch, wake):
    with connect(tmp_path / 'kanban.db') as conn:
        tid, parent = _parked_with_late_parent(conn, monkeypatch)
        _clock(monkeypatch, T0 + 500)
        kind = wake(conn, tid)
        assert kb.get_task(conn, tid).status == 'todo'
        woke, data = _event(conn, tid, kind)
        intent = data['actionability_intent']
        assert intent['status'] == 'todo' and intent['source_kind'] == kind
        assert intent['deadline_at'] == T0 + 500 + 86400 and intent['invalid_reason'] is None
        assert intent['action'] == 'satisfy_dependency' and intent['execution_authority'] is False
        assert data['actionability_parent_ids'] == [parent]
        for row in _rows(conn, tid, T0 + 600):
            assert row['source_refs'] == [_ref(woke)]
            assert row['action'] == 'satisfy_dependency' and row['blocker']
            assert row['escalation']['at'] == T0 + 500 + 86400 and row['escalation']['state'] == 'pending'
            assert row['violations'] == []
        for row in _rows(conn, tid, T0 + 500 + 86400):
            assert row['violations'] == ['escalation_overdue']


def test_wake_into_ready_stays_unstamped(tmp_path, monkeypatch):
    with connect(tmp_path / 'kanban.db') as conn:
        _clock(monkeypatch, T0)
        tid = kb.create_task(conn, title='parked card', assignee='default')
        assert kb.schedule_task(conn, tid, reason='SCHEDULED_UNTIL=2099-01-01T00:00:00Z release window', awaiting=CLOCK,
                                clock_only=True)
        kind = _wake_probe(conn, tid)
        assert kb.get_task(conn, tid).status == 'ready'
        assert 'actionability_intent' not in _event(conn, tid, kind)[1]
        for row in _rows(conn, tid, T0 + 10):
            assert row['violations'] == [] and row['escalation']['state'] == 'not_required'


def test_tampered_or_unstamped_landing_is_never_a_deadline(tmp_path, monkeypatch):
    with connect(tmp_path / 'kanban.db') as conn:
        tid, _parent = _parked_with_late_parent(conn, monkeypatch)
        _clock(monkeypatch, T0 + 500)
        kind = _wake_probe(conn, tid)
        woke, data = _event(conn, tid, kind)
        data['actionability_intent']['deadline_at'] += 86400  # a renewed clock is not the frozen one
        conn.execute('UPDATE task_events SET payload=? WHERE id=?', (json.dumps(data), woke['id']))
        for row in _rows(conn, tid, T0 + 600):
            assert row['escalation']['at'] is None
            assert 'creation_intent_invalid' in row['violations']
        del data['actionability_intent']  # a wake written before this stamp existed
        conn.execute('UPDATE task_events SET payload=? WHERE id=?', (json.dumps(data), woke['id']))
        for row in _rows(conn, tid, T0 + 600):
            assert row['source_refs'] == [_ref(_event(conn, tid, 'linked')[0])]
            assert row['violations'] == ['escalation_deadline_missing']


def test_todo_unblock_is_not_the_source_of_the_ready_card_it_later_becomes(board, monkeypatch):
    """Observed: unblocked into todo (stamped), parent done, promoted; the ready row read the todo intent."""
    conn = board
    _clock(monkeypatch, T0)
    parent = kb.create_task(conn, title='prerequisite', assignee='default')
    tid = kb.create_task(conn, title='child', assignee='default')
    assert kb.block_task(conn, tid, reason='needs the prerequisite first')
    kb.link_tasks(conn, parent, tid)
    _clock(monkeypatch, T0 + 100)
    assert kb.unblock_task(conn, tid)
    unblocked, data = _event(conn, tid, 'unblocked')
    assert kb.get_task(conn, tid).status == 'todo'
    assert data['actionability_intent']['deadline_at'] == T0 + 100 + 86400
    for row in _rows(conn, tid, T0 + 200):  # still the todo row's own source
        assert row['source_refs'] == [_ref(unblocked)] and row['escalation']['at'] == T0 + 100 + 86400
    _clock(monkeypatch, T0 + 300)
    assert kb.complete_task(conn, parent, summary='prerequisite delivered')
    kb.recompute_ready(conn)
    assert kb.get_task(conn, tid).status == 'ready'
    for row in _rows(conn, tid, T0 + 400):
        assert row['source_refs'] == [_ref(_event(conn, tid, 'created')[0])]
        assert row['escalation']['at'] is None  # the todo wait's deadline is not this ready card's
        assert 'creation_intent_invalid' not in row['violations']


def test_unblock_to_ready_is_still_a_ready_source(tmp_path, monkeypatch):
    with connect(tmp_path / 'kanban.db') as conn:
        _clock(monkeypatch, T0)
        tid = kb.create_task(conn, title='card', assignee='default')
        assert kb.block_task(conn, tid, reason='waiting on an answer')
        _clock(monkeypatch, T0 + 100)
        assert kb.unblock_task(conn, tid)
        unblocked, _ = _event(conn, tid, 'unblocked')
        for row in _rows(conn, tid, T0 + 200):
            assert row['source_refs'] == [_ref(unblocked)] and row['violations'] == []


def test_lane_admission_into_todo_freezes_a_deadline(tmp_path, monkeypatch):
    """Observed: a gate-held card merged under an open owner (linked in triage), then admitted into todo."""
    from hermes_cli import kanban_lanes as lanes
    with connect(tmp_path / 'kanban.db') as conn:
        _clock(monkeypatch, T0)
        parent = kb.create_task(conn, title='open owner', assignee='default')
        tid = kb.create_task(conn, title='gated card', assignee='default')
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='triage', block_kind='lane_gate' WHERE id=?", (tid,))
        kb.link_tasks(conn, parent, tid)
        assert 'actionability_intent' not in _event(conn, tid, 'linked')[1]
        _clock(monkeypatch, T0 + 500)
        assert lanes.admit(conn, tid, actor='lane-planner', reason='deadline passed')
        assert kb.get_task(conn, tid).status == 'todo'
        admitted, data = _event(conn, tid, 'lane_admitted')
        assert data['status'] == 'todo' and data['actionability_intent']['deadline_at'] == T0 + 500 + 86400
        for row in _rows(conn, tid, T0 + 600):
            assert row['source_refs'] == [_ref(admitted)]
            assert row['action'] == 'satisfy_dependency' and row['violations'] == []
            assert row['escalation']['at'] == T0 + 500 + 86400
        free = kb.create_task(conn, title='free gated card', assignee='default')
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='triage', block_kind='lane_gate' WHERE id=?", (free,))
        assert lanes.admit(conn, free, actor='lane-planner', reason='capacity')
        assert kb.get_task(conn, free).status == 'ready'
        assert 'actionability_intent' not in _event(conn, free, 'lane_admitted')[1]
