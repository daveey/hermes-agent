"""Selected source bytes survive replay, transport failure and scratch loss."""
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import test_kanban_sync_attachments as transport

stores = transport.stores


def api():
    from hermes_cli import kanban_source_artifacts
    return kanban_source_artifacts


def test_selection_replays_after_reopen_and_original_loss(stores, tmp_path):
    _, worker, _, conn, service, tid, _ = stores
    src = tmp_path / "report.txt"
    src.write_bytes(b"selected evidence")
    first = api().stage(conn, tid, [str(src)], board="default", run_uid="run-one")
    assert len(first) == 1
    assert api().stage(conn, tid, [str(src)], board="default", run_uid="run-one") == first
    reopened = kbc.connect(worker / "kanban.db")
    try:
        assert api().stage(reopened, tid, [str(src)], board="default", run_uid="run-one") == first
        src.unlink()
        client.push(reopened, service.hub("worker"), "default", "worker", primary=True)
        assert api().ensure(reopened, tid, first, service.hub("worker"), "default") == first
        assert len(kb.list_attachments(reopened, tid)) == 1
    finally:
        reopened.close()


def test_changed_bytes_get_new_uid_and_only_selected_transfer(stores, tmp_path):
    _, _, _, conn, service, tid, _ = stores
    src = tmp_path / "report.txt"
    src.write_bytes(b"one")
    first = api().stage(conn, tid, [str(src)], board="default", run_uid="run-one")
    src.write_bytes(b"two")
    second = api().stage(conn, tid, [str(src)], board="default", run_uid="run-one")
    assert first[0]["attachment_uid"] != second[0]["attachment_uid"]
    assert api().selected(conn, tid, "run-one") == second
    client.push(conn, service.hub("worker"), "default", "worker", primary=True)
    api().ensure(conn, tid, second, service.hub("worker"), "default")
    assert set(service.blobs) == {second[0]["attachment_uid"]}
    assert api().stage(conn, tid, [], board="default", run_uid="run-one") == []
    assert api().selected(conn, tid, "run-one") == []
    assert len(kb.list_attachments(conn, tid)) == 2


def test_remote_selected_download_survives_scratch_loss(stores, tmp_path, monkeypatch):
    home, _, dest, conn, service, tid, _ = stores
    src = tmp_path / "report.txt"
    src.write_bytes(b"remote evidence")
    entries = api().stage(conn, tid, [str(src)], board="default", run_uid="run-one")
    client.push(conn, service.hub("worker"), "default", "worker", primary=True)
    api().ensure(conn, tid, entries, service.hub("worker"), "default")
    src.unlink()
    monkeypatch.setenv("HERMES_HOME", str(home))
    client.pull(dest, service.hub("hub"), "default", observers=False)
    api().ensure(dest, tid, entries, service.hub("hub"), "default")
    assert Path(kb.list_attachments(dest, tid)[0].stored_path).read_bytes() == b"remote evidence"


def test_unanswered_upload_reuses_uid_and_bytes(stores, tmp_path, monkeypatch):
    _, _, _, conn, service, tid, _ = stores
    src = tmp_path / "report.txt"
    src.write_bytes(b"evidence")
    entries = api().stage(conn, tid, [str(src)], board="default", run_uid="run-one")
    client.push(conn, service.hub("worker"), "default", "worker", primary=True)
    original = transport.BlobHub.put_attachment

    def lost_reply(self, *args):
        original(self, *args)
        raise OSError("reply lost")

    monkeypatch.setattr(transport.BlobHub, "put_attachment", lost_reply)
    with pytest.raises(api().ArtifactRefused, match="artifact_unavailable"):
        api().ensure(conn, tid, entries, service.hub("worker"), "default")
    assert api().selected(conn, tid, "run-one") == entries
    assert api().stage(conn, tid, [str(src)], board="default", run_uid="run-one") == entries
    monkeypatch.setattr(transport.BlobHub, "put_attachment", original)
    api().ensure(conn, tid, entries, service.hub("worker"), "default")
    assert len(kb.list_attachments(conn, tid)) == 1


@pytest.mark.parametrize("damage", ["digest", "filename", "foreign", "deleted", "bytes"])
def test_mismatch_never_becomes_success(stores, tmp_path, damage):
    _, _, _, conn, service, tid, _ = stores
    src = tmp_path / "report.txt"
    src.write_bytes(b"evidence")
    entries = api().stage(conn, tid, [str(src)], board="default", run_uid="run-one")
    client.push(conn, service.hub("worker"), "default", "worker", primary=True)
    def foreign():
        nonlocal tid
        tid = kb.create_task(conn, title="other", assignee="worker")

    damages = {
        "digest": lambda: entries[0].__setitem__("sha256", "0" * 64),
        "filename": lambda: entries[0].__setitem__("filename", "other.txt"),
        "foreign": foreign,
        "deleted": lambda: kb.delete_attachment(conn, kb.list_attachments(conn, tid)[0].id),
        "bytes": lambda: Path(kb.list_attachments(conn, tid)[0].stored_path).write_bytes(b"tampered"),
    }
    damages[damage]()
    with pytest.raises(api().ArtifactRefused):
        api().ensure(conn, tid, entries, service.hub("worker"), "default")
    assert not service.blobs


@pytest.mark.parametrize("bad", ["missing", "directory", "secret", "denylisted", "oversize"])
def test_invalid_selection_does_not_replace_prior(stores, tmp_path, monkeypatch, bad):
    _, _, _, conn, _, tid, _ = stores
    src = tmp_path / "report.txt"
    src.write_bytes(b"good")
    prior = api().stage(conn, tid, [str(src)], board="default", run_uid="run-one")
    other = tmp_path / (".env" if bad == "denylisted" else "other")
    if bad == "directory":
        other.mkdir()
    elif bad != "missing":
        other.write_bytes(b"-----BEGIN PRIVATE KEY-----" if bad == "secret" else b"x" * 10)
    if bad == "oversize":
        monkeypatch.setattr(kb, "KANBAN_ATTACHMENT_MAX_BYTES", 4)
    with pytest.raises(api().ArtifactRefused):
        api().stage(conn, tid, [str(other)], board="default", run_uid="run-one")
    assert api().selected(conn, tid, "run-one") == prior
    assert len(kb.list_attachments(conn, tid)) == 1


def test_snapshot_rollback_retries_without_duplicate_or_missing_bytes(stores, tmp_path, monkeypatch):
    _, _, _, conn, _, tid, _ = stores
    src = tmp_path / "report.txt"
    src.write_bytes(b"evidence")
    original = kb._insert_completion_attachment

    def insert_then_fail(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError("crash before journal commit")

    monkeypatch.setattr(kb, "_insert_completion_attachment", insert_then_fail)
    with pytest.raises(api().ArtifactRefused):
        api().stage(conn, tid, [str(src)], board="default", run_uid="run-one")
    assert kb.list_attachments(conn, tid) == []
    assert api().selected(conn, tid, "run-one") is None
    monkeypatch.setattr(kb, "_insert_completion_attachment", original)
    entries = api().stage(conn, tid, [str(src)], board="default", run_uid="run-one")
    assert len(entries) == len(kb.list_attachments(conn, tid)) == 1
    assert Path(kb.list_attachments(conn, tid)[0].stored_path).read_bytes() == b"evidence"


def test_copy_changed_between_precheck_and_upload_is_never_sent(stores, tmp_path, monkeypatch):
    from hermes_cli import kanban_sync_attachments as blobs
    _, _, _, conn, service, tid, _ = stores
    src = tmp_path / "report.txt"
    src.write_bytes(b"evidence")
    entries = api().stage(conn, tid, [str(src)], board="default", run_uid="run-one")
    client.push(conn, service.hub("worker"), "default", "worker", primary=True)
    original = blobs._read
    reads = []

    def changed(path, row, board):
        reads.append(path)
        return original(path, row, board) if len(reads) == 1 else b"modified"

    monkeypatch.setattr(blobs, "_read", changed)
    with pytest.raises(api().ArtifactRefused):
        api().ensure(conn, tid, entries, service.hub("worker"), "default")
    assert not service.blobs, "hash only the exact bytes passed to put_attachment"


def test_portable_uses_same_run_selection_not_stale_predecessor(stores, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from hermes_cli import kanban_db_sync as sync
    _, _, _, conn, service, tid, _ = stores
    src = tmp_path / "report.txt"
    src.write_bytes(b"evidence")
    entries = api().stage(conn, tid, [str(src)], board="default", run_uid="run-one")
    monkeypatch.setattr(sync, "_client", lambda: SimpleNamespace(hub=service.hub("worker")))
    assert api().portable(conn, tid, None, "run-one") == entries
    assert service.blobs[entries[0]["attachment_uid"]] == b"evidence"
    assert api().selected(conn, tid, "run-two") is None
    assert api().portable(conn, tid, {"artifacts": []}, "run-two") == []
    assert api().portable(conn, tid, {"artifacts": entries}, "run-two") == entries
    api().stage(conn, tid, [], board="default", run_uid="run-two")
    assert api().selected(conn, tid, "run-one") == entries
    assert api().portable(conn, tid, {"artifacts": entries}, "run-two") == []


def test_duplicate_names_are_distinct_and_order_is_canonical(stores, tmp_path):
    _, _, _, conn, _, tid, _ = stores
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    paths = [a / "report.txt", b / "report.txt"]
    for n, path in enumerate(paths):
        path.write_bytes(f"evidence {n}".encode())
    entries = api().stage(conn, tid, list(map(str, paths)), board="default", run_uid="run-one")
    assert len(entries) == 2
    assert [e["filename"] for e in entries] == ["report.txt", "report.txt"]
    assert entries == sorted(entries, key=lambda e: e["attachment_uid"])
    assert api().stage(conn, tid, list(map(str, reversed(paths))), board="default", run_uid="run-one") == entries
