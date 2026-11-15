"""Real resolver and selected-byte transfer, before attachment/spawn."""
import hashlib
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_source_artifacts as artifacts
from hermes_cli import kanban_source_artifact_launch as selected_launch
from hermes_cli import kanban_source_registration as registration
from hermes_cli import kanban_source_resolver as resolver
from hermes_cli import kanban_workspace_source as ws
from tests.hermes_cli import test_kanban_source_registration as helpers
from tests.hermes_cli.test_kanban_workspace_source import manifest

setup = helpers.setup


@pytest.mark.parametrize("fault", [None, "missing_metadata", "unavailable", "mismatch", "expired_after"])
def test_resolver_verifies_selected_download_before_return(setup, tmp_path, monkeypatch, fault):
    conn, _, tid, source, _, _, _ = setup
    run = registration.register_initial(conn, tid, str(source))
    src = tmp_path / "report.txt"
    src.write_bytes(b"report")
    entries = artifacts.stage_for_run(conn, tid, [str(src)], board=kb.get_current_board(), expected_run_id=run.run_id)
    entry = entries[0]
    conn.execute("UPDATE task_attachments SET stored_path='',blob_key=? WHERE uid=?",
                 ("sha256:" + entry["sha256"], entry["attachment_uid"]))
    if fault == "missing_metadata":
        conn.execute("DELETE FROM task_attachments WHERE uid=?", (entry["attachment_uid"],))
    conn.commit()
    raw = {**manifest(), "version": 2, "artifacts": entries, "source_kind": "scratch_external",
           "commit": run.pre_head, "branch": "refs/heads/" + run.branch}
    snapshot = ws.SourceSnapshot(raw, ws.Custody(run.machine, "published", 1, source_protocol=2))
    calls = []
    now = [0.0]

    def download(board, uid):
        calls.append(uid)
        if fault == "unavailable":
            raise OSError("offline")
        if fault == "expired_after":
            now[0] = 100000.0
        return b"tamper" if fault == "mismatch" else b"report"

    monkeypatch.setattr(selected_launch, "_transport", lambda c, bound: (
        SimpleNamespace(get_attachment_blob=download), kb.get_current_board()))
    # Preserve actual Git holder resolution, DB rows, transport validation and copy.
    if fault:
        code = "artifact_mismatch" if fault == "mismatch" else "artifact_unavailable"
        with pytest.raises(ws.WorkspaceSourceRefusal, match=code):
            resolver.resolve_source_workspace(conn, kb.get_task(conn, tid), snapshot, me=run.machine, clock=lambda: now[0])
    else:
        resolved = resolver.resolve_source_workspace(conn, kb.get_task(conn, tid), snapshot, me=run.machine)
        assert resolved.path == str(source)
        row = artifacts._row(conn, tid, entry)
        from pathlib import Path
        assert hashlib.sha256(Path(row["stored_path"]).read_bytes()).hexdigest() == entry["sha256"]
    assert calls == ([] if fault == "missing_metadata" else [entry["attachment_uid"]])


@pytest.mark.parametrize("old_deadline,expected", [(None, 125.0), (110.0, 110.0), (140.0, 125.0)])
def test_transport_has_private_shared_remaining_deadline(setup, monkeypatch, old_deadline, expected):
    from hermes_cli import kanban_db_sync as sync
    conn, _, _, _, _, _, _ = setup
    shared = SimpleNamespace(ident=object(), timeout=30, deadline=old_deadline)
    monkeypatch.setattr(sync, "_client", lambda: SimpleNamespace(hub=shared))
    monkeypatch.setattr(artifacts.time, "monotonic", lambda: 100.0)
    hub, _ = selected_launch._transport(conn, SimpleNamespace(left=lambda: 25.0))
    assert hub is not shared and hub.ident is shared.ident
    assert hub.deadline == expected
    assert shared.deadline == old_deadline


def test_premanifest_successor_verifies_exact_retained_selection(setup, tmp_path, monkeypatch):
    from tests.hermes_cli.test_kanban_source_dispatch_v2 import successor, grant_snapshot
    conn, _, tid, source, _, _, _ = setup
    previous = registration.register_initial(conn, tid, str(source))
    src = tmp_path / "report.txt"
    src.write_bytes(b"report")
    entries = artifacts.stage_for_run(conn, tid, [str(src)], board=kb.get_current_board(), expected_run_id=previous.run_id)
    successor(conn, tid)
    snapshot, _ = grant_snapshot(conn, tid, previous)
    seen = []
    monkeypatch.setattr(selected_launch, "_transport", lambda c, bound: (object(), "test"))
    def ensure(c, task_id, selected, hub, board):
        seen.extend(selected)
        raise artifacts.ArtifactRefused("artifact_unavailable", "offline")
    monkeypatch.setattr(artifacts, "ensure", ensure)
    with pytest.raises(ws.WorkspaceSourceRefusal, match="artifact_unavailable"):
        resolver.resolve_source_workspace(conn, kb.get_task(conn, tid), snapshot, me=previous.machine)
    assert seen == entries
    assert artifacts.selected(conn, tid, previous.run_uid) == entries
