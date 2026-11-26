"""Uncaptured decisions are not legacy packets and cannot be rebound. [daveey]"""
import json
import sqlite3

import pytest

from hermes_cli import kanban_db_connect as dbc
from hermes_cli import kanban_home_command as home
from hermes_cli import kanban_spend_review_request as journal
from tests.hermes_cli import test_home_review_composed_delivery as cases

board = cases.board


@pytest.mark.parametrize('phase', ['prepared', 'pending'])
@pytest.mark.parametrize('replacement_kind', ['same', 'different'])
def test_deferred_is_invisible_to_legacy_sender_and_binds_once(board, phase, replacement_kind):
    source = cases.support.card(board, 'triage')
    target = cases.support.card(board, 'ready')
    cases.support.freeze(board, target, phase)
    wire = {'task': source, 'episode': 'deferred-episode', 'event_uid': 'original-decision-0001',
            'verdict': 'wait_on', 'target': target, 'reason': 'wait on existing prerequisite'}
    retained = journal.freeze(board, 'board', 'worker-a3', wire)
    assert retained['capture_pending'] is True
    # The pre-upgrade get() reads only this table; no unbound envelope is there.
    assert board.execute('SELECT request FROM spend_review_requests WHERE task=? AND episode=?',
                         (source, wire['episode'])).fetchone() is None
    assert journal.open_keys(board) == [(source, wire['episode'])]
    with pytest.raises(home.Pending):
        home.outbound(retained, wire)
    replacement = {k: v for k, v in retained.items() if k != 'capture_pending'}
    if replacement_kind == 'different':
        replacement['wire'] = {**wire, 'event_uid': 'replacement-must-not-send'}
    with pytest.raises(sqlite3.IntegrityError, match='deferred decision identity'):
        with dbc.write_txn(board):
            board.execute('INSERT INTO spend_review_requests(task,episode,request) VALUES (?,?,?)',
                          (source, wire['episode'], json.dumps(replacement)))
    assert journal.get(board, source, wire['episode']) == retained
    cases.unfreeze(board, target, phase)
    captured = journal.capture_deferred(board, source, wire['episode'])
    assert captured['wire'] == wire and 'capture_pending' not in captured
    assert journal.capture_deferred(board, source, wire['episode']) == captured
    with dbc.write_txn(board):
        board.execute('UPDATE kanban_home_generations1 SET nonce=? WHERE task_id=?',
                      ('later-epoch-not-for-original', target))
    assert journal.capture_deferred(board, source, wire['episode']) == captured
    journal.close(board, [(source, wire['episode'])])
    assert journal.open_keys(board) == []
    assert journal.get(board, source, wire['episode']) == captured


def test_selection_to_capture_race_does_not_charge_started_attempt(board, monkeypatch):
    from hermes_cli import kanban_spend_reviewer as reviewer
    from hermes_cli import kanban_wake_check as wake
    source = cases.support.card(board, 'triage')
    target = cases.support.card(board, 'ready')
    cases.support.freeze(board, target, 'pending')
    wire = {'task': source, 'episode': 'raced-episode', 'event_uid': 'raced-decision-0001',
            'verdict': 'wait_on', 'target': target, 'reason': 'wait on prerequisite'}
    retained = journal.freeze(board, 'board', 'worker-a3', wire)
    before = list(board.execute('SELECT * FROM task_events WHERE task_id=?', (source,)))
    monkeypatch.setattr(reviewer, 'settings', lambda: (True, 2))
    monkeypatch.setattr(reviewer, 'queue_backlog', lambda *a, **k: [])
    monkeypatch.setattr(wake, 'foreign_ids', lambda c: set())
    monkeypatch.setattr(reviewer, '_due', lambda *a: [(source, wire['episode'], journal.MAX_RECONCILE_ATTEMPTS)])
    # Selection saw clear; the real writer still sees the pending target.
    monkeypatch.setattr(journal, 'waiting_for_custody', lambda *a: False)
    for _ in range(5):
        assert reviewer.start_due(board, now=10**10) == []
    assert list(board.execute('SELECT * FROM task_events WHERE task_id=?', (source,))) == before
    assert journal.get(board, source, wire['episode']) == retained

