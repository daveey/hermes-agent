"""Regression contracts for PR #4300 review findings 4 and 5.

4. An ordinary (unfrozen) attachment whose row changed between ``put_attachment`` and the local record
   (a pull applied the Board's storage echo) is a successful transfer, not "frozen".
5. Archiving a frozen contained card is a typed ``ContainmentRefused`` (what ``kanban_cli_archive``
   catches), never a raw ``home.Pending``; nothing is inserted or sent.
"""
from __future__ import annotations

from argparse import Namespace
from contextlib import contextmanager, nullcontext
import hashlib
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_attachments as attachments
from hermes_cli import kanban_sync_client as client
from hermes_cli.kanban_containment_lifecycle import ContainmentRefused
from tests.hermes_cli import test_kanban_containment_terminal_remote as tr
from tests.hermes_cli import test_kanban_sync_attachments as helpers
from tests.hermes_cli.test_home_typed_capture import mirror

stores = helpers.stores
kanban_home = tr.kanban_home
synced_conn = tr.synced_conn


def _unsent_row(conn, service, tid):
    aid = kb.store_attachment_bytes(conn, tid, "receipt", b"echo-race")
    with kbc.write_txn(conn):
        conn.execute("UPDATE task_attachments SET blob_key=NULL WHERE id=?", (aid,))
    client.push(conn, service.hub("worker"), "default", "worker", primary=False)
    return dict(conn.execute("SELECT * FROM task_attachments WHERE id=?", (aid,)).fetchone())


@pytest.fixture
def second_store(stores):
    _, worker, _, _, _, _, _ = stores
    conn = kbc.connect(worker / "kanban.db")
    try:
        yield conn
    finally:
        conn.close()


def _echoing_hub(service, second):
    hub = service.hub("worker")
    put = hub.put_attachment

    def put_then_echo(board, uid, digest, data):
        receipt = put(board, uid, digest, data)
        # Real feed apply on a separate SQLite connection, after upload returns.
        client.pull(second, service.hub("worker"), board, observers=False)
        assert second.execute("SELECT blob_key FROM task_attachments WHERE uid=?", (uid,)).fetchone()[0] == receipt["blob_key"]
        return receipt

    hub.put_attachment = put_then_echo
    return hub


def test_unfrozen_row_changed_by_storage_echo_reports_success(stores, second_store):
    _, _, _, conn, service, tid, _ = stores
    row = _unsent_row(conn, service, tid)
    assert row["blob_key"] is None and row["uid"]
    before = conn.total_changes
    assert attachments._transfer(conn, _echoing_hub(service, second_store), "default", row) is True
    assert conn.total_changes == before  # no stale overwrite or unnecessary writer


def test_source_artifact_ensure_accepts_concurrent_storage_echo(stores, second_store):
    from hermes_cli import kanban_source_artifacts as artifacts
    _, _, _, conn, service, tid, _ = stores
    row = _unsent_row(conn, service, tid)
    entries = [{"attachment_uid": row["uid"], "filename": row["filename"],
                "sha256": hashlib.sha256(b"echo-race").hexdigest()}]
    assert artifacts.ensure(conn, tid, entries, _echoing_hub(service, second_store), "default") == entries


def test_storage_echo_before_writer_upgrade_still_reports_success(stores, second_store, monkeypatch):
    _, _, _, conn, service, tid, _ = stores
    row = _unsent_row(conn, service, tid)

    @contextmanager
    def echo_before_writer(target):
        client.pull(second_store, service.hub("worker"), "default", observers=False)
        with kbc.write_txn(target):
            yield

    monkeypatch.setattr(attachments, "write_txn", echo_before_writer)
    assert attachments._transfer(conn, service.hub("worker"), "default", row) is True
    current = conn.execute("SELECT * FROM task_attachments WHERE uid=?", (row["uid"],)).fetchone()
    assert current["blob_key"] == "sha256:" + hashlib.sha256(b"echo-race").hexdigest()
    assert current["stored_path"] == row["stored_path"]


@pytest.mark.parametrize("custody", ["pending", "prepared", "malformed"])
def test_upload_freeze_after_rpc_still_reports_not_done(stores, second_store, custody):
    _, _, _, conn, service, tid, _ = stores
    row = _unsent_row(conn, service, tid)
    hub = service.hub("worker")
    put = hub.put_attachment

    def put_then_freeze(board, uid, digest, data):
        receipt = put(board, uid, digest, data)
        _freeze(second_store, tid, custody)
        return receipt

    hub.put_attachment = put_then_freeze
    assert attachments._transfer(conn, hub, "default", row) is False
    assert dict(conn.execute("SELECT * FROM task_attachments WHERE uid=?", (row["uid"],)).fetchone()) == row


def test_frozen_row_still_reports_not_done_without_upload(stores):
    _, _, _, conn, service, tid, _ = stores
    row = _unsent_row(conn, service, tid)
    mirror(conn, tid, "epoch", pending=1)
    hub = service.hub("worker")
    hub.put_attachment = lambda *a: pytest.fail("a frozen card must not upload")
    assert attachments._transfer(conn, hub, "default", row) is False


def _freeze(conn, task, custody):
    if custody == "pending":
        mirror(conn, task, "epoch", pending=1)
    else:
        from hermes_cli import kanban_home_transfer as transfer
        with kbc.write_txn(conn):
            transfer._install(conn)
            conn.execute("INSERT INTO kanban_home_prepared1 VALUES (?,?,?,?,?)",
                         (task, "worker", "destination", sync.uuid7() if custody == "prepared" else "malformed", 1))


@pytest.mark.parametrize("custody", ["pending", "prepared", "malformed"])
def test_frozen_contained_archive_is_typed_refusal_with_nothing_sent(synced_conn, monkeypatch, custody):
    conn = synced_conn
    hub = tr.setup(conn, monkeypatch)
    _freeze(conn, hub.target, custody)
    with pytest.raises(ContainmentRefused):
        _archive(conn, hub.target)
    assert hub.posts == []
    assert conn.execute("SELECT COUNT(*) FROM containment_delivery WHERE json_extract(op_json,'$.op')='archive'"
                        ).fetchone()[0] == 0
    assert getattr(kb.get_task(conn, hub.target), "status", None) != "archived"


def _archive(conn, task):
    from hermes_cli import kanban_containment_terminal_remote as terminal
    return terminal.archive(conn, task)


@pytest.mark.parametrize("custody", [None, "pending", "prepared", "malformed"])
def test_public_archive_command_handles_custody(synced_conn, monkeypatch, capsys, custody):
    from hermes_cli import kanban_cli_archive as cli
    conn = synced_conn
    hub = tr.setup(conn, monkeypatch)
    if custody:
        _freeze(conn, hub.target, custody)
    monkeypatch.setattr(kbc, "connect_closing", lambda: nullcontext(conn))
    args = Namespace(task_ids=[hub.target], purge_ids=[], reason=None)
    result = cli.cmd_archive(args)
    output = capsys.readouterr()
    task = kb.get_task(conn, hub.target)
    assert task is not None
    if custody:
        assert result != 0
        assert "home transfer pending" in output.out + output.err
        assert hub.posts == []
        assert task.status == "triage"
    else:
        assert result == 0
        assert len(hub.posts) == 1
        assert task.status == "archived"


@pytest.mark.parametrize("change", ["deleted", "rebound_task", "renamed", "recreated"])
def test_download_race_never_publishes_stale_bytes(stores, second_store, monkeypatch, change):
    _, _, _, conn, service, tid, _ = stores
    row, data = helpers._queued_attachment(conn, tid, "receipt")
    other_task = kb.create_task(conn, title="other", assignee="worker")
    Path(row["stored_path"]).unlink()
    with kbc.write_txn(conn):
        conn.execute("UPDATE task_attachments SET stored_path='' WHERE uid=?", (row["uid"],))
    row = dict(conn.execute("SELECT * FROM task_attachments WHERE uid=?", (row["uid"],)).fetchone())
    destination = attachments._destination(row, "default")
    hub = service.hub("worker")
    after = []

    def download(board, uid):
        with kbc.write_txn(second_store):
            if change == "deleted":
                second_store.execute("DELETE FROM task_attachments WHERE uid=?", (uid,))
            elif change == "rebound_task":
                second_store.execute("UPDATE task_attachments SET task_id=? WHERE uid=?", (other_task, uid))
            elif change == "renamed":
                second_store.execute("UPDATE task_attachments SET filename='replacement' WHERE uid=?", (uid,))
            else:
                second_store.execute("DELETE FROM task_attachments WHERE uid=?", (uid,))
                second_store.execute("INSERT INTO task_attachments(task_id,filename,stored_path,size,created_at,uid,blob_key) "
                                     "VALUES (?,?,?,?,?,?,?)",
                                     (tid, row["filename"], "", row["size"], row["created_at"] + 1, uid, row["blob_key"]))
        after.extend(tuple(r) for r in second_store.execute("SELECT * FROM task_attachments"))
        return data

    hub.get_attachment_blob = download
    assert attachments._transfer(conn, hub, "default", row) is True
    assert [tuple(r) for r in conn.execute("SELECT * FROM task_attachments")] == after
    assert not destination.exists()


@pytest.mark.parametrize("custody", ["pending", "prepared", "malformed"])
def test_download_freeze_from_second_connection_refuses_publication(stores, second_store, custody):
    _, _, _, conn, service, tid, _ = stores
    row, data = helpers._queued_attachment(conn, tid, "receipt")
    Path(row["stored_path"]).unlink()
    hub = service.hub("worker")

    def download(board, uid):
        _freeze(second_store, tid, custody)
        return data

    hub.get_attachment_blob = download
    before = dict(row)
    assert attachments._transfer(conn, hub, "default", row) is False
    assert dict(conn.execute("SELECT * FROM task_attachments WHERE uid=?", (row["uid"],)).fetchone()) == before
    assert not attachments._destination(row, "default").exists()


def test_verified_attachment_fast_path_does_not_reserve_writer(stores, second_store):
    _, _, _, conn, service, tid, _ = stores
    row, data = helpers._queued_attachment(conn, tid, "receipt")
    # A different connection owns the writer: read-only verification must still work.
    with kbc.write_txn(second_store):
        assert attachments._transfer(conn, service.hub("worker"), "default", row) is True
    assert Path(row["stored_path"]).read_bytes() == data

