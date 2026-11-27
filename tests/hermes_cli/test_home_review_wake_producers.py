"""CI and shared-blocker wake producers retain frozen probe state. [daveey]"""
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


def _parked(conn, title, *, awaiting=None, probe=None):
    from hermes_cli import kanban_wake_check as kwc
    t = kb.create_task(conn, title=title, assignee='default')
    with dbc.write_txn(conn):
        conn.execute("UPDATE tasks SET status='scheduled' WHERE id=?", (t,))
        payload = {'reason': 'wait', 'timed_park': True, 'wake_at': int(time.time()) + 5000, 'resume_status': 'ready'}
        if awaiting:
            payload['awaiting'] = awaiting
        if probe:
            payload.update(wake_check=probe, check_every=60)
        kb._append_event(conn, t, 'scheduled', payload)
        if probe:
            kwc.set_check(conn, t, probe, 60)
    return t


# P4: frozen CI waits are excluded before I/O; a late direct result is independently inert.
@pytest.mark.parametrize('phase', ['prepared', 'pending', None])
def test_p4_ci_progress_apply(board, phase):
    from hermes_cli import kanban_ci_progress as ci
    ref = 'o/r/pull/1'
    task = _parked(board, 'ci waiter', awaiting={'kind': 'ci', 'ref': ref, 'why': 'checks'}, probe='true')
    assert [w[0] for w in ci._ci_waits(board)] == [task]
    sup.freeze(board, task, phase)
    expected = [] if phase else [task]
    assert [w[0] for w in ci._ci_waits(board)] == expected, 'frozen CI waits must be excluded before I/O'
    snap = _snap(board, task)
    p = ci.Progress(state=ci.NOT_STARTING, repo='o/r', pr=1, per_pr=True, detail='conflict', remedy='rebase')
    exc, out = None, None
    try:
        out = ci.apply(board, task, ref, p)
    except Exception as e:
        exc = e
    print('P4', phase, 'out=', out, 'exc=', repr(exc), 'status=', kb.get_task(board, task).status,
          'bytes_same=', _snap(board, task) == snap, 'probe_rows=', len(_rows(board, 'task_wake_checks', task)))
    if phase:
        assert frozen(board, task)
        assert exc is None, f'uncaught {exc!r}'
        assert _snap(board, task) == snap
    else:
        assert out and kb.get_task(board, task).status == 'ready'


# P5: shared-blocker clear lane; custody lands after the loop-top frozen() read, before the wake writer.
@pytest.mark.parametrize('phase', ['prepared', 'pending', None])
def test_p5_shared_blocker_clear_late_freeze(board, monkeypatch, phase):
    from hermes_cli import kanban_shared_blockers as ksb, kanban_continuation as kc
    task = _parked(board, 'late frozen')
    neighbour = _parked(board, 'healthy neighbour')
    assert task < neighbour
    st = ksb.BlockerState(blocker_id='probe-wall', asserted_at=1, cleared_at=5, cleared_by=('t_clearer', None),
                          cleared_via='run', first_cleared_at=5, behind={task: 1, neighbour: 1})
    monkeypatch.setattr(ksb, 'blocker_states', lambda *a, **k: {'probe-wall': st})
    original = kc.pending_wake
    done = {}

    def racing(conn, tid):
        if tid == task and not done:
            done['x'] = True
            sup.freeze(conn, task, phase)
            done['snap'] = _snap(conn, task)
        return original(conn, tid)
    monkeypatch.setattr(kc, 'pending_wake', racing)
    exc, out = None, None
    try:
        out = ksb.release_cleared_blockers(board)
    except Exception as e:
        exc = e
    print('P5', phase, 'out=', out, 'exc=', repr(exc), 'task=', kb.get_task(board, task).status,
          'neighbour=', kb.get_task(board, neighbour).status, 'bytes_same=', _snap(board, task) == done.get('snap'))
    assert done
    if phase:
        assert exc is None, f'uncaught {exc!r}'
        assert kb.get_task(board, neighbour).status == 'ready'
        assert _snap(board, task) == done['snap']
    else:
        assert exc is None and sorted(out) == [task, neighbour]
