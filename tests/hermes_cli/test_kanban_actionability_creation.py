"""New lifecycle sources persist administrative deadlines, never release authority. [daveey]"""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect
from hermes_cli.kanban_actionability_sources import describe
from hermes_cli.kanban_actionability_creation import stamp


@pytest.mark.parametrize('wake,reason', [
    (None, 'wake_invalid'), (True, 'wake_invalid'), ('1800001000', 'wake_invalid'),
    (float('inf'), 'wake_invalid'), (float('nan'), 'wake_invalid'),
    (1_799_999_400, 'deadline_not_future'), (1_799_999_399, 'deadline_not_future'),
])
def test_invalid_source_never_fabricates_deadline(tmp_path, wake, reason):
    with connect(tmp_path / 'kanban.db') as conn:
        tid = kb.create_task(conn, title='invalid new park', assignee='default')
        conn.execute("UPDATE tasks SET status='scheduled' WHERE id=?", (tid,))
        data = stamp(conn, tid, 'scheduled', {'wake_at': wake, 'awaiting': {'kind': 'clock'}}, now=1_800_000_000)
        intent = data['actionability_intent']
        assert intent['invalid_reason'] == reason
        assert intent['deadline_at'] is None
        assert intent['execution_authority'] is False


@pytest.mark.parametrize('assignee,awaiting,reason', [
    ('', {'kind': 'clock'}, 'owner_missing'),
    ('default', None, 'trigger_missing'),
    ('default', {'kind': 'ci'}, 'producer_malformed'),
    ('default', ['clock'], 'producer_malformed'),
])
def test_incomplete_source_stays_invalid(tmp_path, assignee, awaiting, reason):
    with connect(tmp_path / 'kanban.db') as conn:
        tid = kb.create_task(conn, title='incomplete park', assignee='default')
        conn.execute("UPDATE tasks SET status='scheduled',assignee=? WHERE id=?", (assignee, tid))
        data = stamp(conn, tid, 'scheduled', {'wake_at': 1_800_000_100, 'awaiting': awaiting}, now=1_800_000_000)
        assert data['actionability_intent']['invalid_reason'] == reason
        assert data['actionability_intent']['deadline_at'] is None


@pytest.mark.parametrize('field,value', [
    ('version', True), ('version', 2), ('policy', 'unknown'), ('execution_authority', True),
    ('first_observed', True), ('deadline_at', 1_900_000_000), ('action', 'execute_current_scope'),
    ('instruction', 'replacement'), ('owner', {'kind': 'assignee', 'refs': ['foreign']}),
    ('blocker', {'prerequisite': 'invented', 'trigger': 'invented', 'evidence': []}),
])
def test_reader_rejects_corrupt_intent(tmp_path, monkeypatch, field, value):
    monkeypatch.setattr('time.time', lambda: 1_800_000_000)
    with connect(tmp_path / 'kanban.db') as conn:
        tid = kb.create_task(conn, title='human wait', assignee='default')
        request = {'action': 'Reply A or B. Unblock = build; archive = drop.',
                   'why_human': 'Only owner chooses.', 'interaction': 'reply',
                   'scope': 'fixture', 'evidence': 'Equivalent options'}
        assert kb.block_task(conn, tid, kind='needs_input', reason='Choose', human_request=request)
        source = conn.execute("SELECT * FROM task_events WHERE task_id=? AND kind='blocked'", (tid,)).fetchone()
        payload = json.loads(source['payload'])
        payload['actionability_intent'][field] = value
        conn.execute('UPDATE task_events SET payload=? WHERE id=?', (json.dumps(payload), source['id']))
        row = describe(conn, tid)
        assert row['escalation']['at'] is None
        assert row['evidence_state'] == 'unknown'
        assert 'creation_intent_invalid' in row['violations']


def test_delayed_continue_is_supported_but_immediate_is_not(tmp_path):
    with connect(tmp_path / 'kanban.db') as conn:
        tid = kb.create_task(conn, title='continuation stamp', assignee='default')
        conn.execute("UPDATE tasks SET status='scheduled' WHERE id=?", (tid,))
        data = {'status': 'scheduled', 'wake_at': 1_800_000_100, 'awaiting': {'kind': 'clock'}}
        intent = stamp(conn, tid, 'continued', data, now=1_800_000_000)['actionability_intent']
        assert intent['deadline_at'] == 1_800_000_700
        assert intent['policy'] == 'scheduled-wake-grace-v1'
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        immediate = {'status': 'ready'}
        assert stamp(conn, tid, 'continued', immediate, now=1_800_000_000) == immediate


def test_new_dependency_sources_freeze_admin_deadline(tmp_path, monkeypatch):
    monkeypatch.setattr('time.time', lambda: 1_800_000_000)
    with connect(tmp_path / 'kanban.db') as conn:
        parent = kb.create_task(conn, title='dependency owner', assignee='default')
        tid = kb.create_task(conn, title='new dependency wait', assignee='default', parents=[parent])
        source = conn.execute("SELECT * FROM task_events WHERE task_id=? AND kind='dependency_wait'", (tid,)).fetchone()
        payload = json.loads(source['payload'])
        intent = payload['actionability_intent']
        assert intent['policy'] == 'blocked-admin-review-v1'
        assert intent['deadline_at'] == source['created_at'] + 86400
        assert parent in intent['blocker']['trigger']
        assert describe(conn, tid)['escalation']['at'] == intent['deadline_at']
        assert kb.get_task(conn, tid).status == 'todo'
        tid = kb.create_task(conn, title='ready task acquires a dependency', assignee='default')
        # Prepare the already-recorded graph; block_task owns the ready -> todo transition.
        kb._link(conn, parent, tid)
        assert kb.block_task(conn, tid, kind='dependency', reason='Wait for the existing dependency')
        source = conn.execute("SELECT * FROM task_events WHERE task_id=? AND kind='dependency_wait' ORDER BY id DESC", (tid,)).fetchone()
        assert json.loads(source['payload'])['actionability_intent']['deadline_at'] == 1_800_086_400


@pytest.mark.parametrize('operation', ['schedule', 'timed-block'])
def test_schedule_freezes_final_wake_policy_and_owner(tmp_path, monkeypatch, operation):
    monkeypatch.setattr('time.time', lambda: 1_800_000_000)
    conn = connect(tmp_path / 'kanban.db')
    try:
        tid = kb.create_task(conn, title='new source obligation', assignee='default')
        park = kb.schedule_task if operation == 'schedule' else kb.block_task
        extra = {} if operation == 'schedule' else {'kind': 'time'}
        assert park(conn, tid, reason='SCHEDULED_UNTIL=2027-01-15T08:01:40+00:00',
                    clock_only=True, awaiting={'kind': 'clock', 'why': 'bounded observation window'}, **extra)
        source = conn.execute("SELECT * FROM task_events WHERE task_id=? AND kind='scheduled'", (tid,)).fetchone()
        payload = json.loads(source['payload'])
        assert 'actionability_intent' in payload
        intent = payload['actionability_intent']
        assert intent['version'] == 1
        assert intent['policy'] == 'scheduled-wake-grace-v1'
        assert intent['owner'] == {'kind': 'assignee', 'refs': ['default']}
        assert intent['deadline_at'] == payload['wake_at'] + 600
        assert intent['first_observed'] == source['created_at']
        assert intent['action'] == 'wait_for_named_producer'
        assert str(payload['wake_at']) in intent['blocker']['trigger']
        assert intent['execution_authority'] is False
        before = dict(source)
        kb.add_comment(conn, tid, 'default', 'An annotation cannot renew the obligation')
        monkeypatch.setattr('time.time', lambda: 1_800_001_000)
        assert dict(conn.execute('SELECT * FROM task_events WHERE id=?', (source['id'],)).fetchone()) == before
        assert describe(conn, tid)['escalation']['at'] == intent['deadline_at']
    finally:
        conn.close()


def test_block_freezes_assignee_admin_review_not_human_release(tmp_path, monkeypatch):
    monkeypatch.setattr('time.time', lambda: 1_800_000_000)
    conn = connect(tmp_path / 'kanban.db')
    try:
        tid = kb.create_task(conn, title='new human wait', assignee='default')
        request = {'action': 'Reply A or B. Unblock = implement the choice; archive = drop scope.',
                   'why_human': 'Only the owner can choose the scope.', 'interaction': 'reply',
                   'scope': 'This isolated fixture only', 'evidence': 'Two equally supported scope options'}
        assert kb.block_task(conn, tid, kind='needs_input', reason='Choose the scope', human_request=request)
        source = conn.execute("SELECT * FROM task_events WHERE task_id=? AND kind='blocked'", (tid,)).fetchone()
        payload = json.loads(source['payload'])
        assert 'actionability_intent' in payload
        intent = payload['actionability_intent']
        assert intent['policy'] == 'blocked-admin-review-v1'
        assert intent['deadline_at'] == source['created_at'] + 86400
        assert intent['owner'] == {'kind': 'assignee', 'refs': ['default']}
        assert intent['execution_authority'] is False
        assert request['action'] in intent['blocker']['trigger']
        row = describe(conn, tid)
        assert row['escalation']['at'] == intent['deadline_at']
        assert row['action'] == 'supply_recorded_human_step'
        assert row['release']['authority'] == 'existing_lifecycle_gates'
        assert 'escalation_deadline_missing' not in row['violations']
        assert kb.get_task(conn, tid).status == 'blocked'
        assert kb.recompute_ready(conn) == 0
    finally:
        conn.close()
