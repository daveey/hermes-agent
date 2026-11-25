"""Ordinary triage dependency edges freeze an administrative clock, never release a hold."""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_actionability_sources import describe
from tests.hermes_cli import test_kanban_spend_review_landing as support

kanban_home = support.kanban_home
clock = support.clock
DAY = 86400


def _triage(conn):
    parent = kb.create_task(conn, title='prerequisite', assignee='default')
    child = kb.create_task(conn, title='ordinary failed card', assignee='default')
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='triage' WHERE id=?", (child,))
        kb._append_event(conn, child, 'failure_finalized', {'reason': 'failed'})
    return parent, child


def _last(conn, child):
    return conn.execute('SELECT * FROM task_events WHERE task_id=? ORDER BY id DESC LIMIT 1', (child,)).fetchone()


def test_triage_link_freezes_clock_without_changing_status(kanban_home):
    with kbc.connect_closing() as conn:
        parent, child = _triage(conn)
        assert not kb.link_tasks(conn, parent, child)
        event = _last(conn, child)
        data = json.loads(event['payload'])
        assert data['actionability_intent']['status'] == 'triage'
        assert data['actionability_intent']['execution_authority'] is False
        for now in (event['created_at'] + 1, event['created_at'] + DAY + 1):
            row = describe(conn, child, now=now)
            assert row['status'] == 'triage' and row['action'] == 'satisfy_dependency'
            assert parent in row['blocker']['prerequisite']
            assert row['escalation']['at'] == event['created_at'] + DAY
            assert 'escalation_deadline_missing' not in row['violations']
        assert 'escalation_overdue' in row['violations']


def test_relink_and_nonfinal_unlink_carry_original_triage_clock(kanban_home, clock):
    with kbc.connect_closing() as conn:
        parent, child = _triage(conn)
        other = kb.create_task(conn, title='second prerequisite', assignee='default')
        kb.link_tasks(conn, parent, child)
        first = _last(conn, child)
        with clock(first['created_at'] + DAY + 1):
            kb.link_tasks(conn, other, child)
        with clock(first['created_at'] + DAY + 2):
            kb.unlink_tasks(conn, parent, child)
        row = describe(conn, child, now=first['created_at'] + DAY + 3)
        assert row['escalation']['at'] == first['created_at'] + DAY
        assert 'escalation_overdue' in row['violations']
        assert other in row['blocker']['prerequisite']


@pytest.mark.parametrize('status', ['ready', 'done', 'archived'])
def test_triage_link_does_not_lend_clock_after_status_change(kanban_home, status):
    with kbc.connect_closing() as conn:
        parent, child = _triage(conn)
        kb.link_tasks(conn, parent, child)
        event = _last(conn, child)
        with kb.write_txn(conn):
            conn.execute('UPDATE tasks SET status=? WHERE id=?', (status, child))
        row = describe(conn, child, now=event['created_at'] + 1)
        assert row['escalation']['at'] != event['created_at'] + DAY


def test_private_stop_stays_above_triage_edge(kanban_home):
    with kbc.connect_closing() as conn:
        parent, child = _triage(conn)
        with kb.write_txn(conn):
            kb._append_event(conn, child, 'spend_budget_triaged', {'cause': 'scope_stopped', 'reason': 'stop scope'})
        before = describe(conn, child)
        kb.link_tasks(conn, parent, child)
        after = describe(conn, child)
        assert after['action'] == before['action']
        assert after['escalation']['at'] == before['escalation']['at']
        assert kb.get_task(conn, child).status == 'triage'


def test_final_unlink_ends_triage_clock_and_relink_starts_new_episode(kanban_home, clock):
    with kbc.connect_closing() as conn:
        parent, child = _triage(conn)
        kb.link_tasks(conn, parent, child)
        first = _last(conn, child)
        with clock(first['created_at'] + 10):
            kb.unlink_tasks(conn, parent, child)
        assert 'actionability_intent' not in json.loads(_last(conn, child)['payload'])
        assert describe(conn, child)['escalation']['at'] is None
        with clock(first['created_at'] + 20):
            kb.link_tasks(conn, parent, child)
        assert describe(conn, child)['escalation']['at'] == first['created_at'] + 20 + DAY


@pytest.mark.parametrize('field,value', [('assignee', 'other'), ('block_kind', 'lane_gate')])
def test_changed_triage_intent_facts_fail_closed(kanban_home, field, value):
    with kbc.connect_closing() as conn:
        parent, child = _triage(conn)
        kb.link_tasks(conn, parent, child)
        with kb.write_txn(conn):
            conn.execute(f'UPDATE tasks SET {field}=? WHERE id=?', (value, child))
        row = describe(conn, child)
        assert 'creation_intent_invalid' in row['violations']
        assert 'escalation_deadline_missing' in row['violations']
        assert row['escalation']['at'] is None
