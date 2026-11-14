"""Receiver-local byte custody and source policy regression tests."""
if not __name__.startswith("tests."):
    raise ImportError("Use scripts/run_tests.sh; unittest discovery bypasses test isolation")

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_artifact_delivery as delivery
from tests.hermes_cli import test_kanban_artifact_machine as support

stores = support.stores


@pytest.mark.parametrize("retained", [False, True])
def test_large_local_artifact_delivery_checks_identity_digest_and_policy(stores, monkeypatch, retained):
    from gateway.platforms import base

    _, worker, home_conn, conn, _, tid, _ = stores
    source = worker / "large.bin"
    source.write_bytes(b"larger than fixture cap")
    monkeypatch.setattr(kb, "KANBAN_ATTACHMENT_MAX_BYTES", 1)
    record = support.complete(conn, tid, [str(source)])["artifact_receipt"][0]
    if retained:
        target = worker / "retained.bin"
        source.rename(target)
        record["retained_path"] = str(target)
    else:
        target = source
    assert delivery.receipt_delivery_paths(conn, tid, [record]) == [str(target)]
    assert delivery.receipt_delivery_paths(home_conn, tid, [record]) == []
    assert delivery.receipt_delivery_paths(conn, tid, [{**record, "sha256": "bad"}]) == []
    monkeypatch.setattr(base, "validate_media_delivery_path", lambda p: None)
    assert delivery.receipt_delivery_paths(conn, tid, [record]) == []


@pytest.mark.asyncio
async def test_notifier_uploads_large_artifact_on_writer(stores, monkeypatch):
    from gateway.kanban_watchers import GatewayKanbanWatchersMixin

    _, worker, _, conn, _, tid, _ = stores
    report = worker / "large-report.bin"
    report.write_bytes(b"large report")
    monkeypatch.setattr(kb, "KANBAN_ATTACHMENT_MAX_BYTES", 1)
    payload = support.complete(conn, tid, [str(report)])
    adapter = SimpleNamespace(extract_local_files=lambda text: ([], []), send_document=AsyncMock())
    # Resolved on the test thread: the notifier digests on a worker thread, where
    # this fixture's sqlite connection may not be used.
    resolved = delivery.receipt_delivery_paths(conn, tid, payload["artifact_receipt"])
    monkeypatch.setattr(delivery, "delivery_paths", lambda task_id, records, board: resolved)
    await GatewayKanbanWatchersMixin._deliver_kanban_artifacts(
        None, adapter=adapter, chat_id="chat", metadata={},
        event_payload=payload, task=SimpleNamespace(id=tid, result="done"),
    )
    assert adapter.send_document.await_args.kwargs["file_path"] == str(report)


@pytest.mark.parametrize("symlink", [False, True])
def test_denied_source_is_not_laundered_into_attachment(stores, monkeypatch, symlink):
    from gateway.platforms import base

    _, worker, _, conn, _, tid, _ = stores
    secret = worker / "credential"
    secret.write_text("not for delivery")
    source = worker / "alias" if symlink else secret
    if symlink:
        source.symlink_to(secret)
    monkeypatch.setattr(base, "_path_under_denied_prefix", lambda p: p == secret.resolve())
    monkeypatch.setattr(base, "_media_delivery_allowed_roots", list)
    record = support.complete(conn, tid, [str(source)])["artifact_receipt"][0]
    assert "denied" in record["warning"]
    assert not kb.list_attachments(conn, tid)


@pytest.mark.parametrize("missing", [False, True])
def test_superseding_file_has_custody(stores, missing):
    from tests.hermes_cli.test_kanban_receipt_artifacts import receipt

    _, worker, _, conn, _, tid, _ = stores
    source = worker / "replacement.md"
    if not missing:
        source.write_text("replacement")
    value = receipt("commit abc")
    value["items"][0].update(disposition="superseded", superseded_by=str(source))
    record = support.complete(conn, tid, [], {"completion_receipt": value})["artifact_receipt"][0]
    assert record["path"] == str(source)
    assert record["machine"] == "worker"
    assert ("warning" in record) == missing
    assert bool(kb.list_attachments(conn, tid)) != missing


@pytest.mark.asyncio
async def test_notifier_uses_replicated_attachment_and_not_foreign_paths(stores, monkeypatch):
    from gateway.kanban_watchers import GatewayKanbanWatchersMixin

    home, worker, home_conn, conn, _, tid, round_at = stores
    report = worker / "proof.md"
    report.write_bytes(b"worker proof")
    payload = support.complete(conn, tid, [str(report)])
    payload["summary"] = str(report)
    task = SimpleNamespace(id=tid, result=str(report))
    adapter = SimpleNamespace(extract_local_files=lambda text: ([text], []), send_document=AsyncMock())
    monkeypatch.setattr(delivery, "delivery_paths", lambda task_id, records, board: delivery.receipt_delivery_paths(home_conn, task_id, records))
    from gateway import kanban_watchers

    async def inline(fn, *args):  # home_conn is bound to this thread; off-loop digesting has its own test
        return fn(*args)
    monkeypatch.setattr(kanban_watchers.asyncio, "to_thread", inline)
    call = dict(adapter=adapter, chat_id="chat", metadata={}, event_payload=payload, task=task)
    # Both writer-local paths exist here: receiver must not use either before sync.
    await GatewayKanbanWatchersMixin._deliver_kanban_artifacts(None, **call)
    adapter.send_document.assert_not_awaited()
    round_at(worker, "worker")
    round_at(home, "hub")
    await GatewayKanbanWatchersMixin._deliver_kanban_artifacts(None, **call)
    uploaded = Path(adapter.send_document.await_args.kwargs["file_path"])
    assert uploaded.read_bytes() == b"worker proof"
    assert uploaded != report
    assert str(uploaded) != payload["artifacts"][0]
    adapter.send_document.reset_mock()
    # Corrupt local bytes, warnings, wrong UID, and foreign integer IDs fail closed.
    uploaded.write_bytes(b"unrelated")
    await GatewayKanbanWatchersMixin._deliver_kanban_artifacts(None, **call)
    adapter.send_document.assert_not_awaited()
    uploaded.write_bytes(b"worker proof")
    record = payload["artifact_receipt"][0]
    for changes in ({"warning": "missing"}, {"attachment_uid": "wrong"}, {"attachment_uid": None}):
        changed = {**record, **changes}
        assert delivery.receipt_delivery_paths(home_conn, tid, [changed]) == []
    assert delivery.receipt_delivery_paths(home_conn, "other-task", [record]) == []
    without_digest = {key: value for key, value in record.items() if key != "sha256"}
    assert delivery.receipt_delivery_paths(home_conn, tid, [None, "bad", {}, without_digest]) == []
    uploaded.unlink()
    assert delivery.receipt_delivery_paths(home_conn, tid, [record]) == []
