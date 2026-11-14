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
