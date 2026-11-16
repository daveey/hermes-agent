"""Attachment retry scheduling keeps byte verification without idle write churn."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_sync_attachments as attachments
from tests.hermes_cli import test_kanban_sync_attachments as helpers

stores = helpers.stores


def test_empty_transfer_with_existing_retry_table_is_read_only(stores):
    _, _, _, conn, service, _, _ = stores
    attachments._setup(conn)
    statements = []
    conn.set_trace_callback(statements.append)
    try:
        assert attachments.transfer_pending(conn, service.hub("worker"), "default") == 0
    finally:
        conn.set_trace_callback(None)
    assert not [s for s in statements if s.upper().startswith("BEGIN")]


def _verified_row(conn, tid, data=b"proof"):
    aid = kb.store_attachment_bytes(conn, tid, "receipt", data)
    with kbc.write_txn(conn):
        conn.execute("UPDATE task_attachments SET blob_key=? WHERE id=?",
                     ("sha256:" + hashlib.sha256(data).hexdigest(), aid))
    return dict(conn.execute("SELECT * FROM task_attachments WHERE id=?", (aid,)).fetchone())


def test_unchanged_valid_file_is_hashed_without_record_transaction(stores, monkeypatch):
    _, _, _, conn, service, tid, _ = stores
    row = _verified_row(conn, tid)
    hashed = []
    valid = attachments._valid

    def observe(data, key):
        hashed.append((data, key))
        valid(data, key)

    monkeypatch.setattr(attachments, "_valid", observe)
    monkeypatch.setattr(helpers.BlobHub, "get_attachment_blob",
                        lambda *args: pytest.fail("valid bytes must not be downloaded"))
    statements = []
    conn.set_trace_callback(statements.append)
    try:
        assert attachments._transfer(conn, service.hub("worker"), "default", row)
    finally:
        conn.set_trace_callback(None)
    assert hashed == [(b"proof", row["blob_key"])]
    assert not [s for s in statements if s.upper().startswith("BEGIN")]


def test_retry_stamps_share_one_transaction_for_mixed_results(stores, monkeypatch):
    _, _, _, conn, service, tid, _ = stores
    verified = _verified_row(conn, tid)
    pending_id = kb.store_attachment_bytes(conn, tid, "pending", b"proof")
    with kbc.write_txn(conn):
        conn.execute("UPDATE task_attachments SET stored_path='' WHERE id=?", (pending_id,))
    failed = _verified_row(conn, tid, b"other")
    Path(failed["stored_path"]).unlink()
    attachments._setup(conn)
    monkeypatch.setattr(attachments, "_now", lambda: 1000)

    def offline(*args):
        raise OSError("offline")

    monkeypatch.setattr(helpers.BlobHub, "get_attachment_blob", offline)
    statements = []
    conn.set_trace_callback(statements.append)
    try:
        assert attachments.transfer_pending(conn, service.hub("worker"), "default", limit=3) == 1
    finally:
        conn.set_trace_callback(None)
    receipts = {r["uid"]: dict(r) for r in conn.execute("SELECT * FROM kanban_attachment_retry")}
    pending_uid = conn.execute("SELECT uid FROM task_attachments WHERE id=?", (pending_id,)).fetchone()[0]
    assert set(receipts) == {verified["uid"], pending_uid, failed["uid"]}
    assert receipts[verified["uid"]]["retry_at"] == 1600
    assert receipts[pending_uid]["retry_at"] == receipts[failed["uid"]]["retry_at"] == 1030
    assert receipts[failed["uid"]]["error"] == "OSError"
    assert receipts[pending_uid]["error"] is None
    assert {r["attempts"] for r in receipts.values()} == {1}
    assert len([s for s in statements if s.upper().startswith("BEGIN")]) == 1
    statements.clear()
    conn.set_trace_callback(statements.append)
    try:
        assert attachments.transfer_pending(conn, service.hub("worker"), "default", limit=3) == 0
    finally:
        conn.set_trace_callback(None)
    assert not [s for s in statements if s.upper().startswith("BEGIN")]


def test_record_rereads_metadata_cleared_after_snapshot(stores):
    _, worker, _, conn, service, tid, _ = stores
    row = _verified_row(conn, tid)
    other = kbc.connect(worker / "kanban.db")
    try:
        with kbc.write_txn(other):
            other.execute("UPDATE task_attachments SET stored_path='' WHERE uid=?", (row["uid"],))
        assert attachments._transfer(conn, service.hub("worker"), "default", row)
    finally:
        other.close()
    current = conn.execute("SELECT * FROM task_attachments WHERE uid=?", (row["uid"],)).fetchone()
    assert current["stored_path"] == row["stored_path"]
    assert current["blob_key"] == row["blob_key"]
    assert conn.execute("SELECT applying FROM kanban_sync_state").fetchone()[0] == 0


@pytest.mark.parametrize("change", ["deleted", "task_id", "size", "blob_key"])
@pytest.mark.parametrize("timing", ["before_read", "before_write"])
def test_record_never_overwrites_deleted_or_rebound_row(stores, monkeypatch, change, timing):
    _, worker, _, conn, _, tid, _ = stores
    row = _verified_row(conn, tid)
    other_tid = kb.create_task(conn, title="other owner", assignee="worker")
    replacement = {"task_id": other_tid, "size": row["size"] + 1,
                   "blob_key": "sha256:" + "0" * 64}
    other = kbc.connect(worker / "kanban.db")

    def change_row():
        with kbc.write_txn(other):
            if change == "deleted":
                other.execute("DELETE FROM task_attachments WHERE uid=?", (row["uid"],))
            else:
                other.execute(f"UPDATE task_attachments SET {change}=? WHERE uid=?",
                              (replacement[change], row["uid"]))

    @contextmanager
    def race_before_write(target):
        change_row()
        with kbc.write_txn(target):
            yield

    try:
        if timing == "before_read":
            change_row()
        else:
            monkeypatch.setattr(attachments, "write_txn", race_before_write)
        # A different path requires a write, allowing the race after the read.
        attachments._record_local(conn, row, Path(row["stored_path"]).with_name("new"), row["blob_key"])
    finally:
        other.close()
    current = conn.execute("SELECT * FROM task_attachments WHERE uid=?", (row["uid"],)).fetchone()
    if change == "deleted":
        assert current is None
    else:
        assert current[change] == replacement[change]
        assert current["stored_path"] == row["stored_path"]
    assert conn.execute("SELECT applying FROM kanban_sync_state").fetchone()[0] == 0


@pytest.mark.parametrize("damage", ["deleted", "corrupted"])
def test_due_retry_repairs_without_feed_and_not_before_deadline(stores, monkeypatch, damage):
    _, _, _, conn, service, tid, _ = stores
    row = _verified_row(conn, tid)
    hub = service.hub("worker")
    clock = [1000]
    monkeypatch.setattr(attachments, "_now", lambda: clock[0])
    assert attachments.transfer_pending(conn, hub, "default") == 1
    path = Path(row["stored_path"])
    if damage == "deleted":
        path.unlink()
    else:
        path.write_bytes(b"xxxxx")  # same size: rehashing is necessary
    fetched = []

    def download(_hub, _board, uid):
        fetched.append(uid)
        return b"proof"

    monkeypatch.setattr(helpers.BlobHub, "get_attachment_blob", download)
    cursor = conn.execute("SELECT * FROM kanban_sync_state").fetchone()
    clock[0] = 1599
    assert attachments.transfer_pending(conn, hub, "default") == 0
    assert fetched == []
    clock[0] = 1600
    assert attachments.transfer_pending(conn, hub, "default") == 1
    assert fetched == [row["uid"]]
    repaired_path = conn.execute("SELECT stored_path FROM task_attachments WHERE uid=?", (row["uid"],)).fetchone()[0]
    assert Path(repaired_path).read_bytes() == b"proof"
    assert tuple(conn.execute("SELECT * FROM kanban_sync_state").fetchone()) == tuple(cursor)
    stamp = conn.execute("SELECT * FROM kanban_attachment_retry WHERE uid=?", (row["uid"],)).fetchone()
    assert stamp["attempts"] == 2
    assert stamp["retry_at"] == 2200


def test_default_limit_keeps_remaining_row_immediately_eligible(stores, monkeypatch):
    _, _, _, conn, service, tid, _ = stores
    rows = [_verified_row(conn, tid, data) for data in (b"first", b"other", b"third")]
    monkeypatch.setattr(attachments, "_now", lambda: 1000)
    assert attachments.transfer_pending(conn, service.hub("worker"), "default") == 2
    assert {r[0] for r in conn.execute("SELECT uid FROM kanban_attachment_retry")} == {
        row["uid"] for row in rows[:2]}
    assert attachments.transfer_pending(conn, service.hub("worker"), "default") == 1
    assert conn.execute("SELECT count(*) FROM kanban_attachment_retry").fetchone()[0] == 3
