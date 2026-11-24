"""Empty outbox observations skip schema opens, not positive/deadline work."""
from unittest.mock import Mock
import os
import shutil
import sqlite3

import pytest

from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_client as client
from hermes_cli import kanban_sync_outbox_age as age
from tests.hermes_cli import kanban_sync_support as support


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = support.kanban_home(tmp_path, monkeypatch)
    support.write_sync_config(home, True)
    path = home / "kanban.db"
    conn = kbc.connect(path)
    client.ensure_identity(conn, "default", "macbook")
    sync.set_mode(conn, "primary")
    conn.execute("DELETE FROM kanban_sync_outbox")
    yield path, conn
    conn.close()
    client.reset_board_watch()


def test_checkpoint_keeps_reader_and_answers_without_rearming_repair_generation(board):
    path, conn = board
    sql = "SELECT 1 FROM kanban_sync_outbox WHERE state='pending' LIMIT 1"
    first = client._WATCH.query(path, sql, unchanged_ok=True)
    assert first is not None and first[1] is None
    old = client._WATCH._conns[os.path.realpath(path)][1]
    _insert(conn)
    assert conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
    current = client._WATCH.query(path, sql, unchanged_ok=True)
    assert current == (first[0], (1,))
    # SQLite's wal-index proves the main file moved in a checkpoint: the reader is kept (no
    # schema re-parse) and still answers from the checkpointed content.
    assert client._WATCH._conns[os.path.realpath(path)][1] is old
    assert old.execute("SELECT 1").fetchone() == (1,)


def test_empty_sample_does_not_reopen_schema(board, monkeypatch):
    path, _conn = board
    opened = Mock(wraps=kbc.connect_closing)
    monkeypatch.setattr(kbc, "connect_closing", opened)
    assert age._sample(path) is None
    assert age._sample(path) is None
    assert opened.call_count == 0


def _insert(conn, op="op", retry=None):
    conn.execute(
        "INSERT INTO kanban_sync_outbox "
        "(op_id, table_name, row_uid, op, fields, hlc, created_at, retry_after) "
        "VALUES (?, 'tasks', 't_probe', 'upsert', '{}', '', 1000, ?)", (op, retry))


def test_empty_proof_sees_external_insert_and_delete(board, monkeypatch):
    path, conn = board
    monkeypatch.setattr(age, "_since", lambda created: float(created))
    monkeypatch.setattr(client.backlog, "behind", lambda path: False)
    assert age._sample(path) is None
    _insert(conn)
    assert age._sample(path) == ("op", 1000.0)
    conn.execute("DELETE FROM kanban_sync_outbox")
    assert age._sample(path) is None
    _insert(conn, "next")
    assert age._sample(path) == ("next", 1000.0)


def test_unknown_empty_proof_falls_back_to_original_read(board, monkeypatch):
    path, conn = board
    _insert(conn)
    monkeypatch.setattr(client.backlog, "behind", lambda path: False)
    monkeypatch.setattr(client._WATCH, "query", lambda *args, **kwargs: None)
    opened = Mock(wraps=kbc.connect_closing)
    monkeypatch.setattr(kbc, "connect_closing", opened)
    assert age._sample(path)[0] == "op"
    assert opened.call_count == 1


def test_pending_answers_are_never_used_as_empty_deadline_proof(board, monkeypatch):
    path, conn = board
    _insert(conn, retry=100)
    _insert(conn, "unanswered")
    now = [50]
    monkeypatch.setattr(age.time, "time", lambda: now[0])
    sendable = Mock(side_effect=lambda conn, hub, slug, rows, evidence: (rows, []))
    monkeypatch.setattr(client.backlog, "behind", lambda path: True)
    monkeypatch.setattr(client.backlog, "sendable_before_head", sendable)
    assert age._sample(path)[0] == "unanswered"
    assert [r["op_id"] for r in sendable.call_args.args[3]] == ["unanswered"]
    now[0] = 100
    assert age._sample(path)[0] == "unanswered"  # the deferred row's eligibility is recomputed, not cached
    assert [r["op_id"] for r in sendable.call_args.args[3]] == ["op", "unanswered"]
    assert sendable.call_count == 2


def test_rejected_to_pending_invalidates_empty_proof(board, monkeypatch):
    path, conn = board
    _insert(conn)
    conn.execute("UPDATE kanban_sync_outbox SET state='rejected'")
    monkeypatch.setattr(client.backlog, "behind", lambda path: False)
    assert age._sample(path) is None
    conn.execute("UPDATE kanban_sync_outbox SET state='pending'")
    assert age._sample(path)[0] == "op"


def test_closed_watch_handle_falls_back_then_recovers(board, monkeypatch):
    path, conn = board
    monkeypatch.setattr(client.backlog, "behind", lambda path: False)
    assert age._sample(path) is None
    # Keep the writer open: losing the watcher must not be masked by last-close
    # sidecar deletion or a main-file checkpoint.
    client._WATCH._conns[os.path.realpath(path)][1].close()
    _insert(conn)
    opened = Mock(wraps=kbc.connect_closing)
    monkeypatch.setattr(kbc, "connect_closing", opened)
    assert age._sample(path)[0] == "op"
    assert opened.call_count == 1
    assert age._sample(path)[0] == "op"


def test_empty_after_round_still_reads_fresh_awake_state(board, monkeypatch):
    path, _conn = board
    awake = Mock(side_effect=[True, False, True])
    monkeypatch.setattr(age.replica_age, "note_round", lambda *a, **kw: None)
    result = client.RoundResult(board="default", mode="primary")
    for _ in range(3):
        assert age.after_round([result], [("default", path)], awake_fn=awake) == []
    assert awake.call_count == 3


def test_same_inode_restore_cannot_reuse_empty_proof(board, monkeypatch):
    source, conn = board
    path = source.with_name("closed-empty.db")
    target = sqlite3.connect(path)
    conn.backup(target)
    target.execute("PRAGMA journal_mode=DELETE")
    target.close()
    image = path.with_name("restored.db")
    shutil.copyfile(path, image)
    with sqlite3.connect(image) as restored:
        _insert(restored)
    restored.close()
    # Both branches advance once from the same image: the header counter alone
    # cannot distinguish this replacement from the cached empty branch.
    with sqlite3.connect(path) as empty_branch:
        empty_branch.execute("UPDATE kanban_sync_state SET cursor=cursor+1")
    empty_branch.close()
    monkeypatch.setattr(client.backlog, "behind", lambda path: False)
    assert age._sample(path) is None
    before = path.stat()
    shutil.copyfile(image, path)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 2_000_000_000))
    assert path.stat().st_ino == before.st_ino
    sample = age._sample(path)
    assert sample is not None and sample[0] == "op"
