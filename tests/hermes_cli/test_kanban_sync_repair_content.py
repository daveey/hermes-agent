"""Cursor-only feed arrivals must not repeat content repair scans."""
from __future__ import annotations

from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_sync_repair_content as content
from hermes_cli.kanban_sync_repair_revision import ensure_revision

from hermes_cli import kanban_sync_client as ksc
from hermes_cli.kanban_sync_connections import connection_scope
from tests.hermes_cli import test_kanban_sync_repair_window as window

kanban_home = window.kanban_home
svc = window.svc
live = window.live
clock = window.clock


@pytest.mark.parametrize("mode", ["shadow", "primary"])
def test_event_only_feed_skips_content_and_partition_repairs(
    live, svc, kanban_home, monkeypatch, clock, mode,
):
    # Checkpoints deliberately invalidate file identity. This case isolates
    # event-only WAL writes; checkpoint invalidation is exercised separately.
    connect = kbc.connect
    def no_autocheckpoint(*args, **kwargs):
        conn = connect(*args, **kwargs)
        conn.execute("PRAGMA wal_autocheckpoint=0")
        return conn
    monkeypatch.setattr(kbc, "connect", no_autocheckpoint)
    live.execute("PRAGMA wal_autocheckpoint=0")
    with connection_scope():
        parent, _ = window._settle(live, svc, kanban_home, mode)
        window._round(kanban_home, svc)
        # The settle push's own acknowledgement is credited (kanban_sync_own_writes), so its round deferred:
        # let that deferred pass run, so the content stamp is current before the event-only round.
        clock[0] += ksc.REPLICA_REPAIR_MIN_S + 1
        window._round(kanban_home, svc)
        calls = window._spy(monkeypatch, (
            "repair_divergence", "repair_missing_children", "repair_partition_links",
            "repair_partition_chat_links", "retire_lost_placeholder_links",
        ))
        assert window._round(kanban_home, svc).error is None
        assert not any(calls.values())  # Warm unchanged ticks also do no scans.
        refreshes = []
        refresh = window.sync.refresh_remote_parents
        def refreshed(*args, **kwargs):
            refreshes.append(True)
            return refresh(*args, **kwargs)
        monkeypatch.setattr(window.sync, "refresh_remote_parents", refreshed)
        from hermes_cli import kanban_sync_parent_refresh as parent_gate
        rows = parent_gate._refresh_rows
        def row_refreshed(*args, **kwargs):
            refreshes.append(True)  # the keyed path re-reads just the moved parent
            return rows(*args, **kwargs)
        monkeypatch.setattr(parent_gate, "_refresh_rows", row_refreshed)
        clock[0] += ksc.REPLICA_REPAIR_MIN_S + 1
        window._remote(svc, table="task_events", uid="event-foreign", fields={
            "task_id": parent, "kind": "commented", "created_at": 1, "payload": "{}",
        })
        result = window._round(kanban_home, svc)
        assert result.error is None and result.pulled > 0
        assert not result.repair_deferred
        assert calls == {
            "repair_divergence": 0, "repair_missing_children": 0,
            # Event cursors do not change eligible partition repair content.
            "repair_partition_links": 0, "repair_partition_chat_links": 0,
            "retire_lost_placeholder_links": 0,
        }
        assert refreshes == ([True] if mode == "primary" else [])
        # A relevant local edit is visible on the very next tick, even inside
        # the outer repair window that just admitted the event-only pass.
        with kbc.write_txn(live):
            live.execute("UPDATE tasks SET title='next tick' WHERE id=?", (parent,))
        assert not window._round(kanban_home, svc).repair_deferred
        assert all(count == 1 for count in calls.values())
        assert svc.board(window.BOARD)["rows"]["tasks"][parent]["title"] == "next tick"


@pytest.fixture
def pair(live, svc, kanban_home, monkeypatch):
    parent, child = window._settle(live, svc, kanban_home, "shadow")
    replica = ksc.open_shadow(window.BOARD, window.ME)
    ensure_revision(live)
    ensure_revision(replica)
    path = kanban_home / "kanban.db"
    inputs = ksc._repair_inputs(path, replica, "shadow", window.ME)
    assert inputs is not None
    content.reset()
    calls = window._spy(monkeypatch, ("repair_divergence", "repair_missing_children"))

    def run(**kwargs):
        return content.run(live, replica, window.ME, path=path, inputs=inputs, **kwargs)

    for _ in range(3):
        run()
    calls.update({name: 0 for name in calls})
    assert run() == {}
    assert not any(calls.values())
    try:
        yield SimpleNamespace(live=live, replica=replica, path=path, inputs=inputs,
                              parent=parent, child=child, calls=calls, run=run)
    finally:
        replica.close()
        content.reset()


@pytest.mark.parametrize("side", ["live", "replica"])
@pytest.mark.parametrize("change", ["task", "link", "schema", "trigger"])
def test_relevant_changes_rearm(pair, side, change):
    conn = getattr(pair, side)
    with kbc.write_txn(conn):
        if change == "task":
            conn.execute("UPDATE tasks SET title = 'changed' WHERE id = ?", (pair.parent,))
        elif change == "link":
            conn.execute("DELETE FROM task_links WHERE parent_id = ?", (pair.parent,))
        elif change == "schema":
            conn.execute("CREATE TABLE extra_schema (id INTEGER)")
        else:
            conn.execute("DROP TRIGGER kanban_repair_tasks_update")
            conn.execute("UPDATE tasks SET title = 'capture gap' WHERE id = ?", (pair.parent,))
    pair.run()
    assert pair.calls == {"repair_divergence": 1, "repair_missing_children": 1}


@pytest.mark.parametrize("change", ["insert", "update", "delete"])
def test_omission_only_change_rearms_content(pair, change):
    pair.live.execute("PRAGMA wal_autocheckpoint=0")
    with kbc.write_txn(pair.live):
        pair.live.execute(
            "INSERT INTO kanban_sync_omissions "
            "(original_op_id,table_name,row_uid,field,compat_op_id) VALUES (?,?,?,?,?)",
            ("original", "tasks", pair.parent, "title", "compat"))
    pair.run()
    pair.run()
    before = dict(pair.calls)
    with kbc.write_txn(pair.live):
        if change == "insert":
            pair.live.execute(
                "INSERT INTO kanban_sync_omissions "
                "(original_op_id,table_name,row_uid,field,compat_op_id) VALUES (?,?,?,?,?)",
                ("other", "tasks", pair.parent, "body", "other-compat"))
        elif change == "update":
            pair.live.execute("UPDATE kanban_sync_omissions SET state='delivered'")
        else:
            pair.live.execute("DELETE FROM kanban_sync_omissions")
    pair.run()
    assert pair.calls == {name: count + 1 for name, count in before.items()}


def test_checkpoint_rearms_content_without_revision_change(pair):
    pair.replica.execute("PRAGMA wal_autocheckpoint=0")
    with kbc.write_txn(pair.replica):
        pair.replica.execute("CREATE TABLE checkpoint_payload (value TEXT)")
        pair.replica.execute("INSERT INTO checkpoint_payload VALUES (?)", ("x" * 10000,))
    pair.run()
    pair.run()
    before = dict(pair.calls)
    revision = pair.replica.execute("SELECT revision FROM kanban_repair_revision").fetchone()[0]
    assert pair.replica.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
    assert pair.replica.execute("SELECT revision FROM kanban_repair_revision").fetchone()[0] == revision
    pair.run()
    assert pair.calls == {name: count + 1 for name, count in before.items()}


@pytest.mark.parametrize("side", ["live", "replica"])
def test_uncommitted_then_rollback_never_caches(pair, side):
    conn = getattr(pair, side)
    conn.execute("BEGIN")
    try:
        for _ in range(2):
            if side == "live":
                # Preserve the original scans' refusal to nest write_txn.
                with pytest.raises(RuntimeError, match="already inside a transaction"):
                    pair.run()
            else:
                pair.run()
        assert pair.calls == {"repair_divergence": 2, "repair_missing_children": 2}
    finally:
        conn.rollback()
    pair.run()
    assert pair.calls == {"repair_divergence": 3, "repair_missing_children": 3}


@pytest.mark.parametrize("field", ["mode", "gateway", "config"])
def test_outer_scope_changes_rearm(pair, field):
    inputs = list(pair.inputs)
    inputs[{"mode": 2, "gateway": 3, "config": 4}[field]] = "different"
    content.run(pair.live, pair.replica, window.ME, path=pair.path, inputs=tuple(inputs))
    assert pair.calls == {"repair_divergence": 1, "repair_missing_children": 1}


def test_rebuilt_and_unknown_inputs_always_scan(pair):
    pair.run(rebuilt=True)
    for _ in range(2):
        content.run(pair.live, pair.replica, window.ME, path=pair.path, inputs=None)
    assert pair.calls == {"repair_divergence": 3, "repair_missing_children": 3}


def test_revision_error_evicts_and_retries(pair, monkeypatch):
    real = content.ensure_revision
    def broken(_conn):
        raise sqlite3.OperationalError("transient read failure")
    monkeypatch.setattr(content, "ensure_revision", broken)
    pair.run()
    pair.run()
    assert pair.calls == {"repair_divergence": 2, "repair_missing_children": 2}
    monkeypatch.setattr(content, "ensure_revision", real)
    pair.run()
    pair.run()
    assert pair.calls == {"repair_divergence": 3, "repair_missing_children": 3}


def test_second_scan_error_cannot_publish_success(pair, monkeypatch):
    real = ksc.repair_missing_children
    def broken(*_args):
        raise sqlite3.OperationalError("scan failed")
    monkeypatch.setattr(ksc, "repair_missing_children", broken)
    with pytest.raises(sqlite3.OperationalError, match="scan failed"):
        pair.run(rebuilt=True)
    monkeypatch.setattr(ksc, "repair_missing_children", real)
    pair.run()
    assert pair.calls == {"repair_divergence": 2, "repair_missing_children": 1}


@pytest.mark.parametrize("side", ["live", "replica"])
def test_concurrent_commit_during_scan_is_seen_next_pass(pair, monkeypatch, side):
    target = getattr(pair, side)
    filename = target.execute("PRAGMA database_list").fetchone()[2]
    other = kbc.connect(Path(filename))
    real = ksc.repair_missing_children
    def changed(*args):
        real(*args)
        with kbc.write_txn(other):
            other.execute("UPDATE tasks SET title = title WHERE id = ?", (pair.parent,))
    monkeypatch.setattr(ksc, "repair_missing_children", changed)
    try:
        pair.run(rebuilt=True)
        monkeypatch.setattr(ksc, "repair_missing_children", real)
        pair.run()
        assert pair.calls == {"repair_divergence": 2, "repair_missing_children": 2}
    finally:
        other.close()


def test_reset_and_lru_bound_force_rescan(pair, monkeypatch):
    monkeypatch.setattr(content, "_LIMIT", 2)
    for name in ("other-a", "other-b"):
        content.run(pair.live, pair.replica, window.ME, path=name, inputs=pair.inputs)
    assert len(content._done) == 2
    pair.run()
    assert pair.calls == {"repair_divergence": 3, "repair_missing_children": 3}
    ksc.reset_board_watch()
    assert not content._done
    pair.run()
    assert pair.calls == {"repair_divergence": 4, "repair_missing_children": 4}


def test_trigger_reinstallation_observes_the_following_write(pair):
    with kbc.write_txn(pair.replica):
        pair.replica.execute("DROP TRIGGER kanban_repair_tasks_update")
    pair.run()
    pair.run()
    before = dict(pair.calls)
    with kbc.write_txn(pair.replica):
        pair.replica.execute("UPDATE tasks SET title = title WHERE id = ?", (pair.parent,))
    pair.run()
    assert pair.calls == {name: count + 1 for name, count in before.items()}


def test_missing_replica_trigger_inside_transaction_preserves_scan(pair):
    pair.replica.execute("BEGIN")
    try:
        pair.replica.execute("DROP TRIGGER kanban_repair_tasks_update")
        pair.run()
        pair.run()
        assert pair.calls == {"repair_divergence": 2, "repair_missing_children": 2}
    finally:
        pair.replica.rollback()


@pytest.mark.parametrize("change", ["row", "table"])
def test_unknown_live_revision_never_caches(pair, change):
    with kbc.write_txn(pair.live):
        pair.live.execute("DELETE FROM kanban_repair_revision" if change == "row"
                          else "DROP TABLE kanban_repair_revision")
    pair.run()
    pair.run()
    assert pair.calls == {"repair_divergence": 2, "repair_missing_children": 2}


@pytest.mark.parametrize("side", ["live", "replica"])
def test_new_handle_cannot_inherit_old_handle_stamp(pair, side):
    conn = getattr(pair, side)
    other = kbc.connect(Path(conn.execute("PRAGMA database_list").fetchone()[2]))
    try:
        args = [pair.live, pair.replica]
        args[0 if side == "live" else 1] = other
        # No file/schema/revision change: ONLY connection identity differs.
        content.run(args[0], args[1], window.ME, path=pair.path, inputs=pair.inputs)
        assert pair.calls == {"repair_divergence": 1, "repair_missing_children": 1}
    finally:
        other.close()


def test_main_file_rewrite_with_same_identity_and_counter_rearms(pair, monkeypatch):
    path = Path(pair.replica.execute("PRAGMA database_list").fetchone()[2])
    pair.replica.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    pair.run()
    pair.run()
    before = dict(pair.calls)
    stat = path.stat()
    # Rewrite identical bytes through a real descriptor. No inode, schema,
    # revision or size change; pin mtime to isolate the non-restorable ctime.
    with path.open("r+b") as handle:
        data = handle.read()
        handle.seek(0)
        handle.write(data)
        handle.flush()
    actual = path.stat()
    assert actual.st_ino == stat.st_ino and actual.st_ctime_ns != stat.st_ctime_ns
    real_stat = Path.stat
    def stamp(p, *args, **kwargs):
        got = real_stat(p, *args, **kwargs)
        if p == path:
            return SimpleNamespace(st_dev=got.st_dev, st_ino=got.st_ino, st_size=got.st_size,
                                   st_mtime_ns=stat.st_mtime_ns, st_ctime_ns=got.st_ctime_ns)
        return got
    monkeypatch.setattr(Path, "stat", stamp)
    pair.run()
    assert pair.calls == {name: count + 1 for name, count in before.items()}
