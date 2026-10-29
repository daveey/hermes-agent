"""Origin projection caches must not cross profile boundaries."""
import pytest
import json
import sqlite3

from hermes_cli import kanban_session_activity as activity
from hermes_cli import kanban_db, profiles


@pytest.fixture
def scoped(tmp_path, monkeypatch):
    current = ['default']
    monkeypatch.setattr(profiles, 'get_active_profile_name', lambda: current[0])
    monkeypatch.setattr(kanban_db, 'kanban_home', lambda: tmp_path)
    monkeypatch.setattr(activity, '_board_db_paths', list)
    activity._reset_cache_for_tests()
    yield current
    activity._reset_cache_for_tests()


def card():
    return {'session': 'same', 'id': 'task', 'key': 'owner/work/default/task',
            'status': 'running', 'gateway': 'owner', 'profile': 'work'}


def test_count_cache_does_not_leak_between_profiles(scoped, monkeypatch):
    monkeypatch.setattr(activity, '_remote_links', lambda: [card()] if scoped[0] == 'default' else [])
    assert activity.active_task_counts() == {'same': 1}
    scoped[0] = 'work'
    assert activity.active_task_counts() == {}


def test_remote_memory_fallback_does_not_leak_between_profiles(scoped, monkeypatch, tmp_path):
    monkeypatch.setattr(activity, '_hub_db_path', lambda: tmp_path / 'replica')
    monkeypatch.setattr(activity, '_read_remote_links', lambda _, **kw: [card()])
    assert activity._legacy_remote_links() == [card()]
    scoped[0] = 'work'
    def unavailable(_, **kw):
        raise OSError('offline')
    monkeypatch.setattr(activity, '_read_remote_links', unavailable)
    with pytest.raises(OSError, match='offline'):
        activity._legacy_remote_links()


def test_durable_snapshot_is_scoped_by_origin_profile(scoped):
    activity._remote_snapshot([card()])
    scoped[0] = 'work'
    assert activity._remote_snapshot() is None
    activity._remote_snapshot([])
    scoped[0] = 'default'
    assert activity._remote_snapshot() == [card()]


def test_unqualified_old_snapshot_never_disappears_after_scoped_write(scoped, tmp_path):
    with sqlite3.connect(tmp_path / 'origin-work.db') as db:
        db.execute('CREATE TABLE snapshot (id INTEGER PRIMARY KEY, data TEXT)')
        db.execute('INSERT INTO snapshot VALUES (1, ?)', (json.dumps([card()]),))
    activity._remote_snapshot([])
    with pytest.raises(ValueError, match='unqualified'):
        activity._remote_snapshot()


def test_attention_cache_does_not_leak_between_profiles(scoped, monkeypatch, tmp_path):
    monkeypatch.setattr(activity, '_done_checks', {'test': lambda _: True})
    monkeypatch.setattr(activity, '_board_db_paths', lambda: [tmp_path / 'kanban.db'])
    monkeypatch.setattr(activity, '_read_recent_done',
                        lambda *_: [('same', card())] if scoped[0] == 'default' else [])
    assert activity.attention_task_counts() == {'same': 1}
    scoped[0] = 'work'
    assert activity.attention_task_counts() == {}


def test_shepherd_chat_counts_its_card_not_the_cards_children(scoped, monkeypatch, tmp_path):
    """A chat that shepherds a card (explicit notify-subscribe) counts that card, not every card the card's
    worker fans out: children inherit only owning subs. The creating chat still counts the whole program.
    Regression: runners-panel chat d48ade showed 5 unfinished cards it never created. [daveey]"""
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_notify as kbn

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    board = tmp_path / 'board.db'
    monkeypatch.setattr(activity, '_board_db_paths', lambda: [board])
    conn = kbc.connect(board)
    try:
        parent = kanban_db.create_task(conn, title='shepherd', assignee='default', session_id='creator')
        kbn.add_notify_sub(conn, task_id=parent, platform='tui', chat_id='creator',
                           delivery_metadata={'reason': 'create'})
        for chat in ('panel', 'other-panel'):
            kbn.add_notify_sub(conn, task_id=parent, platform='tui', chat_id=chat,
                               delivery_metadata={'reason': 'shepherd'})
        for i in range(2):
            kanban_db.create_task(conn, title=f'fan-out {i}', assignee='w', creator_task_id=parent)
    finally:
        conn.close()
    counts = activity._read_counts(board)
    assert counts == {'creator': 3, 'panel': 1, 'other-panel': 1}
