"""Preserve historical evidence identity through scratch relocation."""
if not __name__.startswith("tests."):
    raise ImportError("Use scripts/run_tests.sh; unittest discovery bypasses test isolation")

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_metadata_files as kmf
from hermes_completion_receipt import validate_completion_receipt
from tests.hermes_cli import test_kanban_metadata_files as helpers

kanban_home = helpers.kanban_home


def receipt(ref):
    return {"version": 1, "items": [{"scope": "report", "disposition": "delivered",
        "evidence": [{"ref": ref, "observed_at": "2026-01-01T00:00:00Z", "verification": "reported"}], "remaining": []}]}


def test_receipt_identity_survives_artifact_relocation(kanban_home):
    with kbc.connect_closing() as conn:
        task, ws, run = helpers._scratch_card(conn)
        report = ws / "report.md"
        report.write_text("observed report")
        value = receipt(str(report))
        assert kb.complete_task(conn, task, summary="Report delivered", metadata={"completion_receipt": value}, expected_run_id=run)
        meta = kb.latest_run(conn, task).metadata
        assert meta["completion_receipt"] == value
        assert validate_completion_receipt(meta["completion_receipt"]) == value
        assert Path(meta["attached_from_workspace"][str(report)]).read_text() == "observed report"
        assert not ws.exists()


@pytest.mark.parametrize("name,exists", [("report.md", False), ("report with spaces.md", False), ("report.md", True)])
def test_unpreservable_receipt_rolls_back_and_removes_staged_files(kanban_home, monkeypatch, name, exists):
    monkeypatch.setattr(kmf, "METADATA_FILES_MAX_BYTES", 2)
    with kbc.connect_closing() as conn:
        task, ws, run = helpers._scratch_card(conn)
        report = ws / name
        if exists:
            report.write_text("exceeds cap")
        artifact = ws / "report"
        artifact.write_text("ok")
        before = conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=?", (task,)).fetchone()[0]
        with pytest.raises(kb.ArtifactPreservationError, match="completion_receipt"):
            kb.complete_task(conn, task, summary="Report delivered", metadata={"artifacts": [str(artifact)], "completion_receipt": receipt(str(report))}, expected_run_id=run)
        assert kb.get_task(conn, task).status == "running"
        assert conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id=?", (task,)).fetchone()[0] == before
        assert not kb.list_attachments(conn, task)
        assert not list(kb.task_attachments_dir(task).glob("*"))
        assert ws.exists()


def test_caller_cannot_forge_relocation_of_missing_evidence(kanban_home):
    with kbc.connect_closing() as conn:
        task, ws, run = helpers._scratch_card(conn)
        ref = str(ws / "missing")
        with pytest.raises(kb.ArtifactPreservationError, match="completion_receipt"):
            kb.complete_task(conn, task, summary="Report delivered", expected_run_id=run,
                             metadata={"completion_receipt": receipt(ref), "attached_from_workspace": {ref: "/elsewhere/pretend"}})
        assert kb.get_task(conn, task).status == "running"


def test_late_run_write_failure_removes_receipt_copies(kanban_home, monkeypatch):
    with kbc.connect_closing() as conn:
        task, ws, run = helpers._scratch_card(conn)
        report = ws / "report.md"
        report.write_text("observed report")
        def fail(*args, **kwargs):
            raise RuntimeError("controlled run write failure")
        monkeypatch.setattr(kb, "_end_run", fail)
        with pytest.raises(RuntimeError, match="controlled run write"):
            kb.complete_task(conn, task, summary="Report delivered", expected_run_id=run,
                             metadata={"completion_receipt": receipt(str(report))})
        assert kb.get_task(conn, task).status == "running"
        assert not kb.list_attachments(conn, task)
        assert not list(kb.task_attachments_dir(task).glob("*"))


def test_prefix_replacement_cannot_mutate_receipt():
    value = receipt("/scratch/report.md")
    rewritten = kmf._rewrite({"completion_receipt": value, "note": "/scratch/report"}, {"/scratch/report": "/attachments/report_1"})
    assert rewritten["completion_receipt"] == value
    assert rewritten["note"] == "/attachments/report_1"


@pytest.mark.parametrize("alias", [" {} ", "file://{}", "evidence at {}", "./report.md"])
def test_ambiguous_scratch_evidence_alias_refuses_before_cleanup(kanban_home, alias):
    with kbc.connect_closing() as conn:
        task, ws, run = helpers._scratch_card(conn)
        report = ws / "report.md"
        report.write_text("observed report")
        with pytest.raises(kb.ArtifactPreservationError, match="completion_receipt"):
            kb.complete_task(conn, task, summary="Report", expected_run_id=run,
                             metadata={"completion_receipt": receipt(alias.format(report))})
        assert ws.exists()
        assert kb.get_task(conn, task).status == "running"
        assert not kb.list_attachments(conn, task)

