"""Local associations dominate their own qualified stale receipt duplicates."""
import pytest

from hermes_cli import kanban_session_activity as activity
from hermes_cli import kanban_origin_adapter as adapter, kanban_origin_ledger as ledger, profiles
from tests.hermes_cli.test_kanban_session_activity import _add_human_requests, _make_board
import hermes_constants


@pytest.mark.parametrize('status', ['running', 'blocked', 'scheduled'])
@pytest.mark.parametrize('actionable', [False, True])
@pytest.mark.parametrize('source', ['ledger', 'legacy'])
@pytest.mark.parametrize('membership', ['created', 'shepherd'])
def test_local_open_association_dominates_stale_receipt(tmp_path, monkeypatch, status, actionable, source, membership):
    home = tmp_path
    (home / 'kanban').mkdir()
    (home / 'kanban/origin-ledger.db').touch()
    board = home / 'kanban.db'
    _make_board(board, [('t_one', status, 's_one')])
    if actionable:
        _add_human_requests(board, 't_one')
    import sqlite3
    with sqlite3.connect(board) as db:
        if not actionable:
            db.execute('ALTER TABLE tasks ADD COLUMN human_request TEXT')
        db.execute('ALTER TABLE tasks ADD COLUMN block_kind TEXT')
        db.execute("UPDATE tasks SET block_kind='needs_input'")
        if membership == 'shepherd':
            db.execute("UPDATE tasks SET session_id='creator'")
            db.execute('CREATE TABLE kanban_notify_subs (task_id TEXT, platform TEXT, chat_id TEXT, delivery_metadata TEXT)')
            db.execute('INSERT INTO kanban_notify_subs VALUES (?, ?, ?, ?)',
                       ('t_one', 'tui', 's_one', '{"reason":"shepherd"}'))
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
    if source == 'legacy':
        row.pop('origin_session')
    source_rows = [row]
    monkeypatch.setattr(ledger, 'project', lambda *args, **kwargs: source_rows if source == 'ledger' else [])
    monkeypatch.setattr(activity, '_legacy_remote_links', lambda: source_rows if source == 'legacy' else [])
    activity._reset_cache_for_tests()
    cards = activity.session_tasks(['s_one'])
    assert len(cards) == 1
    assert cards[0]['status'] == status
    lean = activity.session_outcome_tasks(['s_one'])
    assert len(lean) == 1
    assert lean[0]['status'] == status
    if status == 'running':
        from hermes_state_outcome import _read_card_items
        decisions, waits = _read_card_items(['s_one'])
        assert decisions == []
        assert [item['state'] for item in waits] == ['running']
    human_blocked = status == 'blocked' and actionable
    owners = ['s_one', 'creator'] if membership == 'shepherd' else ['s_one']
    assert activity.blocked_task_counts() == ({sid: 1 for sid in owners} if human_blocked else {})
    assert activity.active_task_counts() == ({} if human_blocked else {sid: 1 for sid in owners})
    # A different owner profile, board, unknown owner, or receipt-only origin
    # association is not covered by the local association above.
    for changed in ({'profile': 'other'}, {'board': 'other'}, {'profile': None},
                    {'session': 's_other', 'origin_session': 's_other'}):
        row.update(changed)
        activity._reset_cache_for_tests()  # the stubbed projection changed without any store changing
        assert activity._remote_links() == [row]
        row.update(profile='default', board='default', session='s_one', origin_session='s_one')
    other = {**row, 'session': 's_other', 'origin_session': 's_other'}
    source_rows.append(other)
    activity._reset_cache_for_tests()
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
