"""Local associations dominate their own qualified stale receipt duplicates."""
import pytest

from hermes_cli import kanban_session_activity as activity
from hermes_cli import kanban_origin_adapter as adapter, kanban_origin_ledger as ledger, profiles
from tests.hermes_cli.test_kanban_session_activity import _make_board
import hermes_constants


@pytest.mark.parametrize('status', ['running', 'blocked', 'scheduled'])
def test_local_open_association_dominates_stale_receipt(tmp_path, monkeypatch, status):
    home = tmp_path
    (home / 'kanban').mkdir()
    (home / 'kanban/origin-ledger.db').touch()
    board = home / 'kanban.db'
    _make_board(board, [('t_one', status, 's_one')])
    monkeypatch.setattr(hermes_constants, 'get_hermes_home', lambda: home)
    monkeypatch.setattr(adapter, '_gateway', lambda _: 'origin')
    monkeypatch.setattr(profiles, 'get_active_profile_name', lambda: 'default')
    monkeypatch.setattr(activity, '_hub_db_path', lambda: None)
    monkeypatch.setattr(activity, '_board_db_paths', lambda: [board])
    monkeypatch.setattr(activity, '_legacy_remote_links', list)
    row = dict(gateway='origin', profile='default', board='default', id='t_one',
               key='origin/default/default/t_one', session='s_one', origin_session='s_one',
               origin_gateway='origin', origin_profile='default', status='unknown',
               unfinished=True, stale=True)
    monkeypatch.setattr(ledger, 'project', lambda *args, **kwargs: [row])
    activity._reset_cache_for_tests()
    cards = activity.session_tasks(['s_one'])
    assert len(cards) == 1
    assert cards[0]['status'] == status
    assert activity.blocked_task_counts() == ({'s_one': 1} if status == 'blocked' else {})
    assert activity.active_task_counts() == ({} if status == 'blocked' else {'s_one': 1})
    # A different owner profile, board, unknown owner, or receipt-only origin
    # association is not covered by the local association above.
    for changed in ({'profile': 'other'}, {'board': 'other'}, {'profile': None},
                    {'session': 's_other', 'origin_session': 's_other'}):
        row.update(changed)
        assert activity._remote_links() == [row]
        row.update(profile='default', board='default', session='s_one', origin_session='s_one')
    other = {**row, 'session': 's_other', 'origin_session': 's_other'}
    monkeypatch.setattr(ledger, 'project', lambda *args, **kwargs: [row, other])
    assert activity._remote_links() == [other]
    # An arbitrary terminal status is not a verified durable release.
    import sqlite3
    with sqlite3.connect(board) as db:
        db.execute("UPDATE tasks SET status='done'")
    assert activity._remote_links() == [row, other]
    board.unlink()
    monkeypatch.setattr(activity, '_board_db_paths', list)
    assert activity._remote_links() == [row, other]
    activity._reset_cache_for_tests()
