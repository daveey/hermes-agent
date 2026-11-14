"""Exact scratch artifacts retain custody across cleanup, including large files."""
if not __name__.startswith("tests."):
    raise ImportError("Use scripts/run_tests.sh; unittest discovery bypasses test isolation")

import hashlib
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from tests.hermes_cli import test_kanban_metadata_files as helpers
from tests.hermes_cli.test_kanban_receipt_artifacts import receipt

kanban_home = helpers.kanban_home


@pytest.mark.parametrize("receipt_only", [False, True])
def test_large_scratch_file_survives_completion_without_blob(kanban_home, monkeypatch, receipt_only):
    monkeypatch.setattr(kb, "KANBAN_ATTACHMENT_MAX_BYTES", 4)
    with kbc.connect_closing() as conn:
        task, ws, run = helpers._scratch_card(conn)
        report = ws / "report.bin"
        report.write_bytes(b"proof")
        metadata = {"completion_receipt": receipt(str(report))} if receipt_only else {"artifacts": [str(report)]}
        assert kb.complete_task(conn, task, summary="Report", expected_run_id=run, metadata=metadata)
        meta = kb.latest_run(conn, task).metadata
        record = meta["artifact_receipt"][0]
        assert record["path"] == str(report)
        assert record["machine"]
        assert record["sha256"] == hashlib.sha256(b"proof").hexdigest()
        assert Path(record["retained_path"]).read_bytes() == b"proof"
        assert not Path(record["retained_path"]).is_relative_to(ws)
        assert not kb.list_attachments(conn, task)
        assert not ws.exists()


def test_missing_declared_scratch_file_completes_with_warning(kanban_home):
    with kbc.connect_closing() as conn:
        task, ws, run = helpers._scratch_card(conn)
        report = ws / "missing.md"
        assert kb.complete_task(conn, task, summary="Report", expected_run_id=run,
                                metadata={"artifacts": [str(report)]})
        record = kb.latest_run(conn, task).metadata["artifact_receipt"][0]
        assert "missing" in record["warning"].lower()
        assert record["path"] == str(report)
        assert "sha256" not in record


@pytest.mark.parametrize("large", [False, True])
def test_scratch_copy_failure_does_not_destroy_existing_evidence(kanban_home, monkeypatch, large):
    from hermes_cli import kanban_artifact_prestage as prestage

    if large:
        monkeypatch.setattr(kb, "KANBAN_ATTACHMENT_MAX_BYTES", 4)
    def fail(*args, **kwargs):
        raise OSError("controlled preservation failure")

    def fail_partway(source, dest):
        if dest is not None:
            dest.write_bytes(b"pa")  # a partial pre-lock copy must not survive
        fail()
    monkeypatch.setattr(prestage, "_read", fail_partway)
    if not large:
        monkeypatch.setattr(kb, "_copy_capped", fail)
    with kbc.connect_closing() as conn:
        task, ws, run = helpers._scratch_card(conn)
        report = ws / "report.bin"
        report.write_bytes(b"proof")
        with pytest.raises(kb.ArtifactPreservationError, match="controlled preservation"):
            kb.complete_task(conn, task, summary="Report", expected_run_id=run,
                             metadata={"artifacts": [str(report)]})
        assert report.read_bytes() == b"proof"
        assert kb.get_task(conn, task).status == "running"
        assert not kb.list_attachments(conn, task)
        assert not [p for p in kb.task_attachments_dir(task).rglob("*") if p.is_file()]


def test_incidental_metadata_cannot_launder_denied_source(kanban_home, monkeypatch):
    from gateway.platforms import base
    from hermes_cli.kanban_metadata_files import SKIPPED_KEY

    with kbc.connect_closing() as conn:
        task, ws, run = helpers._scratch_card(conn)
        secret = ws / "credential"
        secret.write_text("not for delivery")
        denied = secret.resolve()
        monkeypatch.setattr(base, "_path_under_denied_prefix", lambda p: p == denied)
        monkeypatch.setattr(base, "_media_delivery_allowed_roots", list)
        assert kb.complete_task(conn, task, summary="Report", expected_run_id=run,
                                metadata={"report": str(secret)})
        meta = kb.latest_run(conn, task).metadata
        assert "denied" in meta[SKIPPED_KEY][str(secret)]
        assert not kb.list_attachments(conn, task)
