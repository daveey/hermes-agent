"""Same-clock source order and compatibility regressions. [daveey]"""
import json

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_actionability_sources as sources
from tests.hermes_cli import test_kanban_actionability_intents as journal
from tests.hermes_cli import test_kanban_assignment_routing as assignment

live = journal.live
assignment_board = assignment.board


def test_assignment_freezes_real_supervisor_and_dependency(assignment_board):
    conn, _ = assignment_board
    tid = assignment.task_with_drift(assignment_board)
    assert kb.block_task(conn, tid, kind='assignment', reason='Worker lacks terminal tools')
    data = assignment.payload(conn, tid)
    intent = data['actionability_intent']
    assert intent['owner'] == {'kind': 'assignee', 'refs': ['supervisor']}
    assert intent['status'] == 'todo'
    assert intent['invalid_reason'] is None
    assert intent['deadline_at'] == intent['first_observed'] + 86400
    assert data['supervisor_task_id'] in data['actionability_parent_ids']
    assert kb.get_task(conn, tid).status == 'todo'
    assert not kb._parents_satisfied(conn, tid)


@pytest.mark.parametrize('human', [True, False])
def test_initial_blocked_uses_actual_blocked_event_not_created_guess(live, human):
    request = {'action': 'Choose A or B. Unblock = implement; archive = drop.',
               'why_human': 'Only owner chooses.', 'interaction': 'reply',
               'scope': 'isolated fixture', 'evidence': 'Equivalent choices'} if human else None
    tid = kb.create_task(live, title='initial blocked', assignee='default', initial_status='blocked',
                         human_request=request, lint_blocked=human)
    source = live.execute("SELECT * FROM task_events WHERE task_id=? AND kind='blocked'", (tid,)).fetchone()
    intent = json.loads(source['payload'])['actionability_intent']
    assert intent['source_kind'] == 'blocked'
    assert intent['deadline_at'] == (source['created_at'] + 86400 if human else None)
    assert intent['invalid_reason'] == (None if human else 'trigger_missing')
    assert kb.get_task(live, tid).status == 'blocked'


def test_shared_blocker_uses_final_stored_wake(live):
    tid = kb.create_task(live, title='shared outage', assignee='default')
    assert kb.block_task(live, tid, reason='External service unavailable', kind='capability', blocker_id='fixture-outage')
    source = live.execute("SELECT * FROM task_events WHERE task_id=? AND kind='scheduled'", (tid,)).fetchone()
    data = json.loads(source['payload'])
    intent = data['actionability_intent']
    assert intent['invalid_reason'] is None
    assert intent['deadline_at'] == data['wake_at'] + 600
    assert intent['wake_at'] == data['wake_at']
    assert intent['execution_authority'] is False



@pytest.mark.parametrize('created_uid,wait_uid', [('z-created', 'a-wait'), ('a-created', 'z-wait')])
def test_same_clock_dependency_uses_append_order_not_uuid_random_bits(live, monkeypatch, created_uid, wait_uid):
    monkeypatch.setattr('time.time', lambda: 1_800_000_000)
    parent = kb.create_task(live, title='parent', assignee='default')
    tid = kb.create_task(live, title='waiter', assignee='default', parents=[parent])
    with kbc.write_txn(live):
        live.execute("UPDATE task_events SET uid=? WHERE task_id=? AND kind='created'", (created_uid, tid))
        live.execute("UPDATE task_events SET uid=? WHERE task_id=? AND kind='dependency_wait'", (wait_uid, tid))
    source = live.execute("SELECT * FROM task_events WHERE task_id=? AND kind='dependency_wait'", (tid,)).fetchone()
    row = sources.describe(live, tid)
    assert row['source_refs'] == [wait_uid]
    assert row['escalation']['at'] == json.loads(source['payload'])['actionability_intent']['deadline_at']
    assert row['evidence_state'] == 'unknown'  # local append order is not canonical authority


def test_material_newer_clock_wins_over_append_order(live):
    tid = kb.create_task(live, title='source clock', assignee='default')
    with kbc.write_txn(live):
        kb._append_event(live, tid, 'created', {'status': 'ready'})
        live.execute("UPDATE task_events SET created_at=1 WHERE task_id=? AND id=(SELECT MAX(id) FROM task_events)", (tid,))
    events = sources.Reader(live).events(tid)
    assert events[0]['created_at'] > events[1]['created_at']
