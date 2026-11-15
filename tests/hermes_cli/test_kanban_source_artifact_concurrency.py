"""Artifact selection cannot overwrite a competing selection during filesystem I/O."""
import gc
import weakref

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_source_artifacts as artifacts
from tests.hermes_cli import test_kanban_source_artifact_staging as support

stores = support.stores


def test_competing_selection_during_copy_keeps_winner(stores, tmp_path, monkeypatch):
    from hermes_cli import kanban_db_connect as kbc
    _, worker, _, conn, _, tid, _ = stores
    first, second = tmp_path / "first.txt", tmp_path / "second.txt"
    first.write_bytes(b"first")
    second.write_bytes(b"winner")
    original = artifacts._snapshot
    winner = []

    def interleave(path):
        result = original(path)
        if str(path) == str(first):
            competing = kbc.connect(worker / "kanban.db")
            try:
                winner.extend(artifacts.stage(competing, tid, [str(second)], board="default", run_uid="run"))
            finally:
                competing.close()
        return result

    monkeypatch.setattr(artifacts, "_snapshot", interleave)
    with pytest.raises(artifacts.ArtifactRefused, match="selection changed"):
        artifacts.stage(conn, tid, [str(first)], board="default", run_uid="run")
    assert artifacts.selected(conn, tid, "run") == winner
    assert len(kb.list_attachments(conn, tid)) == 1


def test_snapshot_buffers_are_released_before_next_copy(stores, tmp_path, monkeypatch):
    _, _, _, conn, _, tid, _ = stores
    paths = [tmp_path / f"evidence-{n}.txt" for n in range(4)]
    for path in paths:
        path.write_bytes(b"evidence")
    original = artifacts._snapshot
    live = []

    class Buffer(bytearray):
        pass

    def tracked(path):
        gc.collect()
        assert not any(ref() is not None for ref in live), "previous snapshot buffer retained"
        resolved, data, digest = original(path)
        buffer = Buffer(data)
        live.append(weakref.ref(buffer))
        return resolved, buffer, digest

    monkeypatch.setattr(artifacts, "_snapshot", tracked)
    result = artifacts.stage(conn, tid, list(map(str, paths)), board="default", run_uid="run")
    assert len(result) == 4
    assert len(kb.list_attachments(conn, tid)) == 4


@pytest.mark.parametrize("phase", ["metadata", "blob"])
def test_portable_refuses_selection_changed_during_transport(stores, tmp_path, monkeypatch, phase):
    from types import SimpleNamespace
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_sync_client as client
    _, _, _, conn, service, tid, _ = stores
    src = tmp_path / "evidence.txt"
    src.write_bytes(b"evidence")
    entries = artifacts.stage(conn, tid, [str(src)], board="default", run_uid="run")
    monkeypatch.setattr(sync, "_client", lambda: SimpleNamespace(hub=service.hub("worker")))
    owner, name = (client, "push") if phase == "metadata" else (artifacts.blobs, "_transfer")
    original = getattr(owner, name)

    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        artifacts.stage(conn, tid, [], board="default", run_uid="run")
        return result

    monkeypatch.setattr(owner, name, changed)
    with pytest.raises(artifacts.ArtifactRefused, match="selection changed"):
        artifacts.portable(conn, tid, None, "run")
    assert artifacts.selected(conn, tid, "run") == []
    assert len(kb.list_attachments(conn, tid)) == 1
    assert entries[0]["attachment_uid"] in service.blobs


def test_copy_failure_preserves_previous_selection(stores, tmp_path, monkeypatch):
    _, _, _, conn, _, tid, _ = stores
    old, new = tmp_path / "old.txt", tmp_path / "new.txt"
    old.write_bytes(b"old")
    new.write_bytes(b"new")
    prior = artifacts.stage(conn, tid, [str(old)], board="default", run_uid="run")

    def fail(*args):
        raise OSError("copy unavailable")

    monkeypatch.setattr(artifacts.blobs, "_publish_file", fail)
    with pytest.raises(artifacts.ArtifactRefused, match="artifact_unavailable"):
        artifacts.stage(conn, tid, [str(new)], board="default", run_uid="run")
    assert artifacts.selected(conn, tid, "run") == prior
    assert len(kb.list_attachments(conn, tid)) == 1
