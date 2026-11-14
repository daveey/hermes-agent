"""Artifact custody stays bounded, declared-only for chat, and on the task's board."""
if not __name__.startswith("tests."):
    raise ImportError("Use scripts/run_tests.sh; unittest discovery bypasses test isolation")

import hashlib
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from tests.hermes_cli import test_kanban_metadata_files as helpers
from tests.hermes_cli.test_kanban_receipt_artifacts import receipt

kanban_home = helpers.kanban_home
BIG = b"larger than the fixture cap"


def _spy_reads(monkeypatch, conn, target: Path) -> list[bool]:
    """Record, for every read-open of *target*, whether the write txn was open."""
    seen: list[bool] = []
    original = Path.open

    def spy(self, mode="r", *args, **kwargs):
        if "r" in mode and self.expanduser() == target:
            seen.append(conn.in_transaction)
        return original(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", spy)
    return seen


@pytest.mark.parametrize("in_scratch", [True, False])
def test_large_file_is_never_read_under_the_write_lock(kanban_home, monkeypatch, tmp_path, in_scratch):
    monkeypatch.setattr(kb, "KANBAN_ATTACHMENT_MAX_BYTES", 4)
    with kbc.connect_closing() as conn:
        task, ws, run = helpers._scratch_card(conn)
        source = (ws if in_scratch else tmp_path) / "checkpoint.bin"
        source.write_bytes(BIG)
        reads = _spy_reads(monkeypatch, conn, source)
        assert kb.complete_task(conn, task, summary="Checkpoint", expected_run_id=run,
                                metadata={"completion_receipt": receipt(str(source))})
        record = kb.latest_run(conn, task).metadata["artifact_receipt"][0]
    assert reads and not any(reads), "large file read while the board write lock was held"
    assert record["sha256"] == hashlib.sha256(BIG).hexdigest()
    assert record["size"] == len(BIG)
    if in_scratch:
        retained = Path(record["retained_path"])
        assert retained.read_bytes() == BIG
        assert not list(retained.parent.glob(".prestage-*"))


@pytest.mark.parametrize("in_scratch", [True, False])
def test_large_file_changed_after_pre_lock_read_is_not_claimed(kanban_home, monkeypatch, tmp_path, in_scratch):
    from hermes_cli import kanban_artifact_prestage as prestage

    monkeypatch.setattr(kb, "KANBAN_ATTACHMENT_MAX_BYTES", 4)
    with kbc.connect_closing() as conn:
        task, ws, run = helpers._scratch_card(conn)
        source = (ws if in_scratch else tmp_path) / "checkpoint.bin"
        source.write_bytes(BIG)
        fill = prestage._fill

        def fill_then_write(*args):
            fill(*args)
            with source.open("ab") as stream:
                stream.write(b"more")

        monkeypatch.setattr(prestage, "_fill", fill_then_write)
        metadata = {"artifacts": [str(source)]}
        if in_scratch:
            with pytest.raises(kb.ArtifactPreservationError, match="checkpoint.bin"):
                kb.complete_task(conn, task, summary="Checkpoint", expected_run_id=run, metadata=metadata)
            assert kb.get_task(conn, task).status == "running"
            assert not [p for p in kb.task_attachments_dir(task).rglob("*") if p.is_file()]
            return
        assert kb.complete_task(conn, task, summary="Checkpoint", expected_run_id=run, metadata=metadata)
        record = kb.latest_run(conn, task).metadata["artifact_receipt"][0]
    assert "sha256" not in record
    assert "not hashed" in record["warning"]


@pytest.mark.parametrize("change_during_read", [True, False])
def test_prestage_rejects_source_changed_during_hash_and_cleans_copy(
    kanban_home, monkeypatch, change_during_read,
):
    from hermes_cli import kanban_artifact_prestage as prestage

    monkeypatch.setattr(kb, "KANBAN_ATTACHMENT_MAX_BYTES", 4)
    monkeypatch.setattr(prestage, "_CHUNK", 4)
    with kbc.connect_closing() as conn:
        task, ws, _ = helpers._scratch_card(conn)
        source = ws / "during-read.bin"
        source.write_bytes(BIG)
        real_sha256 = hashlib.sha256
        updates = []

        class WritingDigest:
            def __init__(self):
                self.digest = real_sha256()

            def update(self, chunk):
                self.digest.update(chunk)
                updates.append(chunk)
                if change_during_read and len(updates) == 1:
                    # A real size change between the first read and final stat;
                    # no timestamp-resolution assumptions or racing threads.
                    with source.open("ab") as stream:
                        stream.write(b"changed")

            def hexdigest(self):
                return self.digest.hexdigest()

        monkeypatch.setattr(prestage.hashlib, "sha256", WritingDigest)
        cache = {}
        prestage._fill(conn, task, {"artifacts": [str(source)]}, cache)
        entry = cache[str(source)]
        directory = kb.task_attachments_dir(task) / "local-artifacts"
        try:
            assert len(updates) > 1, "mutation must occur during a multi-chunk read"
            if change_during_read:
                assert source.read_bytes() == BIG + b"changed"
                assert "error" in entry, "prestage accepted a source changed during hashing"
                assert "changed while it was read" in entry["error"]
                # Check immediately after _fill, not after prestaged's finally:
                # rejected partial bytes must already have been removed.
                assert list(directory.iterdir()) == []
            else:
                assert "error" not in entry
                assert entry["sha256"] == real_sha256(BIG).hexdigest()
                assert entry["signature"] == prestage.signature(source.stat())
                assert entry["temp"].read_bytes() == BIG
                assert list(directory.iterdir()) == [entry["temp"]]
        finally:
            if entry.get("temp") is not None:
                entry["temp"].unlink(missing_ok=True)


@pytest.mark.parametrize("second_as", ["artifact", "evidence"])
def test_large_scratch_file_named_under_two_spellings_is_retained_once(kanban_home, monkeypatch, second_as):
    # `<ws>/./x` and `<ws>/x` are distinct declared strings but one prestage
    # entry: the second record must reuse the first claim, not crash on it.
    monkeypatch.setattr(kb, "KANBAN_ATTACHMENT_MAX_BYTES", 4)
    with kbc.connect_closing() as conn:
        task, ws, run = helpers._scratch_card(conn)
        (ws / "checkpoint.bin").write_bytes(BIG)
        spellings = [str(ws / "checkpoint.bin"), f"{ws}/./checkpoint.bin"]
        metadata = {"artifacts": spellings} if second_as == "artifact" else {
            "artifacts": spellings[:1], "completion_receipt": receipt(spellings[1])}
        assert kb.complete_task(conn, task, summary="Checkpoint", expected_run_id=run, metadata=metadata)
        assert kb.get_task(conn, task).status == "done"
        records = kb.latest_run(conn, task).metadata["artifact_receipt"]
    assert [r["path"] for r in records] == spellings
    assert {r["retained_path"] for r in records} == {records[0]["retained_path"]}
    assert {r["sha256"] for r in records} == {hashlib.sha256(BIG).hexdigest()}
    retained = Path(records[0]["retained_path"])
    assert retained.read_bytes() == BIG
    assert [p.name for p in retained.parent.iterdir()] == [retained.name]


def test_managed_large_file_without_a_pre_lock_copy_fails_closed(kanban_home, monkeypatch):
    from hermes_cli import kanban_artifact_prestage as prestage
    from hermes_cli import kanban_artifact_receipt as custody

    monkeypatch.setattr(kb, "KANBAN_ATTACHMENT_MAX_BYTES", 4)
    with kbc.connect_closing() as conn:
        task, ws, _ = helpers._scratch_card(conn)
        source = ws / "checkpoint.bin"
        source.write_bytes(BIG)
        stat = source.stat()
        # Prestage judged the file unmanaged (no copy); the lock-time check disagrees.
        token = prestage._CACHE.set({str(source): {
            "signature": prestage.signature(stat), "sha256": "0" * 64, "temp": None}})
        try:
            with pytest.raises(kb.ArtifactPreservationError, match="no pre-lock copy"):
                custody._large(conn, task, source, stat, {}, [], kb.DEFAULT_BOARD)
        finally:
            prestage._CACHE.reset(token)


def test_evidence_refs_keep_custody_but_only_declared_artifacts_upload(kanban_home, tmp_path):
    from hermes_cli import kanban_artifact_delivery as delivery
    from hermes_cli.kanban_artifact_receipt import format_receipt

    deliverable = tmp_path / "deliverable.md"
    deliverable.write_text("deliverable")
    evidence = tmp_path / "evidence.log"
    evidence.write_text("evidence")
    value = receipt(str(evidence))
    value["items"][0]["evidence"] += [
        {"ref": ref, "observed_at": "2026-01-01T00:00:00Z", "verification": "reported"}
        for ref in ("/api/v1/tasks", f"{evidence}:12", "/repo commit abc123", "/healthz", str(tmp_path / "gone.md"))
    ]
    with kbc.connect_closing() as conn:
        task, _, run = helpers._scratch_card(conn)
        assert kb.complete_task(conn, task, summary="Report", expected_run_id=run,
                                metadata={"artifacts": [str(deliverable)], "completion_receipt": value})
        records = kb.latest_run(conn, task).metadata["artifact_receipt"]
        by_path = {r["path"]: r for r in records}
        assert set(by_path) == {str(deliverable), str(evidence), str(tmp_path / "gone.md")}
        assert by_path[str(evidence)]["sha256"]  # custody is still recorded and attached
        assert "missing" in by_path[str(tmp_path / "gone.md")]["warning"].lower()
        uploads = delivery.receipt_delivery_paths(conn, task, records)
    assert [Path(p).read_text() for p in uploads] == ["deliverable"]
    notice = format_receipt({"artifact_receipt": records})
    assert "/api/v1/tasks" not in notice and "/healthz" not in notice and "commit" not in notice


def test_missing_declared_artifact_still_warns_whatever_its_shape(kanban_home):
    with kbc.connect_closing() as conn:
        task, _, run = helpers._scratch_card(conn)
        assert kb.complete_task(conn, task, summary="Report", expected_run_id=run,
                                metadata={"artifacts": ["/api/v1/report"]})
        record = kb.latest_run(conn, task).metadata["artifact_receipt"][0]
    assert record["path"] == "/api/v1/report"
    assert "missing" in record["warning"].lower()


@pytest.mark.parametrize("large", [False, True])
def test_custody_copies_land_on_the_tasks_board(kanban_home, monkeypatch, tmp_path, large):
    kb.create_board("other")
    if large:
        monkeypatch.setattr(kb, "KANBAN_ATTACHMENT_MAX_BYTES", 4)
    with kbc.connect_closing(board="other") as conn:
        task = kb.create_task(conn, title="other board", assignee="default")
        ws = helpers.kbw.resolve_workspace(kb.get_task(conn, task), board="other")
        helpers.kbw.set_workspace_path(conn, task, ws)
        assert kb.claim_task(conn, task)
        run = kb.get_task(conn, task).current_run_id
        sources = [ws / "report.bin", tmp_path / "outside.bin"]
        for source in sources:
            source.write_bytes(BIG if large else b"ok")
        assert kb.get_current_board() == kb.DEFAULT_BOARD
        assert kb.complete_task(conn, task, summary="Report", expected_run_id=run,
                                metadata={"artifacts": [str(p) for p in sources]})
        records = kb.latest_run(conn, task).metadata["artifact_receipt"]
        # Receipts carry no executor-local attachment_path: resolve by attachment_id.
        stored = {row["id"]: row["stored_path"] for row in conn.execute(
            "SELECT id, stored_path FROM task_attachments WHERE task_id=?", (task,))}
    root = kb.attachments_root(board="other").resolve()
    kept = [stored.get(r.get("attachment_id")) or r.get("retained_path") for r in records]
    kept = [Path(p).resolve() for p in kept if p]
    assert kept and all(p.is_relative_to(root) for p in kept), (kept, root)


@pytest.mark.asyncio
async def test_notifier_digests_off_the_event_loop(monkeypatch):
    from gateway.kanban_watchers import GatewayKanbanWatchersMixin
    from hermes_cli import kanban_artifact_delivery as delivery

    loop_thread = threading.get_ident()
    seen = []

    def fake(task_id, records, board):
        seen.append(threading.get_ident())
        return []

    monkeypatch.setattr(delivery, "delivery_paths", fake)
    adapter = SimpleNamespace(extract_local_files=lambda text: ([], []), send_document=AsyncMock())
    await GatewayKanbanWatchersMixin._deliver_kanban_artifacts(
        None, adapter=adapter, chat_id="chat", metadata={},
        event_payload={"artifact_receipt": [{"machine": "m", "path": "/x", "sha256": "0"}]},
        task=SimpleNamespace(id="t_1", result="done"),
    )
    assert seen and seen[0] != loop_thread
