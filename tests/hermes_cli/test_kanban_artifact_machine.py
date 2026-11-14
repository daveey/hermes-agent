"""Completion file custody follows the executor, not the card's home."""
if not __name__.startswith("tests."):
    raise ImportError("Use scripts/run_tests.sh; unittest discovery bypasses test isolation")

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_sync as sync
from tests.hermes_cli import kanban_sync_support as support
from tests.hermes_cli import test_kanban_sync_attachments as attachment_support

stores = attachment_support.stores


def complete(conn, tid, paths, extra=None):
    lease = sync.set_lease_client(support.FakeLeaseClient())
    try:
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None
        assert kb.complete_task(conn, tid, summary="artifact custody verified",
                                expected_run_id=claimed.current_run_id,
                                metadata={"artifacts": paths, **(extra or {})})
    finally:
        sync.set_lease_client(lease)
    row = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='completed'", (tid,)).fetchone()
    return json.loads(row[0])


def test_pool_completion_records_executing_machine(stores):
    _, worker, _, conn, _, tid, _ = stores
    report = worker / "external-evidence.md"
    report.write_text("proof")
    payload = complete(conn, tid, [str(report)])
    receipt = payload["artifact_receipt"][0]
    assert receipt["machine"] == "worker"
    assert receipt["path"] == str(report)
    assert receipt["sha256"] == hashlib.sha256(b"proof").hexdigest()


def test_external_small_artifact_reaches_home_attachment(stores):
    home, worker, home_conn, conn, _, tid, round_at = stores
    report = worker / "external-evidence.md"
    report.write_bytes(b"proof")
    complete(conn, tid, [str(report)])
    round_at(worker, "worker")
    round_at(home, "hub")
    rows = kb.list_attachments(home_conn, tid)
    assert len(rows) == 1
    assert Path(rows[0].stored_path).read_bytes() == b"proof"


def test_missing_external_artifact_has_visible_warning(stores):
    _, worker, _, conn, _, tid, _ = stores
    missing = worker / "missing.md"
    payload = complete(conn, tid, [str(missing)])
    receipt = payload["artifact_receipt"][0]
    assert receipt["machine"] == "worker"
    assert receipt["path"] == str(missing)
    assert "missing" in receipt["warning"].lower()
    assert kb.list_attachments(conn, tid) == []


@pytest.mark.parametrize("final", [False, True])
def test_chat_notice_displays_writer_and_warning(final):
    from tui_gateway.session_notifications import _format_kanban_event_text
    from gateway.kanban_watchers_notifier import _fmt_completed

    payload = {"summary": "done", "artifact_receipt": [
        {"machine": "worker-b", "path": "/proof.md", "warning": "missing"},
    ]}
    if final:
        payload["final_sends"] = {"chat-1": "addressed delivery"}
    task = SimpleNamespace(title="proof", assignee="default", result=None)
    event = SimpleNamespace(kind="completed", payload=payload)
    text = _format_kanban_event_text({"task_id": "proof", "chat_id": "chat-1"}, task, event, "default")
    if final:
        assert "addressed delivery" in text
    ping, wake, _ = _fmt_completed(event, SimpleNamespace(task=task, head="proof", title="proof"))
    for value in (text, ping, wake):
        assert "worker-b:/proof.md" in value
        assert "warning: missing" in value


def test_latest_receipt_survives_long_event_history():
    from hermes_cli.kanban_artifact_receipt import latest_receipt, format_receipt

    custody = [{"machine": "worker-b", "path": "/proof.md", "sha256": "abc", "attachment_uid": "uid"}]
    events = [SimpleNamespace(kind="completed", payload={"artifact_receipt": custody})]
    events.extend(SimpleNamespace(kind="commented", payload={}) for _ in range(60))
    assert latest_receipt(events) == {"artifact_receipt": custody}
    assert latest_receipt([]) == {"artifact_receipt": []}
    assert format_receipt({}) == ""
    assert format_receipt({"artifact_receipt": [None, {}, {"path": "/no-host"}]}) == ""
    text = format_receipt(latest_receipt(events))
    assert "sha256: abc" in text
    assert "attachment_uid: uid" in text


def test_external_receipt_ref_is_attached_without_rewriting_identity(stores):
    from tests.hermes_cli.test_kanban_receipt_artifacts import receipt

    _, worker, _, conn, _, tid, _ = stores
    report = worker / "receipt.md"
    report.write_text("proof")
    value = receipt(str(report))
    payload = complete(conn, tid, [], {"completion_receipt": value})
    assert kb.latest_run(conn, tid).metadata["completion_receipt"] == value
    assert payload["artifact_receipt"][0]["path"] == str(report)
    assert payload["artifact_receipt"][0]["attachment_uid"]


def test_large_external_file_has_digest_without_attachment(stores, monkeypatch):
    _, worker, _, conn, _, tid, _ = stores
    report = worker / "large.bin"
    report.write_bytes(b"proof")
    monkeypatch.setattr(kb, "KANBAN_ATTACHMENT_MAX_BYTES", 4)
    record = complete(conn, tid, [str(report)])["artifact_receipt"][0]
    assert record["sha256"] == hashlib.sha256(b"proof").hexdigest()
    assert "attachment_uid" not in record
    assert "warning" not in record
    assert not kb.list_attachments(conn, tid)


def test_partial_copy_failure_warns_and_leaves_no_orphan(stores, monkeypatch):
    _, worker, _, conn, _, tid, _ = stores
    report = worker / "proof.md"
    report.write_bytes(b"proof")
    def fail_copy(source, dest, raw):
        dest.write_bytes(b"partial")
        raise OSError("controlled copy failure")
    monkeypatch.setattr(kb, "_copy_capped", fail_copy)
    record = complete(conn, tid, [str(report)])["artifact_receipt"][0]
    assert "controlled copy failure" in record["warning"]
    assert "attachment_uid" not in record
    assert not kb.list_attachments(conn, tid)
    assert not list(kb.task_attachments_dir(tid).glob("*"))


def test_caller_cannot_spoof_writer_or_relocate_missing_external_file(stores):
    _, worker, _, conn, _, tid, _ = stores
    missing = str(worker / "missing.md")
    actual = worker / "actual.md"
    actual.write_text("different evidence")
    payload = complete(conn, tid, [missing], {
        "artifact_receipt": [{"machine": "pretend", "path": str(actual)}],
        "attached_from_workspace": {missing: str(actual)},
    })
    record = payload["artifact_receipt"][0]
    assert record["machine"] == "worker"
    assert record["path"] == missing
    assert "missing" in record["warning"].lower()
    assert not kb.list_attachments(conn, tid)
