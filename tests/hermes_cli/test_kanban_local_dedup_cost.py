"""Receipt aliases share board reads without sharing evidence across profiles."""
from hermes_cli import kanban_session_activity as activity
from hermes_cli import kanban_links_associations as associations


def test_repeated_receipts_read_each_board_alias_once(monkeypatch, tmp_path):
    board = tmp_path / 'kanban.db'
    calls = []
    monkeypatch.setattr(activity, '_board_db_paths', lambda: [board])

    def read(path, ids, statuses):
        calls.append((path, tuple(ids)))
        return [{'id': 'open'}] if ids == ['tip'] else []

    monkeypatch.setattr(associations, 'read', read)
    rows = [dict(gateway='gw', profile='default', board='default', id='open',
                 key='gw/default/default/open', session='tip', origin_session='tip')
            for _ in range(20)]
    # An unrepresented receipt alias and a foreign profile must survive.
    retained = [{**rows[0], 'origin_session': 'old'}, {**rows[0], 'profile': 'other', 'key': 'gw/other/default/open'}]
    result = activity._without_local_open_duplicates(rows + retained, gateway='gw', profile='default')
    assert result == retained
    assert len(calls) == 2
    # Request-local only: changed board state must be consulted on the next call.
    monkeypatch.setattr(associations, 'read', lambda *args: [])
    assert activity._without_local_open_duplicates(rows, gateway='gw', profile='default') == rows
