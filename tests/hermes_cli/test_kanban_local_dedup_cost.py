"""Receipt aliases share board reads without sharing evidence across profiles."""
from hermes_cli import kanban_session_activity as activity
from hermes_cli import kanban_links_associations as associations


def test_repeated_receipts_read_each_board_once_for_all_aliases(monkeypatch, tmp_path):
    """2026-11-23: one guarded connection per board/alias was a third of each links projection under _links_lock.
    Every alias on a board is now one read; membership stays per alias."""
    board, other = tmp_path / 'kanban.db', tmp_path / 'boards' / 'b2' / 'kanban.db'
    calls = []
    monkeypatch.setattr(activity, '_board_db_paths', lambda: [board, other])
    monkeypatch.setattr(activity, '_board_slug', lambda p: 'default' if p == board else 'b2')

    def pairs(path, ids, statuses):
        calls.append((path, tuple(ids)))
        return [{'session': 'tip', 'id': 'open'}] if path == board else []

    monkeypatch.setattr(associations, 'pairs', pairs)
    rows = [dict(gateway='gw', profile='default', board='default', id='open',
                 key='gw/default/default/open', session='tip', origin_session='tip')
            for _ in range(20)]
    # An unrepresented receipt alias, a foreign profile and another board's unlisted receipt must survive.
    retained = [{**rows[0], 'origin_session': 'old'}, {**rows[0], 'profile': 'other', 'key': 'gw/other/default/open'},
                {**rows[0], 'board': 'b2', 'key': 'gw/default/b2/open'}]
    result = activity._without_local_open_duplicates(rows + retained, gateway='gw', profile='default')
    assert result == retained
    assert sorted(calls) == sorted([(board, ('old', 'tip')), (other, ('tip',))]), 'not one read per board'
    # Request-local only: changed board state must be consulted on the next call.
    monkeypatch.setattr(associations, 'pairs', lambda *args: [])
    assert activity._without_local_open_duplicates(rows, gateway='gw', profile='default') == rows
