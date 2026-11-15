"""Byte custody across worker/home stores; the Board double models the blob API."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import kanban_sync_support as support
from tests.hermes_cli.kanban_sync_fake_board import FakeHub, FakeService


class BlobHub(FakeHub):
    def put_attachment(self, board, uid, digest, data):
        if self.svc.blobs_offline:
            raise OSError("blob endpoint offline")
        assert hashlib.sha256(data).hexdigest() == digest
        b = self.svc.board(board)
        row = b["rows"]["task_attachments"][uid]
        assert len(data) == row["size"]
        key = "sha256:" + digest
        self.svc.blobs[uid] = data
        self.svc._put(b, "task_attachments", uid, {"blob_key": key}, "storage", None, row["hlc"])
        return {"blob_key": key, "sha256": digest, "size": len(data)}

    def get_attachment_blob(self, board, uid):
        if self.svc.blobs_offline:
            raise OSError("blob endpoint offline")
        return self.svc.blobs[uid]


class BlobService(FakeService):
    def __init__(self):
        super().__init__()
        self.blobs = {}
        self.blobs_offline = False

    def hub(self, gateway):
        return BlobHub(self, gateway)


@pytest.fixture
def stores(tmp_path, monkeypatch):
    home = support.kanban_home(tmp_path, monkeypatch)
    # These stores launch no external workspace-owning jobs. Keep the real
    # cleanup veto, but do not make byte-custody assertions scan the busy host.
    monkeypatch.setattr("psutil.process_iter", lambda: iter(()))
    support.write_sync_config(home, True)
    worker = tmp_path / "worker"
    worker.mkdir()
    support.write_sync_config(worker, True)
    service = BlobService()
    connections = []

    def open_store(root, gateway):
        monkeypatch.setenv("HERMES_HOME", str(root))
        conn = kbc.connect(root / "kanban.db")
        client.ensure_identity(conn, "default", gateway)
        sync.set_mode(conn, "primary")
        connections.append(conn)
        return conn

    hub_conn = open_store(home, "hub")
    tid = kb.create_task(hub_conn, title="worker output", assignee="worker")
    client.push(hub_conn, service.hub("hub"), "default", "hub", primary=True)
    worker_conn = open_store(worker, "worker")
    client.pull(worker_conn, service.hub("worker"), "default", observers=False)

    clock = [2_000_000_000.0]

    def round_at(root, gateway):
        monkeypatch.setenv("HERMES_HOME", str(root))
        from hermes_cli import kanban_sync_attachments as attachments
        clock[0] += 601
        monkeypatch.setattr(attachments, "_now", lambda: clock[0])
        return client.sync_board("default", root / "kanban.db", service.hub(gateway), gateway)

    yield home, worker, hub_conn, worker_conn, service, tid, round_at
    client.reset_board_watch()
    for conn in connections:
        conn.close()


def test_worker_attachment_reaches_normal_home_read_path(stores):
    home, worker, hub_conn, worker_conn, service, tid, round_at = stores
    data = b"worker regression evidence\x00\xff"
    aid = kb.store_attachment_bytes(worker_conn, tid, "findings.bin", data, uploaded_by="kanban_complete")
    uid = worker_conn.execute("SELECT uid FROM task_attachments WHERE id=?", (aid,)).fetchone()[0]
    round_at(worker, "worker")
    assert service.blobs.get(uid) == data
    round_at(home, "hub")
    rows = kb.list_attachments(hub_conn, tid)
    assert len(rows) == 1
    assert Path(rows[0].stored_path).is_relative_to(kb.attachments_root())
    assert Path(rows[0].stored_path).read_bytes() == data


@pytest.fixture
def unavailable_host_inventory(monkeypatch):
    # The stores fixture must override discovery even on an unhealthy host.
    def unavailable():
        raise RuntimeError("host process inventory unavailable")
    monkeypatch.setattr("psutil.process_iter", unavailable)


@pytest.mark.usefixtures("unavailable_host_inventory")
def test_worker_completion_removes_scratch_but_home_reads_its_bytes(stores):
    from hermes_cli import kanban_db_workspace as workspace
    home, worker, hub_conn, conn, _service, tid, round_at = stores
    root = workspace.resolve_workspace(kb.get_task(conn, tid))
    workspace.set_workspace_path(conn, tid, root)
    report = root / "findings.md"
    report.write_bytes(b"real completion artifact")
    lease = sync.set_lease_client(support.FakeLeaseClient())
    try:
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None
        assert kb.complete_task(conn, tid, summary="verified report", expected_run_id=claimed.current_run_id,
                                metadata={"artifacts": [str(report)]})
    finally:
        sync.set_lease_client(lease)
    assert not root.exists()
    round_at(worker, "worker")
    round_at(home, "hub")
    assert kb.get_task(hub_conn, tid).status == "done"
    assert Path(kb.list_attachments(hub_conn, tid)[0].stored_path).read_bytes() == b"real completion artifact"


def test_ship_proof_is_readable_offline_after_cursor_advance(stores, monkeypatch):
    home, worker, hub_conn, worker_conn, service, tid, round_at = stores
    proof = {"head_sha": "a" * 40, "checks": [{"head_sha": "a" * 40, "name": "required", "conclusion": "success",
             "completed_at": "2026-01-01T00:00:00Z", "html_url": "https://example.com/check/1"}]}
    data = json.dumps(proof).encode()
    kb.store_attachment_bytes(worker_conn, tid, "ship-proof.json", data, uploaded_by="hermes-core-wt")
    service.blobs_offline = True
    round_at(worker, "worker")  # metadata gets ahead of the bytes
    round_at(home, "hub")
    assert not kb.list_attachments(hub_conn, tid)[0].stored_path
    client.reset_board_watch()  # no in-memory apply hint may be needed for recovery
    service.blobs_offline = False
    round_at(worker, "worker")
    round_at(home, "hub")
    service.blobs_offline = True
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: pytest.fail("GitHub/network unavailable"))
    row = kb.list_attachments(hub_conn, tid)[0]
    assert row.stored_path
    assert json.loads(Path(row.stored_path).read_bytes()) == proof


def test_eviction_retains_source_until_storage_ack(stores):
    from hermes_cli.kanban_worker_node import evict
    home, worker, hub_conn, conn, service, tid, round_at = stores
    kb.store_attachment_bytes(conn, tid, "receipt", b"proof")
    service.blobs_offline = True
    round_at(worker, "worker")
    assert not conn.execute("SELECT 1 FROM kanban_sync_outbox WHERE state='pending'").fetchone()
    assert tid not in evict(conn)
    assert kb.list_attachments(conn, tid)
    service.blobs_offline = False
    round_at(worker, "worker")
    assert tid in evict(conn)
    round_at(home, "hub")
    assert Path(kb.list_attachments(hub_conn, tid)[0].stored_path).read_bytes() == b"proof"


@pytest.mark.parametrize("corruption", [b"bad", b"xxxxx"])
def test_corrupt_remote_bytes_never_publish_a_path(stores, corruption):
    home, worker, hub_conn, conn, service, tid, round_at = stores
    aid = kb.store_attachment_bytes(conn, tid, "receipt", b"proof")
    uid = conn.execute("SELECT uid FROM task_attachments WHERE id=?", (aid,)).fetchone()[0]
    round_at(worker, "worker")
    service.blobs[uid] = corruption
    round_at(home, "hub")
    assert kb.list_attachments(hub_conn, tid)[0].stored_path == ""
    service.blobs[uid] = b"proof"
    round_at(home, "hub")  # cursor has advanced, but row remains durable pending work
    assert Path(kb.list_attachments(hub_conn, tid)[0].stored_path).read_bytes() == b"proof"


def test_duplicate_names_and_foreign_shadow_are_readable(stores):
    home, worker, hub_conn, conn, service, tid, round_at = stores
    other = kb.create_task(conn, title="worker owned", assignee="worker")
    for task in (tid, other):
        for data in (b"first", b"second"):
            kb.store_attachment_bytes(conn, task, "same.txt", data)
    for _ in range(3):
        round_at(worker, "worker")
        round_at(home, "hub")
    assert {Path(r.stored_path).read_bytes() for r in kb.list_attachments(hub_conn, tid)} == {b"first", b"second"}
    assert kb.get_task(hub_conn, other) is None
    with client._pulled_replica("default", service.hub("hub"), "hub") as (replica, _, _):
        assert {Path(r.stored_path).read_bytes() for r in kb.list_attachments(replica, other)} == {b"first", b"second"}


@pytest.mark.parametrize("bad_receipt", [None, {}, {"blob_key": "sha256:" + "0" * 64},
                                        {"blob_key": "key", "sha256": "wrong", "size": 5},
                                        {"blob_key": "sha256:" + "0" * 64,
                                         "sha256": hashlib.sha256(b"proof").hexdigest(), "size": 5}])
def test_unverified_upload_receipt_keeps_local_custody(stores, monkeypatch, bad_receipt):
    _, worker, _, conn, _, tid, round_at = stores
    aid = kb.store_attachment_bytes(conn, tid, "receipt", b"proof")
    monkeypatch.setattr(BlobHub, "put_attachment", lambda *a: bad_receipt)
    round_at(worker, "worker")
    row = conn.execute("SELECT * FROM task_attachments WHERE id=?", (aid,)).fetchone()
    assert row["blob_key"] is None
    assert Path(row["stored_path"]).read_bytes() == b"proof"


def test_deleted_row_during_download_is_not_resurrected(stores, monkeypatch):
    from hermes_cli import kanban_sync_attachments as attachments
    home, worker, hub_conn, conn, service, tid, round_at = stores
    kb.store_attachment_bytes(conn, tid, "receipt", b"proof")
    round_at(worker, "worker")
    monkeypatch.setenv("HERMES_HOME", str(home))
    client.pull(hub_conn, service.hub("hub"), "default", observers=False)

    def delete_then_download(hub, board, uid):
        with kbc.write_txn(hub_conn):
            hub_conn.execute("DELETE FROM task_attachments WHERE uid=?", (uid,))
        return b"proof"

    monkeypatch.setattr(BlobHub, "get_attachment_blob", delete_then_download)
    attachments.transfer_pending(hub_conn, service.hub("hub"), "default")
    assert kb.list_attachments(hub_conn, tid) == []


def test_outside_storage_source_is_never_uploaded(stores, tmp_path):
    _, worker, _, conn, service, tid, round_at = stores
    outside = tmp_path / "not-an-attachment"
    outside.write_bytes(b"private")
    kb.add_attachment(conn, tid, filename="fake", stored_path=str(outside), size=7)
    round_at(worker, "worker")
    assert not service.blobs


@pytest.mark.parametrize("damage", ["deleted", "corrupted"])
def test_verified_local_file_is_repaired_from_storage(stores, damage):
    home, worker, hub_conn, conn, _service, tid, round_at = stores
    kb.store_attachment_bytes(conn, tid, "receipt", b"proof")
    round_at(worker, "worker")
    round_at(home, "hub")
    path = Path(kb.list_attachments(hub_conn, tid)[0].stored_path)
    if damage == "deleted":
        path.unlink()
    else:
        path.write_bytes(b"xxxxx")
    round_at(home, "hub")
    assert Path(kb.list_attachments(hub_conn, tid)[0].stored_path).read_bytes() == b"proof"
