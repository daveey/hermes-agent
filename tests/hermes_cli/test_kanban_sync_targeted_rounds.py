"""Event wakes must not pull unchanged sibling boards."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_sync_client as client


@pytest.fixture(autouse=True)
def _boards_in_tmp_cwd(tmp_path, monkeypatch):
    """Board paths here are relative names: keep their SQLite files out of the checkout."""
    monkeypatch.chdir(tmp_path)


def test_feed_wake_only_pulls_changed_board_and_local_outbox(monkeypatch):
    boards = [(name, Path(name)) for name in ('one', 'two', 'three')]
    wake_by_board = {}
    rounds = []
    pending = set()
    clock = [100.0]
    monkeypatch.setattr(client, 'load_identity', lambda: client.Identity('https://example.com', 'test', 'gateway'))
    monkeypatch.setattr(client, 'board_paths', lambda: boards)
    monkeypatch.setattr(client.sync, 'set_lease_client', lambda _: None)
    monkeypatch.setattr(client, 'publish_pickup_if_changed', lambda _: None)
    monkeypatch.setattr(client, 'reconcile_links', lambda *args, **kwargs: None)
    monkeypatch.setattr(client, 'outbox_pending', lambda paths: any(p in pending for p in paths))
    monkeypatch.setattr(client, 'time', SimpleNamespace(monotonic=lambda: clock[0], sleep=lambda _: None))
    from hermes_cli import machine_metrics
    monkeypatch.setattr(machine_metrics, 'Reporter', lambda _: SimpleNamespace(report_due=lambda: None, pending_wake_in=lambda: None))

    class Watcher:
        def __init__(self, ident, board, wake, keep_running, **kwargs):
            wake_by_board[board] = lambda: (kwargs.get('on_change', lambda _: None)(board), wake.set())
        def run(self):
            pass

    class Thread:
        def __init__(self, **kwargs):
            pass
        def start(self):
            pass
        def is_alive(self):
            return True

    import threading
    monkeypatch.setattr(client, 'threading', SimpleNamespace(Thread=Thread, Event=threading.Event))
    monkeypatch.setattr(client, 'FeedWatcher', Watcher)
    def sync_board(slug, *args):
        rounds[-1].append(slug)
        return client.RoundResult(board=slug, mode='primary')
    monkeypatch.setattr(client, 'sync_board', sync_board)
    def keep_running():
        return len(rounds) < 4
    def wait(*args):
        if len(rounds) == 1:
            wake_by_board['one']()
        elif len(rounds) == 2:
            pending.add(Path('three'))
            wake_by_board['two']()
        clock[0] += 1
        rounds.append([])
        return 'remote'
    monkeypatch.setattr(client, 'wait_for_work', wait)
    rounds.append([])
    client.run_forever(keep_running, interval=30)
    assert rounds[:3] == [['one', 'two', 'three'], ['one'], ['two', 'three']]


def test_events_during_io_and_periodic_recovery_are_not_lost():
    from hermes_cli.kanban_sync_work import SyncWork
    work = SyncWork(30)
    boards = [('one', Path('one')), ('two', Path('two'))]
    assert work.select(boards, 100, lambda _: False) == boards
    assert work.select(boards, 101, lambda _: False) == []
    work.changed('one')
    assert work.select(boards, 102, lambda _: False) == boards[:1]
    # Arrival after selection (while the network call is in flight).
    work.changed('one')
    assert work.select(boards, 103, lambda _: False) == boards[:1]
    work.changed('one')
    assert work.select(boards, 130, lambda _: False) == boards
    assert work.select(boards, 131, lambda _: False) == []
    added = boards + [('three', Path('three'))]
    assert work.select(added, 132, lambda _: False) == added[2:]


@pytest.mark.parametrize('journal', ['DELETE', 'WAL'])
def test_unchanged_outbox_does_not_query_and_commit_is_seen_next_tick(tmp_path, journal, monkeypatch):
    import sqlite3
    path = tmp_path / 'board.db'
    writer = sqlite3.connect(path)
    writer.execute(f'PRAGMA journal_mode={journal}')
    writer.execute('CREATE TABLE kanban_sync_outbox (state TEXT)')
    writer.commit()
    watch = client._BoardWatch()
    from hermes_cli import kanban_sync_board_watch as watcher
    traced = []
    connect = watcher._kanban_lockguard.connect
    def traced_connect(*args, **kwargs):
        conn = connect(*args, **kwargs)
        conn.set_trace_callback(traced.append)
        return conn
    monkeypatch.setattr(watcher._kanban_lockguard, 'connect', traced_connect)
    sql = "SELECT 1 FROM kanban_sync_outbox WHERE state = 'pending' LIMIT 1"
    try:
        assert watch.query(path, sql, unchanged_ok=True)[1] is None
        traced.clear()  # include replacement handles when DELETE commits move the main file
        assert watch.query(path, sql, unchanged_ok=True)[1] is None
        assert traced == ['PRAGMA data_version']
        writer.execute("INSERT INTO kanban_sync_outbox VALUES ('pending')")
        writer.commit()
        assert watch.query(path, sql, unchanged_ok=True)[1] == (1,)
        assert sql in traced
    finally:
        watch.reset()
        writer.close()


def test_checkpoint_reopens_watcher_generation(tmp_path):
    import sqlite3
    path = tmp_path / 'checkpoint.db'
    writer = sqlite3.connect(path)
    writer.execute('PRAGMA journal_mode=WAL')
    writer.execute('PRAGMA wal_autocheckpoint=0')
    writer.execute('CREATE TABLE probe (value INTEGER)')
    writer.execute('INSERT INTO probe VALUES (1)')
    writer.commit()
    watch = client._BoardWatch()
    try:
        first = watch.query(path, 'SELECT value FROM probe', unchanged_ok=True)
        writer.execute('UPDATE probe SET value=2')
        writer.commit()
        changed = watch.query(path, 'SELECT value FROM probe', unchanged_ok=True)
        assert changed == (first[0], (2,))  # a WAL commit keeps the reader generation
        result = writer.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
        assert result[0] == 0
        checkpointed = watch.query(path, 'SELECT value FROM probe', unchanged_ok=True)
        assert checkpointed[1] == (2,)
        assert checkpointed[0] != first[0]  # a main-image change cannot certify old history
    finally:
        watch.reset()
        writer.close()


def test_outbox_cache_reopens_replaced_file_and_retries_query_errors(tmp_path):
    import sqlite3
    path = tmp_path / 'board.db'
    replacement = tmp_path / 'replacement.db'
    sql = "SELECT 1 FROM kanban_sync_outbox WHERE state = 'pending' LIMIT 1"
    watch = client._BoardWatch()
    try:
        with sqlite3.connect(path) as writer:
            writer.execute('CREATE TABLE unrelated (value TEXT)')
        assert watch.query(path, sql, unchanged_ok=True) is None
        with sqlite3.connect(path) as writer:
            writer.execute('CREATE TABLE kanban_sync_outbox (state TEXT)')
        first = watch.query(path, sql, unchanged_ok=True)
        assert first is not None and first[1] is None
        # Replace only this isolated, rollback-journal fixture; never a live WAL DB.
        with sqlite3.connect(replacement) as writer:
            writer.execute('CREATE TABLE kanban_sync_outbox (state TEXT)')
            writer.execute("INSERT INTO kanban_sync_outbox VALUES ('pending')")
        replacement.replace(path)
        replaced = watch.query(path, sql, unchanged_ok=True)
        assert replaced is not None and replaced[1] == (1,)
        assert replaced[0] != first[0]
    finally:
        watch.reset()
