"""Typed edge refusals and metadata payload preservation for source artifacts."""
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_source_artifacts as artifacts
from hermes_cli import kanban_source_registration as registration
from hermes_cli.kanban_source_artifact_http import SourceArtifactHub
from tests.hermes_cli import test_kanban_source_registration as helpers

setup = helpers.setup


@pytest.mark.parametrize("body,expected", [(b'{"results":[{"ok":1}]}', {"results": [{"ok": 1}]}), (b"", {})])
def test_metadata_ack_preserved(monkeypatch, body, expected):
    from hermes_cli import kanban_sync_attachment_http as transport
    calls = []

    def request(*args, **kwargs):
        calls.append((args, kwargs))
        return body

    monkeypatch.setattr(transport, "_request", request)
    assert SourceArtifactHub.ops(SimpleNamespace(timeout=5), "b/c", []) == expected
    assert calls[0][0][2] == "/v2/boards/b%2Fc/ops"


@pytest.mark.parametrize("uid", ["", "  ", None, 7, b"x"])
def test_selection_requires_nonblank_string_run_uid(setup, uid):
    conn, _, tid, *_ = setup
    with pytest.raises(artifacts.ArtifactRefused, match="owning run UID") as caught:
        artifacts.stage(conn, tid, [], board="default", run_uid=uid)
    assert caught.value.code == "artifact_refused"
    assert kb.list_attachments(conn, tid) == []


@pytest.mark.parametrize("paths", [["missing"] * 33, "abc", None, {"a"}])
def test_selection_rejects_invalid_container_before_io(setup, paths):
    conn, _, tid, *_ = setup
    with pytest.raises(artifacts.ArtifactRefused, match="at most 32"):
        artifacts.stage(conn, tid, paths, board="default", run_uid="run")
    assert kb.list_attachments(conn, tid) == []
    assert artifacts.selected(conn, tid, "run") is None


@pytest.mark.parametrize("insert", [False, True])
def test_missing_sync_uid_rolls_back_selection(setup, tmp_path, monkeypatch, insert):
    conn, _, tid, *_ = setup
    src = tmp_path / "evidence.txt"
    src.write_bytes(b"evidence")
    original = kb._insert_completion_attachment

    def missing_uid(*args, **kwargs):
        if insert:
            original(*args, **kwargs)
            conn.execute("UPDATE task_attachments SET uid='' WHERE task_id=?", (tid,))

    monkeypatch.setattr(kb, "_insert_completion_attachment", missing_uid)
    with pytest.raises(artifacts.ArtifactRefused, match="no sync UID") as caught:
        artifacts.stage(conn, tid, [str(src)], board="default", run_uid="run")
    assert caught.value.code == "artifact_unavailable"
    assert artifacts.selected(conn, tid, "run") is None
    assert kb.list_attachments(conn, tid) == []


def test_publication_missing_current_run_is_typed(setup):
    conn, _, tid, source, *_ = setup
    run = registration.register_initial(conn, tid, str(source))
    conn.execute("UPDATE tasks SET current_run_id=NULL WHERE id=?", (tid,))
    conn.commit()
    with pytest.raises(artifacts.ArtifactRefused) as caught:
        artifacts.publication_fence(conn, tid, run, None)
    assert caught.value.code == "publish_run_ended"


@pytest.mark.parametrize("run_id", [None, -1])
def test_unobserved_receipt_defers_to_board(setup, run_id):
    conn, _, tid, *_ = setup
    assert artifacts.receipt_selection_current(conn, tid, run_id, {"run_uid": "foreign"}) is True


def test_foreign_receipt_does_not_use_local_selection(setup, tmp_path):
    conn, _, tid, source, *_ = setup
    run = registration.register_initial(conn, tid, str(source))
    src = tmp_path / "evidence.txt"
    src.write_bytes(b"evidence")
    artifacts.stage_for_run(conn, tid, [str(src)], board="default", expected_run_id=run.run_id)
    assert artifacts.receipt_selection_current(conn, tid, run.run_id, {"run_uid": run.run_uid}) is False
    assert artifacts.receipt_selection_current(conn, tid, run.run_id, {"run_uid": "foreign"}) is True
