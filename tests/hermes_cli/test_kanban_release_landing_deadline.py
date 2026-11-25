"""Canonical release landing provenance, not a guess from replica status. [daveey]"""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_spend_reviewer as reviewer
from hermes_cli.kanban_actionability_sources import describe
from tests.hermes_cli import test_kanban_spend_review_landing as support

kanban_home = support.kanban_home
board = support.board
clock = support.clock


def _receipt(board, landing):
    def mutate(row):
        row['payload'] = json.dumps({**json.loads(row['payload']), 'landing': landing})
    board[0].mutate = mutate


def test_release_landing_freezes_before_parent_and_status_projection(kanban_home, spend_reviewer, board, clock):
    with kbc.connect_closing() as conn:
        tid, parent, ep, verdict = support._reviewed(conn, {
            'decision': 'release', 'reason': 'Finish acceptance', 'remaining_quote': support.support.QUOTE})
        _receipt(board, {'status': 'todo', 'parent_ids': [parent]})
        spend_reviewer.append(verdict)
        at = conn.execute('SELECT MAX(created_at) FROM task_events').fetchone()[0]
        with clock(at + 4):
            assert reviewer.review(conn, tid, ep) == 'settled'
        assert kb.get_task(conn, tid).status == 'triage'
        assert not conn.execute('SELECT 1 FROM task_links WHERE child_id=?', (tid,)).fetchone()
        event, data = support._settled(conn, tid)
        deadline = event['created_at'] + support.DAY
        assert data['actionability_intent']['deadline_at'] == deadline
        assert data['actionability_parent_ids'] == [parent]
        assert describe(conn, tid)['action'] == 'decide_scope'  # a receipt alone never clears the stop
        # Project the Board's canonical renewal/comment/promotion rows, not a local release.
        from hermes_cli.kanban_scope_renewal import digest
        mandate = 'Canonical release mandate for the fixture'
        with kb.write_txn(conn):
            comment_id = conn.execute('INSERT INTO task_comments (task_id,author,body,created_at) VALUES (?,?,?,?)',
                                     (tid, reviewer.AUTHOR, mandate, event['created_at'] - 1)).lastrowid
            conn.execute('INSERT INTO task_events (task_id,kind,payload,created_at) VALUES (?,?,?,?)',
                         (tid, 'scope_renewed', json.dumps({'version': 1, 'episode': ep,
                          'covered_restriction_uids': [ep], 'mandate_comment_uid': str(comment_id),
                          'mandate_body_sha256': digest(mandate)}), event['created_at'] - 1))
            conn.execute('INSERT INTO task_events (task_id,kind,payload,created_at) VALUES (?,?,?,?)',
                         (tid, 'promoted_manual', json.dumps({'actor': reviewer.AUTHOR}), event['created_at'] - 1))
            conn.execute('INSERT INTO task_links (parent_id,child_id) VALUES (?,?)', (parent, tid))
            conn.execute("UPDATE tasks SET status='todo' WHERE id=?", (tid,))
        for now in (event['created_at'] + 1, deadline + 1):
            row = describe(conn, tid, now=now)
            assert row['action'] == 'satisfy_dependency'
            assert parent in row['blocker']['prerequisite']
            assert row['escalation']['at'] == deadline
            assert support.MISSING not in row['violations']
        assert 'escalation_overdue' in row['violations']
        # The canonical landing and copied edge snapshot must agree, not merely each look valid.
        from hermes_cli.kanban_actionability_creation import validated_intent
        task = dict(conn.execute('SELECT * FROM tasks WHERE id=?', (tid,)).fetchone())
        forged = {**data, 'landing': {'status': 'todo', 'parent_ids': ['t_other']}}
        assert validated_intent(task, {'kind': reviewer.SETTLED, 'created_at': event['created_at'],
                                      'data': forged}) is None
        # No borrowing after the card leaves todo, or when its parents disagree.
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        assert describe(conn, tid, now=deadline + 2)['escalation']['at'] != deadline
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='todo' WHERE id=?", (tid,))
            conn.execute('DELETE FROM task_links WHERE child_id=?', (tid,))
        assert describe(conn, tid, now=deadline + 2)['escalation']['at'] != deadline


@pytest.mark.parametrize('landing', [None, {}, {'status': 'ready', 'parent_ids': ['P']},
    {'status': 'todo', 'parent_ids': []}, {'status': 'todo', 'parent_ids': ['P', 'P']},
    {'status': 'todo', 'parent_ids': ['SELF']}, {'status': 'todo', 'parent_ids': [7]},
    {'status': 'todo', 'parent_ids': ['P'], 'extra': True}])
def test_release_without_valid_todo_receipt_never_infers_landing(kanban_home, spend_reviewer, board, landing):
    with kbc.connect_closing() as conn:
        tid, parent, ep, verdict = support._reviewed(conn, {
            'decision': 'release', 'reason': 'Finish acceptance', 'remaining_quote': support.support.QUOTE})
        if isinstance(landing, dict):
            landing = {**landing, 'parent_ids': [({'P': parent, 'SELF': tid}.get(p, p))
                       for p in landing.get('parent_ids', [])]}
        _receipt(board, landing)
        spend_reviewer.append(verdict)
        assert reviewer.review(conn, tid, ep) == 'settled'
        data = support._settled(conn, tid)[1]
        assert 'actionability_intent' not in data
        assert kb.get_task(conn, tid).status == 'triage'
