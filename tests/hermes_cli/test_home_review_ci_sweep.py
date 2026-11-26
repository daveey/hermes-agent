"""Real CI sweep preserves frozen custody and wakes a healthy neighbour. [daveey]"""
import time

import pytest
from hermes_cli import kanban_db as kb, kanban_db_connect as dbc
from hermes_cli.kanban_home_transfer import frozen
from tests.hermes_cli import test_home_release_candidates as sup

board = sup.board


def _rows(conn, table, task):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
        return None
    return list(map(tuple, conn.execute(f'SELECT * FROM {table} WHERE task_id=?', (task,))))


def _snap(conn, task):
    return (tuple(conn.execute('SELECT * FROM tasks WHERE id=?', (task,)).fetchone()),
            _rows(conn, 'task_events', task), _rows(conn, 'task_wake_checks', task),
            _rows(conn, 'task_wake_episode_barriers', task), _rows(conn, 'task_wake_episodes', task))


def _ci_parked(conn, title, ref):
    """Scheduled card, timed park awaiting CI with a registered wake_check: the ordinary CI wait."""
    from hermes_cli import kanban_wake_check as kwc
    t = kb.create_task(conn, title=title, assignee='default')
    with dbc.write_txn(conn):
        conn.execute("UPDATE tasks SET status='scheduled' WHERE id=?", (t,))
        kb._append_event(conn, t, 'scheduled', {
            'reason': 'wait', 'timed_park': True, 'wake_at': int(time.time()) + 5000, 'resume_status': 'ready',
            'awaiting': {'kind': 'ci', 'ref': ref, 'why': 'checks'}, 'wake_check': 'true', 'check_every': 60})
        kwc.set_check(conn, t, 'true', 60)
    return t


@pytest.mark.parametrize('phase', ['prepared', 'pending', None])
def test_ci_sweep_frozen_card_then_healthy_neighbour(board, monkeypatch, phase):
    from hermes_cli import kanban_ci_progress as ci
    from hermes_cli.kanban_external_adapters import github as external_github
    first = _ci_parked(board, 'first (frozen in custody phases)', 'o/r/pull/1')
    neighbour = _ci_parked(board, 'healthy neighbour', 'o/r/pull/2')
    sup.freeze(board, first, phase)
    assert {w[0] for w in ci._ci_waits(board)} == {first, neighbour}
    snap = _snap(board, first)
    order = []

    def fake_progress(ref, **_kw):
        order.append(ref)
        return ci.Progress(state=ci.NOT_STARTING, repo='o/r', pr=int(ref.rsplit('/', 1)[1]), per_pr=True,
                           detail='merge conflict', remedy='rebase')
    monkeypatch.setattr(external_github, 'poll', lambda *a, **k: None)
    monkeypatch.setattr(ci, 'progress', fake_progress)
    with ci._LOCK:
        ci._LAST_CHECK.clear()
        ci._JITTER.clear()
    woken = ci.sweep(board)
    status = {t: kb.get_task(board, t).status for t in (first, neighbour)}
    same = _snap(board, first) == snap
    probe_rows = len(_rows(board, 'task_wake_checks', first) or [])
    print('RSWEEP', phase, 'order=', order, 'woken=', [('first' if t == first else 'neighbour') for t in woken],
          'status=', (status[first], status[neighbour]), 'frozen_card_bytes_same=', same, 'probe_rows=', probe_rows)
    if phase is None:
        assert sorted(woken) == sorted([first, neighbour])
        return
    assert frozen(board, first)
    assert status[first] == 'scheduled'
    assert same, 'frozen card local probe/episode custody changed'
    assert status[neighbour] == 'ready', 'healthy neighbour was not woken in the same sweep'
