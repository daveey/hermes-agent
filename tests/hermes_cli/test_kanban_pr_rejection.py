"""Rejected queue jobs must return an open PR to its worker."""
import json
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from tests.hermes_cli.test_kanban_park_invariants import kanban_home
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_dispatch_pr_state as prs

_REAL_LOOKUP = prs._gh_pr_state
URL = "https://github.com/example/project/pull/12"


def marker(state, done=True, exit=2):
    return 'bb land result: new failing tests\n<!-- bb-land ' + json.dumps(
        {"done": done, "state": state, "exit": exit}) + ' -->'


def lookup(monkeypatch, state, bodies):
    monkeypatch.setattr(prs, '_gh_pr_state', _REAL_LOOKUP)
    prs.clear_cache()
    def run(cmd, **kwargs):
        return SimpleNamespace(returncode=0, stderr='', stdout=json.dumps({
            'state': state, 'comments': [{'body': b} for b in bodies]}))
    monkeypatch.setattr(prs.subprocess, 'run', run)


def test_rejected_open_pr_spawns_with_reason(kanban_home, monkeypatch, all_assignees_spawnable):
    lookup(monkeypatch, 'OPEN', [marker('queued', False, 0), marker('rejected')])
    spawned = []
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='queue repair', assignee='a')
        kb.add_comment(conn, tid, 'a', f'opened {URL}')
        assert kbd.check_respawn_guard(conn, tid) is None
        assert kbd.check_respawn_guard(conn, tid) is None
        comments = conn.execute('SELECT body FROM task_comments WHERE task_id = ?', (tid,)).fetchall()
        notes = [r[0] for r in comments if 'new failing tests' in r[0]]
        assert len(notes) == 1
        assert URL in notes[0]
        result = kbd.dispatch_once(conn, spawn_fn=lambda task, *a, **k: spawned.append(task.id) or 1)
        assert tid not in dict(result.respawn_guarded)
        assert spawned == [tid]


@pytest.mark.parametrize('state,bodies,expected', [
    ('OPEN', [marker('rejected'), marker('queued', False, 0)], 'active_pr'),
    ('OPEN', [marker('rejected', False)], 'active_pr'),
    ('OPEN', [marker('failed', True, 2)], None),
    ('OPEN', [marker('rejected'), '<!-- bb-land broken -->'], 'active_pr'),
    ('OPEN', [marker('rejected'), '<!-- bb-land truncated'], 'active_pr'),
    ('OPEN', [marker('rejected'), 'ordinary review comment'], None),
    ('MERGED', [marker('rejected')], None),
])
def test_newest_queue_result(kanban_home, monkeypatch, state, bodies, expected):
    lookup(monkeypatch, state, bodies)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='queue state', assignee='a')
        kb.add_comment(conn, tid, 'a', f'opened {URL}')
        assert kbd.check_respawn_guard(conn, tid) == expected
        if state == 'MERGED':
            assert conn.execute('SELECT COUNT(*) FROM task_comments WHERE task_id = ?', (tid,)).fetchone()[0] == 1


def test_requeue_after_rejection_is_not_cached(kanban_home, monkeypatch):
    bodies = [marker('rejected')]
    lookup(monkeypatch, 'OPEN', bodies)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title='resubmitted', assignee='a')
        kb.add_comment(conn, tid, 'a', f'opened {URL}')
        assert kbd.check_respawn_guard(conn, tid) is None
        bodies.append(marker('queued', False, 0))
        assert kbd.check_respawn_guard(conn, tid) == 'active_pr'
