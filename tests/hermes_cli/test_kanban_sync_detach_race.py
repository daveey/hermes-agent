"""An already-fetched service end cannot land after a native detach commits."""
from contextlib import contextmanager
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_sync as sync
from tests.hermes_cli import kanban_sync_support as support


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    return support.kanban_home(tmp_path, monkeypatch)


@pytest.fixture
def synced_conn(kanban_home):
    yield from support.synced_conn(kanban_home)


def test_detach_between_feed_precheck_and_write_preserves_custody(synced_conn, monkeypatch):
    conn = synced_conn
    tid = kb.create_task(conn, title="custody", assignee="worker")
    assert kb.claim_task(conn, tid)
    sync.set_mode(conn, "primary")
    path = sync._db_key(conn)
    assert path is not None
    with kbc.connect_closing(Path(path)) as detacher:
        real_txn = kbc.write_txn

        @contextmanager
        def detach_before_writer(target, *, allow_nested=False):
            # Real second connection commits detach in the old check/lock gap.
            with monkeypatch.context() as m:
                m.setattr(kbc, "write_txn", real_txn)
                sync.set_mode(detacher, "off")
            with real_txn(target, allow_nested=allow_nested):
                yield

        monkeypatch.setattr(kbc, "write_txn", detach_before_writer)
        before = [tuple(r) for r in conn.execute("SELECT * FROM task_runs")]
        change = sync.RemoteChange(999, "tasks", tid, "upsert",
                                   {"status": "ready", "current_run_uid": None}, 999,
                                   "9999999999999-0000-server", "server")
        with pytest.raises(RuntimeError, match="not enabled"):
            sync.apply_remote_changes(conn, [change], new_cursor=999)
        assert kb.get_task(conn, tid).status == "running"
        assert [tuple(r) for r in conn.execute("SELECT * FROM task_runs")] == before
        assert sync.sync_state(conn)["cursor"] != 999


def test_detach_refuses_until_whole_sync_round_including_push_finishes(synced_conn, monkeypatch):
    from hermes_cli import kanban_sync_client as client
    from tests.hermes_cli.kanban_sync_fake_board import FakeService

    conn = synced_conn
    path = Path(sync._db_key(conn))
    client.ensure_identity(conn, "default", "fixture-host")
    sync.set_mode(conn, "primary")
    kb.create_task(conn, title="pending publication", assignee="worker")
    hub = FakeService().hub("fixture-host")
    real_push = client.push
    reached = []
    with kbc.connect_closing(path) as detacher:
        def push_with_detach(*args, **kwargs):
            with pytest.raises(RuntimeError, match="sync round is busy"):
                sync.set_mode(detacher, "off")
            assert sync.board_mode(conn) == "primary"
            reached.append(True)
            return real_push(*args, **kwargs)

        monkeypatch.setattr(client, "push", push_with_detach)
        result = client.sync_board("default", path, hub, "fixture-host")
        assert reached and result.push.sent
        sync.set_mode(detacher, "off")
        # A round enumerated before detach but starting after it does no I/O.
        result = client.sync_board("default", path, None, "fixture-host")
        assert result.mode == "off"


def test_busy_round_is_scoped_to_one_board_and_next_pass_recovers(synced_conn, tmp_path):
    from hermes_cli import kanban_sync_client as client
    from hermes_cli.kanban_sync_guard import board_sync_lock
    from tests.hermes_cli.kanban_sync_fake_board import FakeService

    first = Path(sync._db_key(synced_conn))
    second = tmp_path / "second.db"
    client.ensure_identity(synced_conn, "first", "fixture-host")
    sync.set_mode(synced_conn, "primary")
    with kbc.connect_closing(second) as conn:
        client.ensure_identity(conn, "second", "fixture-host")
        sync.set_mode(conn, "primary")
    hub = FakeService().hub("fixture-host")
    boards = [("first", first), ("second", second)]
    with board_sync_lock(first):
        results = client.sync_all_once(hub, boards=boards)
    assert [r.board for r in results] == ["first", "second"]
    assert "busy" in results[0].error
    assert not results[1].error
    assert all(not r.error for r in client.sync_all_once(hub, boards=boards))
