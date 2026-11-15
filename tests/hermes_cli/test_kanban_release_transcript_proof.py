"""Retained source tool receipts can prove a delayed legacy reference. [daveey]"""
import json
import sqlite3

import pytest

from hermes_constants import get_hermes_home
from hermes_cli import kanban_db_connect as kbc
from tests.hermes_cli.test_kanban_release_imported_comments import enable_sync, imported
from tests.hermes_cli.test_kanban_release_unread_comments import board as _board, release

board = _board


def delayed(tid):
    """A legacy imported comment whose event lands 13 s after its row, no UID."""
    enable_sync(tid)
    cid = imported(tid, 900001, portable=False)
    with kbc.connect_closing() as conn:
        row = dict(conn.execute("SELECT * FROM task_comments WHERE id=?", (cid,)).fetchone())
        payload = dict(comment_id=900001, writer_session='peer-session', author='default', len=len(row['body']))
        conn.execute("UPDATE task_events SET created_at=?,payload=? WHERE uid='remote-event-one'",
                     (row['created_at'] + 13, json.dumps(payload)))
        conn.commit()
    return cid, row


def retain(messages):
    with sqlite3.connect(get_hermes_home() / 'state.db') as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS messages(session_id TEXT,role TEXT,tool_call_id TEXT,'
                     'tool_calls TEXT,content TEXT,timestamp REAL)')
        conn.executemany('INSERT INTO messages VALUES(?,?,?,?,?,?)', [('peer-session',) + m for m in messages])


def call(cid, tid, body):
    return dict(id=cid, function=dict(name='kanban_comment', arguments=json.dumps(dict(task_id=tid, body=body))))


def receipt(tid, start, *calls):
    out = [('assistant', None, json.dumps([call(*c) for c in calls]), None, start + .1)]
    for i, c in enumerate(calls):
        result = json.dumps(dict(ok=True, task_id=tid, comment_id=900001))
        out.append(('tool', c[0], None, result, start + 13.8 + i / 10))
    return out


def test_delayed_source_receipt_keeps_local_watermark(board, capsys):
    cid, row = delayed(board)
    retain(receipt(board, row['created_at'], ('call-1', board, row['body'])))
    ok, output = release(board, 'promote', capsys, cid - 1)
    assert not ok and row['body'] in output and 'data unavailable' not in output
    ok, output = release(board, 'promote', capsys, cid)
    assert ok, output


@pytest.mark.parametrize('shape', ['duplicate-id-other-task', 'conflicting-receipt', 'absent'])
@pytest.mark.parametrize('entry', ['promote', 'unblock', 'tool'])
def test_unproven_receipt_cannot_be_acknowledged_by_high_watermark(board, shape, entry, capsys):
    cid, row = delayed(board)
    start = row['created_at']
    if shape == 'duplicate-id-other-task':
        retain(receipt(board, start, ('call-1', board, row['body']), ('call-1', 'other-task', row['body']))[:2])
    elif shape == 'conflicting-receipt':
        retain(receipt(board, start, ('call-1', board, row['body']), ('call-2', board, 'conflicting absent body')))
    ok, output = release(board, entry, capsys, cid + 1000)
    assert not ok and 'data unavailable' in output, output
