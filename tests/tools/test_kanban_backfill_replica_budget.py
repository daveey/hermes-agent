"""Bound replica setup per repair pass without abandoning late ownership. [daveey]"""
import sqlite3

import pytest

from hermes_cli import kanban_creator_chats as creator
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_lockguard
from tests.tools.test_kanban_cross_board_creator_chats import _replica
from tests.tools.test_kanban_worker_created_chats import WORKER, make_home


@pytest.mark.parametrize('count', [3, 40, 923])
def test_backfill_opens_replica_once_for_unresolved_backlog(tmp_path, monkeypatch, count):
    home = make_home(tmp_path, monkeypatch)
    _replica(home)
    with kbc.connect_closing() as conn:
        children = [kb.create_task(conn, title=f'orphan {i}', assignee='default',
                                   creator_task_id=f'missing-{i}', session_id=WORKER)
                    for i in range(count)]
        for i, child in enumerate(children):
            conn.execute('UPDATE tasks SET status=? WHERE id=?',
                         (('done', 'archived', 'todo')[i % 3], child))
        conn.commit()
        real = kanban_lockguard.connect
        opened = []
        def counted(*args, **kwargs):
            result = real(*args, **kwargs)
            if 'sync-shadow' in str(args[0]):
                opened.append(result)
            return result
        monkeypatch.setattr(kanban_lockguard, 'connect', counted)
        result = creator.backfill(conn)
        assert set(result['attempted']) == set(children)
        assert set(result['unresolved']) == set(children)
        assert len(opened) == 1
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            opened[0].execute('SELECT 1')


def test_all_local_backfill_never_opens_replica(tmp_path, monkeypatch):
    from tests.tools.test_kanban_cross_board_creator_chats import _link
    make_home(tmp_path, monkeypatch)
    with kbc.connect_closing() as conn:
        parent = kb.create_task(conn, title='parent', assignee='default')
        child = kb.create_task(conn, title='child', assignee='default',
                               creator_task_id=parent, session_id=WORKER)
        _link(conn, parent, 'away', 'research', 'human')
        def forbidden(*args, **kwargs):
            pytest.fail('all-local repair opened a replica')
        monkeypatch.setattr(creator._ReplicaReader, '_open', forbidden)
        assert creator.backfill(conn) == {'attempted': [child], 'unresolved': []}


def test_missing_replica_retried_next_pass_and_terminal_child_repaired(tmp_path, monkeypatch):
    home = make_home(tmp_path, monkeypatch)
    remote = _replica(home, 'pending.db')
    with kbc.connect_closing() as conn:
        child = kb.create_task(conn, title='late', assignee='default',
                               creator_task_id=remote, session_id=WORKER)
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (child,))
        conn.commit()
        assert creator.backfill(conn)['unresolved'] == [child]
        shadow = home / 'kanban' / 'sync-shadow'
        for suffix in ('', '-wal', '-shm'):
            source = shadow / f'pending.db{suffix}'
            if source.exists():
                source.rename(shadow / f'default.db{suffix}')
        assert creator.backfill(conn) == {'attempted': [child], 'unresolved': []}
        assert conn.execute('SELECT status FROM tasks WHERE id=?', (child,)).fetchone()[0] == 'done'


def test_reader_sees_new_and_released_links_without_result_cache(tmp_path, monkeypatch):
    from tests.tools.test_kanban_cross_board_creator_chats import _link
    from hermes_cli import kanban_db_notify as notify
    home = make_home(tmp_path, monkeypatch)
    _replica(home)
    path = home / 'kanban' / 'sync-shadow' / 'default.db'
    with kbc.connect_closing() as conn, kbc.connect_closing(path) as remote:
        parent = kb.create_task(remote, title='new parent', assignee='default')
        with creator._ReplicaReader(conn) as reader:
            assert reader.resolve(parent) == []
            _link(remote, parent, 'away', 'research', 'human')
            notify.add_notify_sub(remote, task_id=parent, platform='hub',
                                  chat_id='away/research/human', delivery_metadata={'reason': 'origin'})
            remote.commit()
            owners = reader.resolve(parent)
            assert [(r['chat_profile'], r['chat_session']) for r in owners] == [('research', 'human')]
            remote.execute('UPDATE task_chat_links SET released=1 WHERE task_id=?', (parent,))
            remote.commit()
            assert reader.resolve(parent) == []  # legacy sub cannot resurrect a released link


def test_backfill_reads_local_writes_before_descendant(tmp_path, monkeypatch):
    from tests.tools.test_kanban_cross_board_creator_chats import _link
    make_home(tmp_path, monkeypatch)
    with kbc.connect_closing() as conn:
        parent = kb.create_task(conn, title='parent', assignee='default')
        child = kb.create_task(conn, title='child', assignee='default',
                               creator_task_id=parent, session_id=WORKER)
        grandchild = kb.create_task(conn, title='grandchild', assignee='default',
                                    creator_task_id=child, session_id=WORKER)
        _link(conn, parent, 'away', 'research', 'human')
        result = creator.backfill(conn)
        assert set(result['attempted']) == {child, grandchild}
        assert result['unresolved'] == []
        assert conn.execute('SELECT COUNT(*) FROM task_chat_links WHERE task_id IN (?,?)',
                            (child, grandchild)).fetchone()[0] == 2


def test_replica_closes_on_resolution_error(tmp_path, monkeypatch):
    home = make_home(tmp_path, monkeypatch)
    remote = _replica(home)
    with kbc.connect_closing() as conn:
        reader = creator._ReplicaReader(conn)
        def broken(*args):
            raise sqlite3.OperationalError('injected read failure')
        monkeypatch.setattr(creator, 'resolve_chats', broken)
        with reader:
            assert reader.resolve(remote) == []
            handle = reader.replica
            assert handle is not None
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            handle.execute('SELECT 1')


def test_open_failure_is_bounded_and_retried_on_next_pass(tmp_path, monkeypatch):
    home = make_home(tmp_path, monkeypatch)
    remote = _replica(home, 'pending.db')
    with kbc.connect_closing() as conn:
        children = [kb.create_task(conn, title=f'child{i}', assignee='default',
                                   creator_task_id=remote, session_id=WORKER) for i in range(3)]
        shadow = home / 'kanban' / 'sync-shadow'
        for suffix in ('', '-wal', '-shm'):
            source = shadow / f'pending.db{suffix}'
            if source.exists():
                source.rename(shadow / f'default.db{suffix}')
        real = kanban_lockguard.connect
        attempts = []
        def fail_once(*args, **kwargs):
            attempts.append(args[0])
            if len(attempts) == 1:
                raise sqlite3.OperationalError('temporary schema contention')
            return real(*args, **kwargs)
        monkeypatch.setattr(kanban_lockguard, 'connect', fail_once)
        assert set(creator.backfill(conn)['unresolved']) == set(children)
        assert len(attempts) == 1
        assert creator.backfill(conn)['unresolved'] == []
        assert len(attempts) == 2


def test_new_link_during_backfill_resolves_later_candidate(tmp_path, monkeypatch):
    from tests.tools.test_kanban_cross_board_creator_chats import _link
    home = make_home(tmp_path, monkeypatch)
    _replica(home)
    path = home / 'kanban' / 'sync-shadow' / 'default.db'
    with kbc.connect_closing(path) as remote:
        parent = kb.create_task(remote, title='unowned', assignee='default')
    with kbc.connect_closing() as conn:
        children = [kb.create_task(conn, title=f'child{i}', assignee='default',
                                   creator_task_id=parent, session_id=WORKER) for i in range(3)]
        real = creator.inherit_chats
        visited = []
        def arriving(local, task_id, creator_id, **kwargs):
            real(local, task_id, creator_id, **kwargs)
            visited.append(task_id)
            if len(visited) == 1:
                with kbc.connect_closing(path) as remote:
                    _link(remote, parent, 'away', 'research', 'human')
        monkeypatch.setattr(creator, 'inherit_chats', arriving)
        result = creator.backfill(conn)
        assert set(result['attempted']) == set(children)
        assert result['unresolved'] == visited[:1]
        assert conn.execute("SELECT COUNT(*) FROM task_events WHERE kind='creator_chats_resolved'").fetchone()[0] == 2


def test_reader_closes_missing_schema_and_exception_scope(tmp_path, monkeypatch):
    home = make_home(tmp_path, monkeypatch)
    _replica(home)
    opened = []
    real = kanban_lockguard.connect
    def counted(*args, **kwargs):
        result = real(*args, **kwargs)
        opened.append(result)
        return result
    with kbc.connect_closing() as conn:
        monkeypatch.setattr(kanban_lockguard, 'connect', counted)
        with pytest.raises(RuntimeError, match='abort'):
            with creator._ReplicaReader(conn) as reader:
                reader.resolve('missing')
                raise RuntimeError('abort')
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            opened[-1].execute('SELECT 1')
    path = home / 'kanban' / 'sync-shadow' / 'default.db'
    with kbc.connect_closing(path) as remote:
        remote.execute('DROP TABLE task_chat_links')
        remote.commit()
    with kbc.connect_closing() as conn, creator._ReplicaReader(conn) as reader:
        assert reader.resolve('missing') == []
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            opened[-1].execute('SELECT 1')
