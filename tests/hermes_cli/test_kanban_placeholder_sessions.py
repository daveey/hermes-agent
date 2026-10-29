"""Placement identities are attribution, never user conversations."""
from contextlib import contextmanager

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_chat_links, kanban_db_notify, kanban_placement


@pytest.mark.parametrize('session', ['kanban-placement', 'kanban-placement-macbook', 'configured-creator'])
def test_placeholder_never_subscribed_or_projected(tmp_path, monkeypatch, session):
    monkeypatch.setattr(kanban_placement, 'config', lambda: {'creator_session': {'hub': 'configured-creator'}})
    with kbc.connect_closing(tmp_path / 'board.db') as conn:
        tid = kb.create_task(conn, title='identity guard', session_id=session)
        kanban_db_notify.add_notify_sub(conn, task_id=tid, platform='hub', chat_id=f'hub/default/{session}')
        kanban_db_notify.add_notify_sub(conn, task_id=tid, platform='tui', chat_id=session)
        assert conn.execute('SELECT COUNT(*) FROM kanban_notify_subs').fetchone()[0] == 0
        assert kanban_chat_links._local_links(conn, 'hub') == {}
        kanban_db_notify.add_notify_sub(conn, task_id=tid, platform='tui', chat_id='real-chat')
        assert conn.execute('SELECT chat_id FROM kanban_notify_subs').fetchone()[0] == 'real-chat'
        assert len(kanban_chat_links._local_links(conn, 'hub')) == 1


def test_stale_remote_notice_dropped_before_session_open(tmp_path, caplog):
    from tui_gateway import kanban_remote_notices as notices
    opened = []
    @contextmanager
    def session_db(profile, sid):
        opened.append(sid)
        yield None, None
    notices._deliver_link('macbook', 'default/default/t_probe', {'id': 't_probe'},
                          {'profile': 'default', 'session': 'kanban-placement-macbook'},
                          me='hub', ref='ref', state='done', path=tmp_path / 'hub.db', live=set(),
                          session_db=session_db, ledger=None, settled=lambda *a: False, delivered=[], swept=[])
    assert opened == []
    assert 'placeholder' in caplog.text


def test_placeholder_origin_binding_skipped_before_storage(monkeypatch):
    from hermes_cli import kanban_origin_adapter as adapter
    monkeypatch.setattr(adapter, '_gateway', lambda _: 'hub')
    binding = {'binding_id': 'binding123', 'version': 1, 'publication_version': 1,
               'owner': {'gateway': 'macbook', 'profile': 'default', 'board': 'default', 'task_id': 't_probe'},
               'origin': {'gateway': 'hub', 'profile': 'default', 'session': 'kanban-placement-macbook'},
               'task_record_key': 'default/default/t_probe'}
    request = {'id': 'request123', 'from': 'gw:macbook', 'to_gw': 'hub', 'coll': 'sessions',
               'key': 'default/kanban-placement-macbook', 'action': 'origin-bind', 'args': {'binding': binding}}
    assert adapter.receive(request)['status'] == 'skipped'


def test_unloaded_placeholder_notice_clears_stale_subscription(tmp_path, caplog):
    from tui_gateway import kanban_unloaded
    with kbc.connect_closing(tmp_path / 'board.db') as conn:
        tid = kb.create_task(conn, title='stale routing')
        ident = dict(task_id=tid, platform='tui', chat_id='kanban-placement-macbook', thread_id='')
        with kbc.write_txn(conn):
            conn.execute("INSERT INTO kanban_notify_subs (task_id, platform, chat_id, thread_id, created_at) "
                         "VALUES (?, 'tui', ?, '', 1)", (tid, ident['chat_id']))
        assert kanban_unloaded._deliver_sub(conn, 'default', ident, ident, 0, 0, None, kb,
                                           kanban_db_notify, (), None, None, None, None) is None
        assert conn.execute('SELECT COUNT(*) FROM kanban_notify_subs').fetchone()[0] == 0
    assert 'placeholder' in caplog.text


def test_origin_creation_and_ledger_projection_ignore_placeholder(tmp_path, monkeypatch):
    from hermes_cli.kanban_origin_creation import _subscribe_origin
    from hermes_cli import kanban_session_activity
    origin = dict(gateway='hub', profile='default', session='kanban-placement-macbook')
    with kbc.connect_closing(tmp_path / 'board.db') as conn:
        tid = kb.create_task(conn, title='origin routing')
        with kbc.write_txn(conn):
            _subscribe_origin(conn, tid, origin)
        assert conn.execute('SELECT COUNT(*) FROM kanban_notify_subs').fetchone()[0] == 0
    monkeypatch.setattr(kanban_session_activity, '_remote_links', lambda: [
        dict(id=tid, board='default', origin_gateway='hub', session=origin['session'], via='origin')])
    assert kanban_chat_links._ledger_links('default', 'hub') == {}

