"""Shadow input captures retain canonical ownership/caps without live render reads."""
from contextlib import closing
import time

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli.fact_contract import freeze
from plugins.kanban.dashboard import board_fact_capture, board_shadow
from tests.plugins import test_kanban_board_shadow as helpers

shadow_board = helpers.shadow_board


def _capture(env, cap=80):
    with closing(kbc.connect()) as live, closing(kbc.connect(env['shadow'])) as replica:
        live.execute('BEGIN')
        replica.execute('BEGIN')
        local = board_fact_capture.capture(live, done_limit=cap)
        payload = board_fact_capture.project(freeze(local))
        captured = board_shadow.capture(live, replica, payload, None, False, None, None, cap)
        return freeze(local), freeze(captured)


def test_shadow_capture_is_plain_canonical_and_detached(shadow_board, monkeypatch):
    monkeypatch.setattr(time, 'time', lambda: 1_700_000_000.0)
    expected = helpers._read(shadow_board)
    local, remote = _capture(shadow_board)

    def forbidden(*args, **kwargs):
        pytest.fail('shadow rendering performed source I/O or sampled a clock')

    with monkeypatch.context() as guard:
        guard.setattr(kbc, 'connect', forbidden)
        guard.setattr(board_shadow.shadow_view, 'homes_of', forbidden)
        guard.setattr(board_shadow.board_read, 'materialize', forbidden)
        guard.setattr(board_shadow.board_read, 'diagnostics_from_inputs', forbidden)
        guard.setattr(time, 'time', forbidden)
        actual = board_shadow.project_captured(board_fact_capture.project(local), remote)
        assert actual == expected
        actual['columns'][0]['tasks'].clear()
        assert board_shadow.project_captured(board_fact_capture.project(local), remote) == expected

    with closing(kbc.connect(shadow_board['shadow'])) as replica, replica:
        replica.execute('DELETE FROM tasks WHERE id=?', (shadow_board['running'],))
    next_local, next_remote = _capture(shadow_board)
    updated = board_shadow.project_captured(board_fact_capture.project(next_local), next_remote)
    assert shadow_board['running'] not in helpers._cards(updated)
    assert board_shadow.project_captured(board_fact_capture.project(local), remote) == expected


@pytest.mark.parametrize('cap', [0, 1, 3, 80])
@pytest.mark.parametrize('hidden_live', [False, True])
def test_capture_ownership_precedes_filters_and_combined_done_window(shadow_board, monkeypatch, cap, hidden_live):
    monkeypatch.setattr(time, 'time', lambda: 1_700_000_000.0)
    with closing(kbc.connect()) as live, live:
        live.execute('UPDATE tasks SET status=?, completed_at=99 WHERE id=?',
                     ('archived' if hidden_live else 'done', shadow_board['live_id']))
    with closing(kbc.connect(shadow_board['shadow'])) as replica, replica:
        replica.execute('UPDATE tasks SET status="done", completed_at=100 WHERE id=?',
                        (shadow_board['live_id'],))
        for i, completed in enumerate((99, 99, None, 0)):
            tid = kb.create_task(replica, title=f'captured done {i}', assignee='default')
            replica.execute('UPDATE tasks SET status="done", completed_at=? WHERE id=?', (completed, tid))
        replica.execute('UPDATE tasks SET created_on=NULL WHERE id=?', (shadow_board['ready'],))
    expected = helpers._read(shadow_board, done_limit=cap)
    local, remote = _capture(shadow_board, cap)
    actual = board_shadow.project_captured(board_fact_capture.project(local), remote)
    assert actual == expected
    assert helpers._cards(actual)[shadow_board['ready']]['home_gateway'] == 'unknown'
    if hidden_live:
        assert shadow_board['live_id'] not in helpers._cards(actual)


@pytest.mark.parametrize('missing', ['live', 'replica', 'both'])
def test_shadow_capture_requires_both_transactions(shadow_board, missing):
    with closing(kbc.connect()) as live, closing(kbc.connect(shadow_board['shadow'])) as replica:
        if missing == 'live':
            replica.execute('BEGIN')
        elif missing == 'replica':
            live.execute('BEGIN')
        with pytest.raises(ValueError, match='caller-owned read transactions'):
            board_shadow.capture(live, replica, {}, None, False, None, None, 80)


def test_shadow_capture_failure_is_not_authoritative_empty(shadow_board, monkeypatch):
    monkeypatch.setattr(time, 'time', lambda: 1_700_000_000.0)
    local, remote = _capture(shadow_board)
    original = board_shadow.shadow_view.homes_of

    def broken(*args, **kwargs):
        raise OSError('controlled replica metadata outage')

    with monkeypatch.context() as guard:
        guard.setattr(board_shadow.shadow_view, 'homes_of', broken)
        with pytest.raises(OSError, match='controlled replica metadata outage'):
            _capture(shadow_board)
    assert board_shadow.shadow_view.homes_of is original
    recovered_local, recovered_remote = _capture(shadow_board)
    assert board_shadow.project_captured(board_fact_capture.project(local), remote) == (
        board_shadow.project_captured(board_fact_capture.project(recovered_local), recovered_remote))
