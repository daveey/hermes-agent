"""Loop-owned SQLite reuse: real board rounds, lifecycle and isolation contracts."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import kanban_sync_support as support
from tests.hermes_cli.kanban_sync_fake_board import FakeService


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = support.kanban_home(tmp_path, monkeypatch)
    support.write_sync_config(home, True)
    path = home / "kanban.db"
    conn = kbc.connect(path)
    client.ensure_identity(conn, "default", "macbook")
    conn.close()
    return path, FakeService().hub("macbook")


@pytest.fixture
def opened(monkeypatch):
    connections = []
    original = client._connect

    def track(path):
        conn = original(path)
        connections.append(conn)
        return conn

    monkeypatch.setattr(client, "_connect", track)
    return connections


def assert_closed(conn):
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        conn.execute("SELECT 1")


def test_successive_rounds_reuse_live_and_shadow(board, opened):
    # The fallback exposes the baseline's repeated opens as an assertion failure.
    scope = getattr(client, "connection_scope", nullcontext)
    path, hub = board
    with scope():
        client.sync_board("default", path, hub, "macbook")
        first = len(opened)
        assert first == 2
        client.sync_board("default", path, hub, "macbook")
        assert len(opened) == first
        assert all(not conn.in_transaction for conn in opened)
    for conn in opened:
        assert_closed(conn)


def test_shadow_initialization_failure_closes_descriptor(board, opened, monkeypatch):
    def fail(*args):
        raise sqlite3.OperationalError("identity failure")

    monkeypatch.setattr(client, "ensure_identity", fail)
    with client.connection_scope():
        with pytest.raises(sqlite3.OperationalError, match="identity failure"):
            client.open_shadow("default", "macbook")
        assert len(opened) == 1
        assert_closed(opened[0])


def test_standalone_rounds_close(board, opened):
    path, hub = board
    for _ in range(2):
        client.sync_board("default", path, hub, "macbook")
        for conn in opened:
            assert_closed(conn)
    assert len(opened) == 4


@pytest.mark.parametrize("phase", ["pull", "push"])
def test_failed_round_discards_handles(board, opened, monkeypatch, phase):
    path, hub = board

    def fail(*args, **kwargs):
        raise OSError("offline")

    original = getattr(client, phase)
    with client.connection_scope():
        client.sync_board("default", path, hub, "macbook")
        monkeypatch.setattr(client, phase, fail)
        with pytest.raises(OSError, match="offline"):
            client.sync_board("default", path, hub, "macbook")
        for conn in opened:
            assert_closed(conn)
        monkeypatch.setattr(client, phase, original)
        client.sync_board("default", path, hub, "macbook")
        assert len(opened) == 4


def test_reuse_sees_external_writer(board, opened):
    from hermes_cli import kanban_db as kb

    path, hub = board
    with client.connection_scope():
        client.sync_board("default", path, hub, "macbook")
        writer = kbc.connect(path)
        try:
            task = kb.create_task(writer, title="external", assignee="worker")
        finally:
            writer.close()
        result = client.sync_board("default", path, hub, "macbook")
        assert result.push is not None and result.push.sent > 0
        assert hub.svc.board("default")["rows"]["tasks"][task]["title"] == "external"
        assert len(opened) == 2


def test_pool_replacement_transaction_and_bound(tmp_path):
    from hermes_cli.kanban_sync_connections import connection, connection_scope

    path = tmp_path / "db"
    other = tmp_path / "other"
    with connection_scope(limit=1):
        with connection(path, lambda: sqlite3.connect(path)) as first:
            first.execute("CREATE TABLE data(value)")
        with connection(other, lambda: sqlite3.connect(other)) as second:
            second.execute("SELECT 1")
        assert_closed(first)
        with connection(other, lambda: sqlite3.connect(other)) as dirty:
            assert dirty is second
            dirty.execute("CREATE TABLE data(value)")
            dirty.execute("INSERT INTO data VALUES (1)")
        assert_closed(dirty)
        with connection(other, lambda: sqlite3.connect(other)) as clean:
            assert clean.execute("SELECT * FROM data").fetchall() == []
        replacement = tmp_path / "replacement"
        fresh = sqlite3.connect(replacement)
        fresh.execute("CREATE TABLE replacement(value)")
        fresh.close()
        replacement.replace(other)
        with connection(other, lambda: sqlite3.connect(other)) as replaced:
            assert_closed(clean)
            replaced.execute("SELECT * FROM replacement")
    assert_closed(replaced)


@pytest.mark.platforms("posix")
def test_pool_retires_deleted_wal_generation(tmp_path):
    from hermes_cli import kanban_generation, kanban_lockguard
    from hermes_cli.kanban_sync_connections import connection, connection_scope

    path = tmp_path / "generation.db"
    def opener():
        conn = kanban_lockguard.connect(path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE IF NOT EXISTS data(value)")
        kanban_generation.remember(conn)
        return conn

    with connection_scope():
        with connection(path, opener) as first:
            first.execute("SELECT * FROM data").fetchall()
            first.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        Path(str(path) + "-wal").unlink()
        Path(str(path) + "-shm").unlink()
        with connection(path, opener) as second:
            assert second is not first
            assert_closed(first)
            second.execute("SELECT * FROM data").fetchall()
        # Same-thread recovery may also close an idle handle before the next borrow.
        second.close()
        with connection(path, opener) as third:
            assert third is not second
            third.execute("SELECT 1")


def test_closed_borrow_is_not_cached(tmp_path):
    from hermes_cli.kanban_sync_connections import connection, connection_scope

    path = tmp_path / "closed.db"
    with connection_scope():
        with connection(path, lambda: sqlite3.connect(path)) as conn:
            conn.close()
        with connection(path, lambda: sqlite3.connect(path)) as again:
            assert again is not conn
            again.execute("SELECT 1")


def test_unstatable_file_is_not_retained(tmp_path, monkeypatch):
    from hermes_cli.kanban_sync_connections import connection, connection_scope

    path = tmp_path / "db"
    original = Path.stat

    def denied(target, *args, **kwargs):
        if target == path:
            raise PermissionError("cannot stat")
        return original(target, *args, **kwargs)

    with connection_scope():
        with connection(path, lambda: sqlite3.connect(path)) as conn:
            monkeypatch.setattr(Path, "stat", denied)
        assert_closed(conn)


def test_scope_is_thread_local_and_nested(tmp_path):
    from hermes_cli.kanban_sync_connections import connection, connection_scope

    path = tmp_path / "db"

    def other_thread():
        with connection(path, lambda: sqlite3.connect(path)) as conn:
            conn.execute("SELECT 1")
        assert_closed(conn)
        return conn

    with connection_scope():
        with connection(path, lambda: sqlite3.connect(path)) as outer:
            pass
        with ThreadPoolExecutor(max_workers=1) as executor:
            assert executor.submit(other_thread).result() is not outer
        with connection_scope():
            with connection(path, lambda: sqlite3.connect(path)) as inner:
                assert inner is not outer
        assert_closed(inner)
        with connection(path, lambda: sqlite3.connect(path)) as again:
            assert again is outer
    assert_closed(outer)


def test_retention_resync_closes_shadow_before_unlink(board, opened, monkeypatch):
    path, hub = board
    original_pull = client.pull
    original_unlink = Path.unlink
    shadow = client.shadow_path("default")
    with client.connection_scope():
        client.sync_board("default", path, hub, "macbook")
        old_shadow = opened[1]

        def stale(conn, *args, **kwargs):
            if conn is old_shadow:
                raise client.Resync()
            return original_pull(conn, *args, **kwargs)

        def unlink(target, *args, **kwargs):
            if target == shadow:
                assert_closed(old_shadow)
            return original_unlink(target, *args, **kwargs)

        monkeypatch.setattr(client, "pull", stale)
        monkeypatch.setattr(Path, "unlink", unlink)
        client.sync_board("default", path, hub, "macbook")
        assert len(opened) == 3
        client.sync_board("default", path, hub, "macbook")
        assert len(opened) == 3
    for conn in opened:
        assert_closed(conn)


@pytest.mark.parametrize("failed", [False, True])
def test_run_forever_owns_and_closes_connections(board, opened, monkeypatch, failed):
    from hermes_cli import machine_metrics

    path, hub = board
    running = True
    monkeypatch.setattr(client, "load_identity", lambda: SimpleNamespace(gateway="macbook", url="fake"))
    monkeypatch.setattr(client, "Hub", lambda ident: hub)
    monkeypatch.setattr(client.sync, "set_lease_client", lambda value: None)
    monkeypatch.setattr(client, "board_paths", list)
    monkeypatch.setattr(client, "reconcile_links", lambda *args, **kwargs: None)
    monkeypatch.setattr(machine_metrics, "Reporter", lambda *args: SimpleNamespace(report_due=lambda: None))
    # FakeHub has no metrics transport; the loop only needs this for Reporter construction.
    monkeypatch.setattr(hub, "report_metrics", lambda *args: None, raising=False)

    def rounds(*args, **kwargs):
        client.sync_board("default", path, hub, "macbook")
        client.sync_board("default", path, hub, "macbook")
        assert len(opened) == 2
        if failed:
            raise OSError("loop failure")
        return []

    def stop(*args):
        nonlocal running
        running = False

    monkeypatch.setattr(client, "sync_all_once", rounds)
    monkeypatch.setattr(client, "wait_for_work", stop)
    with pytest.raises(OSError, match="loop failure") if failed else nullcontext():
        client.run_forever(lambda: running, interval=1)
    for conn in opened:
        assert_closed(conn)
    client.sync_board("default", path, hub, "macbook")
    for conn in opened:
        assert_closed(conn)
