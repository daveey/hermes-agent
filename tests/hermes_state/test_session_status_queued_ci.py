"""Queued CI is a confirmed producer, not an executing owner or an idle chat. [daveey]"""
import json
import sqlite3
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_external_state as external
from hermes_cli import kanban_session_activity as ksa
from tests.hermes_state.test_session_status_refresh import (
    add_card,
    board as board,  # noqa: PLC0414 -- pytest fixture reexport
    db as db,  # noqa: PLC0414 -- pytest fixture reexport
    receipts as receipts,  # noqa: PLC0414 -- pytest fixture reexport
    refresh,
    set_card,
    turn,
)

FIXTURE = Path(__file__).resolve().parents[2] / 'schemas/fixtures/queued-ci-sidebar.json'


@pytest.mark.parametrize('job,age,expected', [
    ('queued', 0, 'external'), ('running', 0, 'external'),
    ('queued', 500, 'waiting'), ('unknown', 0, 'waiting'),
])
def test_owner_watch_to_canonical_dot(db, board, job, age, expected):
    turn(db)
    add_card(board, 'owner', 'scheduled')
    now = time.time()
    park = {'awaiting': {'kind': 'ci', 'ref': 'https://github.com/org/repo/actions/runs/123'},
            'probe_host': 'worker-a1'}
    record = external.next_record(None, {'job': job, 'service': 'degraded',
                                        'detail': 'runner online and busy'},
                                  now=now - age, every=300, adapter='github_run', host='worker-a1')
    with sqlite3.connect(board) as conn:
        conn.execute('CREATE TABLE task_events (id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT, '
                     'payload TEXT, created_at REAL)')
        conn.execute('INSERT INTO task_events VALUES (1,?,\'scheduled\',?,?)',
                     ('owner', json.dumps(park), now - 600))
        conn.execute('INSERT INTO task_events VALUES (2,?,\'external_watch\',?,?)',
                     ('owner', json.dumps(record), now - age))
    ksa._reset_cache_for_tests()
    status = refresh(db, 'card_change')
    assert status['kind'] == 'waiting'  # never promote the owner's own executor
    assert status['presentation']['stripe'] == expected
    if job == 'queued' and age == 0:
        fixture = json.loads(FIXTURE.read_text())
        assert {key: status[key] for key in ('kind', 'on', 'presentation')} == fixture
    set_card(board, 'owner', 'done')
    assert refresh(db, 'card_change')['kind'] == 'done'
