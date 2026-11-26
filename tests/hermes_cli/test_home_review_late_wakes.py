"""Late release and legacy wake writers preserve home custody. [daveey]"""
import time
import pytest
from hermes_cli import kanban_db as kb, kanban_db_connect as dbc, kanban_release as rel
from hermes_cli.kanban_home_transfer import frozen
from tests.hermes_cli import test_home_release_candidates as sup
from tests.hermes_cli.test_kanban_release import spend_stopped, HUMAN
board = sup.board


def _rows(conn, table, task):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
        return None
    return list(map(tuple, conn.execute(f'SELECT * FROM {table} WHERE task_id=?', (task,))))


def _snap(conn, task):
    return (tuple(conn.execute('SELECT * FROM tasks WHERE id=?', (task,)).fetchone()),
            _rows(conn, 'task_events', task), _rows(conn, 'task_wake_checks', task),
            _rows(conn, 'task_wake_episode_barriers', task), _rows(conn, 'task_runs', task))


# P1: release decided with an open parent; before placement the parent finishes AND custody lands.
@pytest.mark.parametrize('phase', ['prepared', 'pending', None])
def test_p1_release_place_parent_done_then_frozen(board, monkeypatch, phase):
    tid = spend_stopped(board)
    parent = kb.create_task(board, title='open parent')
    kb.link_tasks(board, parent, tid)
    original = rel._place
    seen = {}

    def racing(conn, task, *args):
        with dbc.write_txn(conn):
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (parent,))
        assert kb._parents_satisfied(conn, task)
        sup.freeze(conn, task, phase)
        seen['snap'] = _snap(conn, task)
        return original(conn, task, *args)
    monkeypatch.setattr(rel, '_place', racing)
    out = None
    try:
        out = rel.release(board, tid, writer=HUMAN)
    except Exception as exc:
        seen['exc'] = exc
    print('P1', phase, 'out=', out, 'exc=', repr(seen.get('exc')), 'status=', kb.get_task(board, tid).status,
          'bytes_same=', _snap(board, tid) == seen['snap'])
    if phase:
        assert frozen(board, tid)
        assert 'exc' not in seen, f"uncaught {seen['exc']!r}"
        assert out['outcome'] == 'refused'
        assert _snap(board, tid) == seen['snap']
    else:
        assert 'exc' not in seen


def _scheduled_no_wake(conn, title, kind):
    t = kb.create_task(conn, title=title, assignee='default')
    with dbc.write_txn(conn):
        conn.execute("UPDATE tasks SET status='scheduled', block_kind=? WHERE id=?", (kind, t))
    return t


# P2: dispatcher clock-wake sweep: frozen scheduled human-kind card with no wake, healthy neighbour due.
@pytest.mark.parametrize('phase', ['prepared', 'pending', None])
def test_p2_wake_parks_human_invariant(board, phase):
    from hermes_cli import kanban_continuation as kc
    task = _scheduled_no_wake(board, 'frozen human wait', 'needs_input')
    neighbour = _scheduled_no_wake(board, 'healthy neighbour', None)
    sup.freeze(board, task, phase)
    snap = _snap(board, task)
    exc, woken = None, None
    try:
        woken, _ = kc._wake_parks(board, int(time.time()) + 10 * 86400)
    except Exception as e:
        exc = e
    print('P2', phase, 'woken=', woken, 'exc=', repr(exc), 'task=', kb.get_task(board, task).status,
          'neighbour=', kb.get_task(board, neighbour).status, 'bytes_same=', _snap(board, task) == snap)
    if phase:
        assert exc is None, f'uncaught {exc!r}'
        assert kb.get_task(board, neighbour).status != 'scheduled'
        assert _snap(board, task) == snap
    else:
        assert exc is None
        assert kb.get_task(board, task).status == 'blocked'
        assert kb.get_task(board, neighbour).status != 'scheduled'


# P2b: same through the public dispatcher entry (real clock; only the frozen human-kind card).
@pytest.mark.parametrize('phase', ['prepared', 'pending'])
def test_p2b_wake_due_continuations(board, phase):
    from hermes_cli import kanban_continuation as kc
    task = _scheduled_no_wake(board, 'frozen human wait', 'needs_input')
    sup.freeze(board, task, phase)
    snap = _snap(board, task)
    kc.wake_due_continuations(board)
    assert _snap(board, task) == snap


# P3: _wake on a frozen scheduled card (skip_wake branch) is not write-free.
@pytest.mark.parametrize('phase', ['prepared', 'pending'])
def test_p3_wake_skip_branch_writes(board, phase):
    from hermes_cli import kanban_wake_check as kwc
    task = _scheduled_no_wake(board, 'frozen', None)
    sup.freeze(board, task, phase)
    snap = _snap(board, task)
    exc = None
    try:
        with dbc.write_txn(board):
            kwc._wake(board, task, 'shared_blocker_cleared', {})
    except Exception as e:
        exc = e
    print('P3', phase, 'exc=', repr(exc), 'bytes_same=', _snap(board, task) == snap)
    assert exc is None, f'uncaught {exc!r}'
    assert _snap(board, task) == snap
