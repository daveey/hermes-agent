"""Goal cards closed before a board's first judge receipt, and archived cards, never wait forever. [daveey]

A goal card completed before its board ever recorded a ``goal_verified`` receipt could not have one, so the owner
attests it on basis ``legacy_completed``. Receipt-era goal cards without a receipt stay unverified. An archived card
that was never completed produces nothing: the origin shows it closed, not waiting and not completed.
"""
import json

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_origin_ledger as ledger
from hermes_cli.kanban_goal_receipt import GoalVerification
from tests.hermes_cli.test_kanban_origin_creation import board as board  # noqa: PLC0414 -- pytest fixture registration
from tests.hermes_cli.test_kanban_origin_evidence import create_bound, publish_observed
from tests.hermes_cli.test_kanban_origin_ledger import world as world, accept  # noqa: PLC0414 -- pytest fixture registration


def goal_card(conn, title='Build'):
    tid = kb.create_task(conn, title=title, assignee='default', goal_mode=True, completion_contract='local-only')
    claim(conn, tid)
    return tid


def claim(conn, tid):
    """Start a real run, so completion closes it like a dispatched worker's would."""
    assert kb.claim_task(conn, tid) is not None


def complete(conn, tid, *, receipt=False):
    proof = GoalVerification.for_handoff(kb.get_task(conn, tid), 'Tested') if receipt else None
    assert kb.complete_task(conn, tid, summary='Tested', goal_verification=proof)


def open_epoch(conn):
    """The board's first judge receipt, one second after every earlier completion."""
    tid = goal_card(conn, 'Receipt era starts')
    complete(conn, tid, receipt=True)
    conn.execute("UPDATE task_events SET created_at=created_at+1 WHERE kind='goal_verified'")
    for column, table in (('created_at', 'task_events'), ('completed_at', 'tasks'), ('ended_at', 'task_runs')):
        key = 'task_id' if table != 'tasks' else 'id'
        conn.execute(f'UPDATE {table} SET {column}={column}+1 WHERE {key}=?', (tid,))
    conn.commit()
    return tid


def observe(conn, tid):
    conn.execute('BEGIN')
    try:
        status = conn.execute('SELECT status FROM tasks WHERE id=?', (tid,)).fetchone()[0]
        return ledger.observe_owner_task(conn, {'id': tid, 'status': status}, now=1000)['origin_observation']
    finally:
        conn.rollback()


def drop_completed_event(conn, tid):
    """History lost after completion (a recovered board): only the closing run remains."""
    conn.execute("DELETE FROM task_events WHERE task_id=? AND kind='completed'", (tid,))
    conn.commit()


def test_goal_card_completed_before_the_first_receipt_is_owner_verified(board):
    with kbc.connect_closing() as conn:
        legacy = goal_card(conn)
        complete(conn, legacy)
        open_epoch(conn)
        evidence = observe(conn, legacy)
    assert evidence['verified'] is True
    assert evidence['basis'] == 'legacy_completed'


def test_goal_card_without_receipt_after_the_epoch_stays_unverified(board):
    with kbc.connect_closing() as conn:
        open_epoch(conn)
        late = goal_card(conn)
        complete(conn, late)
        evidence = observe(conn, late)
    assert evidence['verified'] is False
    assert 'basis' not in evidence


def test_board_without_any_receipt_gets_no_legacy_pass(board):
    with kbc.connect_closing() as conn:
        legacy = goal_card(conn)
        complete(conn, legacy)
        evidence = observe(conn, legacy)
    assert evidence['verified'] is False


def test_receipt_verified_card_keeps_the_lifecycle_basis(board):
    with kbc.connect_closing() as conn:
        tid = open_epoch(conn)
        evidence = observe(conn, tid)
    assert evidence['verified'] is True
    assert 'basis' not in evidence  # unchanged observations publish identical records


@pytest.mark.parametrize('summary', ['x' * 750, 'Delivered.\n\nDetails line two', '  padded summary  '])
def test_receipt_binds_the_full_handoff_not_the_events_first_line(board, summary):
    """The completed event keeps only the summary's first line (400 chars); the receipt binds the whole handoff."""
    with kbc.connect_closing() as conn:
        tid = goal_card(conn)
        proof = GoalVerification.for_handoff(kb.get_task(conn, tid), summary.strip())
        assert kb.complete_task(conn, tid, summary=summary, goal_verification=proof)
        assert observe(conn, tid)['verified'] is True
        # A closing run whose handoff no longer matches the judged evidence is not proof.
        conn.execute("UPDATE task_runs SET summary='different' WHERE task_id=?", (tid,))
        conn.commit()
        assert observe(conn, tid)['verified'] is (summary.strip() == kb._first_line(summary, 400))


def test_legacy_card_whose_completed_event_was_lost_is_verified_by_its_closing_run(board):
    with kbc.connect_closing() as conn:
        goal, plain = goal_card(conn), kb.create_task(conn, title='Plain', assignee='default',
                                                      completion_contract='local-only')
        claim(conn, plain)
        complete(conn, goal)
        complete(conn, plain)
        open_epoch(conn)
        drop_completed_event(conn, goal)
        drop_completed_event(conn, plain)
        assert observe(conn, goal) == {**observe(conn, goal), 'verified': True, 'basis': 'legacy_completed'}
        assert observe(conn, plain)['verified'] is True


@pytest.mark.parametrize('fault', ['after_epoch', 'run_not_completed', 'no_epoch', 'pr_contract'])
def test_lost_completed_event_needs_every_other_proof(board, fault):
    with kbc.connect_closing() as conn:
        if fault == 'after_epoch':
            open_epoch(conn)
        tid = goal_card(conn)
        complete(conn, tid)
        if fault not in ('after_epoch', 'no_epoch'):
            open_epoch(conn)
        else:  # completed after the first receipt (open_epoch moved it a second ahead)
            conn.execute('UPDATE tasks SET completed_at=completed_at+2 WHERE id=?', (tid,))
            conn.execute('UPDATE task_runs SET ended_at=ended_at+2 WHERE task_id=?', (tid,))
        if fault == 'run_not_completed':
            conn.execute("UPDATE task_runs SET outcome='crashed' WHERE task_id=?", (tid,))
        if fault == 'pr_contract':
            conn.execute("UPDATE tasks SET completion_contract='https://github.com/o/r/pull/1' WHERE id=?", (tid,))
        drop_completed_event(conn, tid)
        evidence = observe(conn, tid)
    assert evidence['verified'] is False, fault


def test_legacy_goal_card_releases_its_origin_obligation(world, board):
    path, replica, binding = world
    with kbc.connect_closing() as conn:
        tid = create_bound(conn, binding, goal_mode=True)
        claim(conn, tid)
        publish_observed(conn, replica, binding, version=3)
        accept(world)
        complete(conn, tid)
        open_epoch(conn)
        publish_observed(conn, replica, binding, version=4)
    row = ledger.project(path, replica=replica, gateway='origin', profile='default', now=1001)[0]
    assert (row['status'], row['unfinished']) == ('done', False)


@pytest.mark.parametrize('history', ['blocked', 'completed_unverified'])
def test_archived_without_verified_completion_is_closed_not_waiting_nor_completed(world, board, history):
    path, replica, binding = world
    with kbc.connect_closing() as conn:
        tid = create_bound(conn, binding, goal_mode=history == 'completed_unverified')
        publish_observed(conn, replica, binding, version=3)
        accept(world)
        if history == 'blocked':
            assert kb.block_task(conn, tid, reason='needs a decision')
        else:
            complete(conn, tid)
        assert kb.archive_task(conn, tid)
        publish_observed(conn, replica, binding, version=4)
    row = ledger.project(path, replica=replica, gateway='origin', profile='default', now=1001)[0]
    assert row['reported_status'] == 'archived'
    assert row['status'] == 'archived'
    assert row['unfinished'] is False
    assert row['closed_uncompleted'] is True
    # Never completion: the ledger obligation itself stays unreleased.
    assert ledger.obligations(path, gateway='origin', profile='default', sessions=['s_current'])[0]['unfinished']


def test_archived_card_from_an_offline_owner_still_waits(world, board):
    path, replica, binding = world
    with kbc.connect_closing() as conn:
        tid = create_bound(conn, binding)
        publish_observed(conn, replica, binding, version=3)
        accept(world)
        assert kb.archive_task(conn, tid)
        publish_observed(conn, replica, binding, version=4)
    row = ledger.project(path, replica=replica, gateway='origin', profile='default', now=2000)[0]
    assert (row['status'], row['unfinished']) == ('unknown', True)
    assert 'closed_uncompleted' not in row


def test_archived_replica_card_is_closed_in_the_link_reader(tmp_path, monkeypatch):
    from hermes_cli import profiles
    from hermes_cli import kanban_session_activity as activity
    from tests.hermes_cli.test_kanban_remote_terminal_release import NOW, card, observed, replica
    monkeypatch.setattr(profiles, 'get_active_profile_name', lambda: 'default')
    record = observed(card('archived', origin_bindings=[{'b': 1}]))
    rows = activity._read_remote_links(replica(tmp_path, [('default/default/t_1', record)]), now=NOW)
    assert [(r['status'], r['unfinished'], r.get('closed_uncompleted')) for r in rows] == [('archived', False, True)]
    assert [r for r in rows if activity._replica_bucket(r) is not None] == []
    json.dumps(rows)
