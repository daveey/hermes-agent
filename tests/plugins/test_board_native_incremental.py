"""Native board evidence reads incrementally, like the legacy route (``fact_board_native._local``). [daveey]

A full board read releases the GIL on every row step; in the contended hub dashboard backend it took minutes.
The capture now keeps its last rows and change cursor and takes ``board_read.materialize``'s delta.
"""
from contextlib import closing
import json

import pytest

from hermes_cli import fact_board_native as native
from hermes_cli import kanban_db, kanban_db_connect
from plugins.kanban.dashboard import board_fact_views, board_read
from tests.plugins import test_kanban_board_shadow as helpers

shadow_board = helpers.shadow_board


@pytest.fixture
def calls(monkeypatch):
    seen = []
    real = board_read.materialize

    def recording(conn, *args, prev=None, changed=None, **kwargs):
        if 'sync-shadow' not in conn.execute('PRAGMA database_list').fetchone()[2]:  # the live file, not the replica
            seen.append((prev is not None, changed))
        return real(conn, *args, prev=prev, changed=changed, **kwargs)

    monkeypatch.setattr(board_read, 'materialize', recording)
    monkeypatch.setattr(native, '_RETAINED', type(native._RETAINED)())
    return seen


def _cards(api):
    result = native.capture(api, None, 'board', board_fact_views.board_selector())
    return json.dumps(result['value']['local']['rows']['cards'], sort_keys=True, default=str)


def test_the_second_capture_takes_the_delta_and_matches_a_full_read(shadow_board, calls):
    api = shadow_board['mod']
    first = _cards(api)
    second = _cards(api)
    assert [prev for prev, _ in calls] == [False, True]
    # changed_since's own answer: a change set, or FULL when it cannot vouch for one (then a correct full read)
    native._RETAINED.clear()
    assert _cards(api) == first == second  # a fresh full read agrees with the delta


def test_a_card_written_between_captures_reaches_the_delta(shadow_board, calls):
    api = shadow_board['mod']
    _cards(api)
    with closing(kanban_db_connect.connect(kanban_db.kanban_db_path())) as conn:
        tid = kanban_db.create_task(conn, title='written between captures', assignee='default')
    assert tid in _cards(api)
    assert calls[-1][0] is True  # read from the retained rows
    changed = calls[-1][1]
    assert not isinstance(changed, frozenset) or tid in changed  # a delta names the card; FULL re-reads every row


def test_a_failed_read_never_seeds_the_next_delta(shadow_board, calls, monkeypatch):
    api = shadow_board['mod']
    _cards(api)
    assert len(native._RETAINED) == 1

    def broken(*_a, **_k):
        raise OSError('disk')

    monkeypatch.setattr(api.board_fact_capture, 'capture_incremental', broken)
    with pytest.raises(OSError):
        _cards(api)
    assert len(native._RETAINED) == 0


def test_a_replaced_board_file_starts_from_a_full_read(shadow_board, calls, monkeypatch):
    """Retained rows are keyed by the file's identity, like the legacy route's per-file observer."""
    api = shadow_board['mod']
    _cards(api)
    real = native.os.stat
    board = str(kanban_db.kanban_db_path())

    class Replaced:  # the same path, a new file: only its inode differs
        def __init__(self, stat):
            self._stat = stat

        def __getattr__(self, name):
            value = getattr(self._stat, name)
            return value + 1 if name == 'st_ino' else value

    monkeypatch.setattr(native.os, 'stat',
                        lambda path, *a, **k: Replaced(real(path, *a, **k)) if str(path) == board else real(path, *a, **k))
    _cards(api)
    assert [prev for prev, _ in calls] == [False, False]


def test_closing_the_runtime_drops_its_retained_rows(shadow_board, calls):
    from hermes_cli.fact_board_startup import BoardRuntime
    api = shadow_board['mod']
    _cards(api)
    assert native._RETAINED
    runtime = BoardRuntime(api, boards=(kanban_db.get_current_board(),)).start()
    runtime.close()
    assert not native._RETAINED


def test_retained_rows_are_capped(shadow_board, calls):
    api = shadow_board['mod']
    for limit in range(1, native._RETAINED_MAX + 4):  # more distinct selectors than the cap
        native.capture(api, None, 'board', board_fact_views.board_selector(done_limit=limit))
    assert len(native._RETAINED) == native._RETAINED_MAX
