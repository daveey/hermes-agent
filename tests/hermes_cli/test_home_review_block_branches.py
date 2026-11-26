"""Writer-entry block refusal preserves custody and bulk healthy peers. [daveey]"""
import pytest
from hermes_cli import kanban_db as kb, kanban_db_connect as dbc
from tests.hermes_cli import test_home_release_candidates as sup
board = sup.board

CASES = {
    'plain': dict(status='ready', kw=dict(reason='x', kind='needs_input')),
    'with_comment': dict(status='ready', kw=dict(reason='x', kind='needs_input', comment=('me', 'BLOCKED: x'))),
    'timed_park': dict(status='ready', kw=dict(reason='cooldown', kind='time',
                                               clock_only=True,
                                               awaiting={'kind': 'clock', 'why': 'quota reset window'})),
    'classify_in_place': dict(status='blocked', kw=dict(reason='x', kind='needs_input')),
}


def _snapshot(conn, task):
    return tuple(tuple(map(tuple, conn.execute(f'SELECT * FROM {table} WHERE {key}=?', (task,))))
                 for table, key in [('tasks', 'id'), ('task_events', 'task_id'),
                                    ('task_comments', 'task_id'), ('task_runs', 'task_id')])


@pytest.mark.parametrize('phase', ['prepared', 'pending', 'malformed', None])
@pytest.mark.parametrize('case', sorted(CASES))
def test_p6_block_branch_exception(board, phase, case):
    spec = CASES[case]
    task = sup.card(board, spec['status'])
    sup.freeze(board, task, phase)
    row = _snapshot(board, task)
    exc, out = None, None
    try:
        out = kb.block_task(board, task, **spec['kw'])
    except Exception as e:
        exc = e
    same = _snapshot(board, task) == row
    print('P6', case, phase, 'out=', out, 'exc=', type(exc).__name__, str(exc)[:80], 'row_same=', same)
    if phase:
        assert same
        assert isinstance(exc, ValueError), f'{case}: not the structured ValueError: {exc!r}'
    else:
        assert exc is None and out is True


# P7: the CLI bulk loop `hermes kanban block A B` (per-id op tolerates ValueError only).
@pytest.mark.parametrize('phase', ['prepared', 'pending'])
def test_p7_cli_bulk_block(board, phase, capsys):
    from hermes_cli.kanban_cli_park import _guarded_park, _commented
    from hermes_cli.kanban_output import _bulk_apply
    task, neighbour = sup.card(board, 'ready'), sup.card(board, 'ready')
    sup.freeze(board, task, phase)
    exc, rc = None, None
    reason, refused = 'operator pause', {}
    # The same wrappers _cmd_block composes (kanban.py:1484-1490), minus argparse.
    op = _guarded_park(board, 'block', (None, None, False), refused, _commented(
        reason, 'me', 'BLOCKED', lambda tid, comment: kb.block_task(
            board, tid, reason=reason, kind='transient', comment=comment)),
        awaiting=None, reason=reason, kind='transient')
    try:
        rc = _bulk_apply([task, neighbour], op, lambda t: f'blocked {t}', lambda t: refused.get(t) or f'cannot block {t}',
                         conn=board)
    except Exception as e:
        exc = e
    print('P7', phase, 'rc=', rc, 'exc=', repr(exc), 'neighbour=', kb.get_task(board, neighbour).status,
          capsys.readouterr())
    assert exc is None, f'uncaught {exc!r}'
    assert kb.get_task(board, neighbour).status == 'blocked'
