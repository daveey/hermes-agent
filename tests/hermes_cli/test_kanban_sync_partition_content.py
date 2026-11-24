"""Partition memo custody: eligibility is not evidence a skipped scan completed."""
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_sync_client as ksc
from hermes_cli import kanban_sync_partition_content as content
from hermes_cli import kanban_sync_partition_repair as repair
from hermes_cli.kanban_sync_pending import REPLICA_GUARD
from hermes_cli.kanban_sync_repair_revision import ensure_revision
from tests.hermes_cli import test_kanban_sync_repair_window as window

kanban_home = window.kanban_home
svc = window.svc
live = window.live
clock = window.clock


@pytest.fixture
def pair(live, svc, kanban_home):
    parent, child = window._settle(live, svc, kanban_home, "primary")
    replica = ksc.open_shadow(window.BOARD, window.ME)
    for conn in (live, replica):
        conn.execute("PRAGMA wal_autocheckpoint=0")
        ensure_revision(conn)
    path = kanban_home / "kanban.db"
    inputs = ksc._repair_inputs(path, replica, "primary", window.ME)
    content.reset()
    def prepare(**kwargs):
        return content.prepare(live, replica, path=path, inputs=inputs, **kwargs)
    def commit(decision):
        content.commit(decision, live, replica, inputs=inputs)
    try:
        yield SimpleNamespace(live=live, replica=replica, path=path, inputs=inputs,
                              parent=parent, child=child, prepare=prepare, commit=commit)
    finally:
        replica.close()
        content.reset()


def test_catchup_during_pass_cannot_cache_skipped_chat_repair(pair):
    # A replica-only chat is recoverable after pickup. No content writes happen
    # during the pass: the two cursor commits below represent event-only pulls.
    with kbc.write_txn(pair.live):
        pair.live.execute("UPDATE tasks SET created_on='foreign' WHERE id=?", (pair.parent,))
        pair.live.execute("DELETE FROM kanban_sync_outbox")
    with kbc.write_txn(pair.replica):
        pair.replica.execute(
            "INSERT INTO task_chat_links (uid,task_id,chat_gateway,chat_session,kind,created_at) "
            "VALUES ('recoverable', ?, ?, 'real-chat', 'shepherd', 1)", (pair.parent, window.ME))
        pair.replica.execute("UPDATE kanban_sync_state SET cursor=100")
    with kbc.write_txn(pair.live):
        pair.live.execute("UPDATE kanban_sync_state SET cursor=100")
    decision = pair.prepare()
    assert not decision.skip and decision.stamp is not None
    with kbc.write_txn(pair.live):
        pair.live.execute("UPDATE kanban_sync_state SET cursor=101")
    assert repair.repair_partition_chat_links(pair.live, pair.replica, window.ME) == []
    with kbc.write_txn(pair.replica):
        pair.replica.execute("UPDATE kanban_sync_state SET cursor=101")
    pair.commit(decision)
    next_decision = pair.prepare()
    assert not next_decision.skip
    assert repair.repair_partition_chat_links(pair.live, pair.replica, window.ME) == ["recoverable"]


def _warm(pair):
    decision = pair.prepare()
    assert not decision.skip and decision.stamp is not None
    pair.commit(decision)
    assert pair.prepare().skip


@pytest.mark.parametrize("side", ["live", "replica"])
@pytest.mark.parametrize("change", ["task", "link", "chat", "outbox", "tombstone", "omission", "trigger"])
def test_relevant_mutations_rearm_partition(pair, side, change):
    conn = getattr(pair, side)
    if side == "replica" and change == "outbox":  # only a file from before the guard holds replica ops
        with kbc.write_txn(conn):
            conn.execute(f"DROP TRIGGER {REPLICA_GUARD}")
    _warm(pair)
    statements = {
        "task": "UPDATE tasks SET title='changed' WHERE id=?",
        "link": "DELETE FROM task_links WHERE parent_id=?",
        "chat": "INSERT INTO task_chat_links "
                "(uid,task_id,chat_gateway,chat_session,kind,created_at) "
                "VALUES ('chat', ?, 'peer', 'session', 'shepherd', 1)",
        "outbox": "INSERT INTO kanban_sync_outbox "
                  "(op_id,table_name,row_uid,op,fields,hlc,created_at,state) "
                  "VALUES ('held','tasks',?,'delete','{}','',1,'pending')",
        "tombstone": "INSERT INTO kanban_sync_tombstones VALUES ('tasks', ?, 1)",
        "omission": "INSERT INTO kanban_sync_omissions "
                    "(original_op_id,table_name,row_uid,field,compat_op_id) "
                    "VALUES ('original','tasks',?,'title','compat')",
        "trigger": "DROP TRIGGER kanban_repair_tasks_update",
    }
    with kbc.write_txn(conn):
        pulled = side == "replica" and change == "tombstone"  # a replica tombstone is a pulled one
        if pulled:
            conn.execute("UPDATE kanban_sync_state SET applying = 1")
        conn.execute(statements[change], () if change == "trigger" else (pair.parent,))
        if pulled:
            conn.execute("UPDATE kanban_sync_state SET applying = 0")
    assert not pair.prepare().skip


@pytest.mark.parametrize("side", ["live", "replica"])
def test_transaction_rollback_evicts_memo(pair, side):
    _warm(pair)
    conn = getattr(pair, side)
    conn.execute("BEGIN")
    try:
        decision = pair.prepare()
        assert not decision.skip and decision.stamp is None
        pair.commit(decision)
        assert not pair.prepare().skip
    finally:
        conn.rollback()
    assert not pair.prepare().skip


def test_cursor_lag_then_catchup_without_revision_change(pair):
    _warm(pair)
    cursor = pair.replica.execute("SELECT cursor FROM kanban_sync_state").fetchone()[0]
    with kbc.write_txn(pair.live):
        pair.live.execute("UPDATE kanban_sync_state SET cursor=?", (cursor + 1,))
    decision = pair.prepare()
    assert not decision.skip and decision.stamp is None
    pair.commit(decision)
    with kbc.write_txn(pair.replica):
        pair.replica.execute("UPDATE kanban_sync_state SET cursor=?", (cursor + 1,))
    _warm(pair)
    # Cursor-only movement between successful passes retains the semantic key.
    for conn in (pair.replica, pair.live):
        with kbc.write_txn(conn):
            conn.execute("UPDATE kanban_sync_state SET cursor=?", (cursor + 2,))
    assert pair.prepare().skip


@pytest.mark.parametrize("side", ["live", "replica"])
@pytest.mark.parametrize("other_connection", [False, True])
def test_pre_scan_stamp_rearms_for_own_or_concurrent_write(pair, side, other_connection):
    from pathlib import Path
    conn = getattr(pair, side)
    writer = kbc.connect(Path(conn.execute("PRAGMA database_list").fetchone()[2])) if other_connection else conn
    decision = pair.prepare()
    try:
        with kbc.write_txn(writer):
            writer.execute("UPDATE tasks SET title='during pass' WHERE id=?", (pair.parent,))
        pair.commit(decision)
        assert not pair.prepare().skip
    finally:
        if other_connection:
            writer.close()


@pytest.mark.parametrize("side", ["live", "replica"])
@pytest.mark.parametrize("field,value", [("cursor", "bad"), ("cursor", -1), ("mode", "bad")])
def test_unknown_cursor_or_mode_never_caches(pair, side, field, value):
    _warm(pair)
    with kbc.write_txn(getattr(pair, side)):
        getattr(pair, side).execute(f"UPDATE kanban_sync_state SET {field}=?", (value,))
    for _ in range(2):
        decision = pair.prepare()
        assert not decision.skip and decision.stamp is None
        pair.commit(decision)


def test_rebuilt_reset_and_lru_bound(pair, monkeypatch):
    _warm(pair)
    assert not pair.prepare(rebuilt=True).skip
    pair.commit(pair.prepare())
    monkeypatch.setattr(content, "_LIMIT", 2)
    for path in ("other-a", "other-b"):
        decision = content.prepare(pair.live, pair.replica, path=path, inputs=pair.inputs)
        pair.commit(decision)
    assert len(content._done) == 2
    assert not pair.prepare().skip
    pair.commit(pair.prepare())
    ksc.reset_board_watch()
    assert not content._done


@pytest.mark.parametrize("name", ["repair_partition_links", "repair_partition_chat_links",
                                  "retire_lost_placeholder_links", "repair_divergence"])
def test_sync_scan_exception_does_not_publish_partition_success(
    live, svc, kanban_home, monkeypatch, clock, name,
):
    from hermes_cli.kanban_sync_connections import connection_scope
    with connection_scope():
        window._settle(live, svc, kanban_home, "primary")
        content.reset()
        clock[0] += ksc.REPLICA_REPAIR_MIN_S + 1
        window._remote(svc)
        owner = repair if hasattr(repair, name) else ksc
        real = getattr(owner, name)
        def broken(*args, **kwargs):
            raise RuntimeError("partition scan failed")
        monkeypatch.setattr(owner, name, broken)
        with pytest.raises(RuntimeError, match="partition scan failed"):
            window._round(kanban_home, svc)
        assert str(kanban_home / "kanban.db") not in content._done
        monkeypatch.setattr(owner, name, real)
        calls = window._spy(monkeypatch, ("repair_partition_links", "repair_partition_chat_links",
                                         "retire_lost_placeholder_links"))
        assert window._round(kanban_home, svc).error is None
        assert all(count == 1 for count in calls.values())


@pytest.mark.parametrize("barrier", ["pending", "rejected", "tombstone"])
def test_cached_barrier_removal_restores_chat_without_echo(pair, barrier):
    with kbc.write_txn(pair.live):
        pair.live.execute("UPDATE tasks SET created_on='foreign' WHERE id=?", (pair.parent,))
        pair.live.execute("DELETE FROM kanban_sync_outbox")
        pair.live.execute("UPDATE kanban_sync_state SET cursor=100")
        if barrier == "tombstone":
            pair.live.execute("INSERT INTO kanban_sync_tombstones VALUES ('task_chat_links','held-chat',1)")
        else:
            pair.live.execute("INSERT INTO kanban_sync_outbox "
                              "(op_id,table_name,row_uid,op,fields,hlc,created_at,state) "
                              "VALUES ('held','task_chat_links','held-chat','delete','{}','',1,?)", (barrier,))
    with kbc.write_txn(pair.replica):
        pair.replica.execute("UPDATE kanban_sync_state SET cursor=100")
        pair.replica.execute("INSERT INTO task_chat_links "
                             "(uid,task_id,chat_gateway,chat_session,kind,created_at) "
                             "VALUES ('held-chat',?,?,'real-chat','shepherd',1)", (pair.parent, window.ME))
    decision = pair.prepare()
    assert repair.repair_partition_chat_links(pair.live, pair.replica, window.ME) == []
    pair.commit(decision)
    assert pair.prepare().skip
    with kbc.write_txn(pair.live):
        if barrier == "tombstone":
            pair.live.execute("DELETE FROM kanban_sync_tombstones")
        else:
            pair.live.execute("DELETE FROM kanban_sync_outbox")
    assert not pair.prepare().skip
    assert repair.repair_partition_chat_links(pair.live, pair.replica, window.ME) == ["held-chat"]
    assert not pair.live.execute("SELECT * FROM kanban_sync_outbox").fetchall()


@pytest.mark.parametrize("side", ["live", "replica"])
def test_new_handle_and_checkpoint_cannot_reuse_memo(pair, side):
    from pathlib import Path
    _warm(pair)
    conn = getattr(pair, side)
    other = kbc.connect(Path(conn.execute("PRAGMA database_list").fetchone()[2]))
    try:
        args = [pair.live, pair.replica]
        args[0 if side == "live" else 1] = other
        assert not content.prepare(*args, path=pair.path, inputs=pair.inputs).skip
    finally:
        other.close()
    with kbc.write_txn(conn):
        conn.execute("CREATE TABLE checkpoint_payload(value TEXT)")
        conn.execute("INSERT INTO checkpoint_payload VALUES (?)", ("x" * 10000,))
    pair.commit(pair.prepare())
    assert pair.prepare().skip
    assert conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
    assert not pair.prepare().skip


@pytest.mark.parametrize("change", ["row", "table", "corrupt"])
def test_unknown_live_revision_never_publishes(pair, change):
    _warm(pair)
    with kbc.write_txn(pair.live):
        if change == "corrupt":
            pair.live.execute("UPDATE kanban_repair_revision SET revision='unknown'")
        else:
            pair.live.execute("DELETE FROM kanban_repair_revision" if change == "row"
                              else "DROP TABLE kanban_repair_revision")
    for _ in range(2):
        decision = pair.prepare()
        assert not decision.skip and decision.stamp is None
        pair.commit(decision)


@pytest.mark.parametrize("index,value", [(2, "shadow"), (3, "other"), (4, '{"policy":1}')])
def test_scope_change_never_reuses_memo(pair, index, value):
    _warm(pair)
    inputs = list(pair.inputs)
    inputs[index] = value
    assert not content.prepare(pair.live, pair.replica, path=pair.path, inputs=inputs).skip


@pytest.mark.parametrize("side", ["live", "replica"])
def test_trigger_reinstallation_tracks_subsequent_write(pair, side):
    conn = getattr(pair, side)
    _warm(pair)
    with kbc.write_txn(conn):
        conn.execute("DROP TRIGGER kanban_repair_tasks_update")
    assert not pair.prepare().skip
    ensure_revision(conn)
    pair.commit(pair.prepare())
    assert pair.prepare().skip
    with kbc.write_txn(conn):
        conn.execute("UPDATE tasks SET title='after reinstall' WHERE id=?", (pair.parent,))
    assert not pair.prepare().skip


def test_post_scan_cursor_ineligible_never_publishes(pair):
    decision = pair.prepare()
    with kbc.write_txn(pair.live):
        pair.live.execute("UPDATE kanban_sync_state SET cursor=cursor+100")
    pair.commit(decision)
    assert str(pair.path) not in content._done


@pytest.mark.parametrize("side", ["live", "replica"])
@pytest.mark.parametrize("replace", [False, True])
def test_same_content_file_rewrite_or_replacement_rearms(pair, side, replace):
    from pathlib import Path
    conn = getattr(pair, side)
    assert conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
    path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    if not replace:
        # Filesystems can coalesce ctime updates within one timestamp tick.
        # Give the real rewrite an observably different mtime before caching;
        # no sleeps, fake stat values, or weakened main-file fence are needed.
        import os
        os.utime(path, ns=(path.stat().st_atime_ns, 1_000_000_000))
    _warm(pair)
    before = path.stat()
    if replace:
        # The retained handle still sees the old inode: pathname fencing must
        # nevertheless reject its memo before the next opener also fences it.
        copy = path.with_suffix(".replacement")
        copy.write_bytes(path.read_bytes())
        copy.replace(path)
        assert path.stat().st_ino != before.st_ino
    else:
        with path.open("r+b") as handle:
            data = handle.read()
            handle.seek(0)
            handle.write(data)
            handle.flush()
        assert path.stat().st_ino == before.st_ino
        assert path.stat().st_mtime_ns != before.st_mtime_ns
    assert not pair.prepare().skip


def test_replica_rebuild_with_retained_handles_runs_partition_scans(
    live, svc, kanban_home, monkeypatch, clock,
):
    from hermes_cli.kanban_sync_connections import connection_scope
    with connection_scope():
        window._settle(live, svc, kanban_home, "primary")
        calls = window._spy(monkeypatch, ("repair_partition_links", "repair_partition_chat_links",
                                         "retire_lost_placeholder_links"))
        window._remote(svc)
        svc.compact(window.BOARD)
        result = window._round(kanban_home, svc)
        assert result.error is None and not result.repair_deferred
        assert all(count == 1 for count in calls.values())


@pytest.mark.parametrize("inputs", [(None, None, "primary", "local"),
                                   (None, None, "shadow", "local", "{}")])
def test_short_or_disagreeing_inputs_cannot_publish(pair, inputs):
    decision = content.prepare(pair.live, pair.replica, path=pair.path, inputs=inputs)
    assert decision.stamp is None and not decision.skip
    content.commit(decision, pair.live, pair.replica, inputs=inputs)
    assert not content._done


def test_prepare_installs_missing_replica_revision_before_snapshot(pair):
    with kbc.write_txn(pair.replica):
        pair.replica.execute("DROP TABLE kanban_repair_revision")
    decision = pair.prepare()
    assert decision.stamp is not None and not decision.skip
    pair.commit(decision)
    assert pair.prepare().skip


def test_unknown_decision_cannot_publish_after_state_recovers(pair):
    decision = content.prepare(pair.live, pair.replica, path=pair.path, inputs=None)
    pair.commit(decision)
    assert not content._done


def test_skipped_decision_cannot_republish_after_reset(pair):
    _warm(pair)
    decision = pair.prepare()
    assert decision.skip
    content.reset()
    pair.commit(decision)
    assert not content._done


def test_memory_store_is_not_a_file_fenced_snapshot(pair):
    import sqlite3
    from contextlib import closing
    with closing(sqlite3.connect(":memory:")) as memory:
        pair.live.backup(memory)
        assert content._snapshot(memory) is None


def test_unknown_inputs_and_snapshot_error_evict(pair, monkeypatch):
    import sqlite3
    _warm(pair)
    decision = content.prepare(pair.live, pair.replica, path=pair.path, inputs=None)
    assert not decision.skip and decision.stamp is None
    _warm(pair)
    real = content._snapshot
    def broken(conn):
        raise sqlite3.OperationalError("cannot observe")
    monkeypatch.setattr(content, "_snapshot", broken)
    decision = pair.prepare()
    assert not decision.skip and decision.stamp is None
    monkeypatch.setattr(content, "_snapshot", real)
    _warm(pair)
    decision = pair.prepare(rebuilt=True)
    monkeypatch.setattr(content, "_snapshot", broken)
    pair.commit(decision)
    monkeypatch.setattr(content, "_snapshot", real)
    assert not pair.prepare().skip
