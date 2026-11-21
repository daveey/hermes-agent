"""Preserve historical evidence identity through scratch relocation."""
if not __name__.startswith("tests."):
    raise ImportError("Use scripts/run_tests.sh; unittest discovery bypasses test isolation")

from copy import deepcopy
import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_metadata_files as kmf
from hermes_completion_receipt import validate_completion_receipt
from tests.hermes_cli import test_kanban_metadata_files as helpers

kanban_home = helpers.kanban_home


@pytest.fixture(autouse=True)
def receipt_process_inventory(monkeypatch):
    # Receipt preservation tests own no external jobs. Real host process scans
    # may fail closed and defer deletion; that separate safety contract is
    # exercised in test_kanban_workspace_jobs, not by receipt byte assertions.
    from hermes_cli import kanban_workspace_jobs as jobs
    monkeypatch.setattr(jobs.psutil, "process_iter", lambda: iter(()))


def receipt(ref):
    return {"version": 1, "items": [{"scope": "report", "disposition": "delivered",
        "evidence": [{"ref": ref, "observed_at": "2026-01-01T00:00:00Z", "verification": "reported"}], "remaining": []}]}


def test_receipt_preserved_when_process_scan_defers_cleanup(kanban_home, monkeypatch):
    from hermes_cli import kanban_workspace_jobs as jobs

    def unavailable():
        raise jobs.psutil.AccessDenied(pid=123456789)

    monkeypatch.setattr(jobs.psutil, "process_iter", unavailable)
    with kbc.connect_closing() as conn:
        task, ws, run = helpers._scratch_card(conn)
        report = ws / "report.md"
        report.write_text("preserved despite cleanup deferral")
        value = receipt(str(report))
        assert kb.complete_task(conn, task, summary="Report", expected_run_id=run,
                                metadata={"completion_receipt": value})
        meta = kb.latest_run(conn, task).metadata
        assert meta["completion_receipt"] == value
        assert Path(meta["attached_from_workspace"][str(report)]).read_text() == report.read_text()
        assert ws.exists()  # fail-closed safety remains production behaviour


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
def test_exact_receipt_records_missing_or_attaches_beyond_incidental_budget(kanban_home, monkeypatch, name, exists):
    monkeypatch.setattr(kmf, "METADATA_FILES_MAX_BYTES", 2)
    with kbc.connect_closing() as conn:
        task, ws, run = helpers._scratch_card(conn)
        report = ws / name
        if exists:
            report.write_text("exceeds incidental cap")
        value = receipt(str(report))
        assert kb.complete_task(conn, task, summary="Report", metadata={"completion_receipt": value}, expected_run_id=run)
        meta = kb.latest_run(conn, task).metadata
        assert meta["completion_receipt"] == value
        record = meta["artifact_receipt"][0]
        assert record["path"] == str(report)
        if exists:
            assert "attachment_path" not in record  # readers resolve by attachment id/uid
            (row,) = [a for a in kb.list_attachments(conn, task) if a.id == record["attachment_id"]]
            assert Path(row.stored_path).read_text() == "exceeds incidental cap"
            assert "warning" not in record
        else:
            assert "missing" in record["warning"].lower()
            assert not kb.list_attachments(conn, task)
        assert not ws.exists()


def test_caller_cannot_forge_relocation_of_missing_evidence(kanban_home):
    with kbc.connect_closing() as conn:
        task, ws, run = helpers._scratch_card(conn)
        ref = str(ws / "missing")
        assert kb.complete_task(conn, task, summary="Report", expected_run_id=run,
                               metadata={"completion_receipt": receipt(ref), "attached_from_workspace": {ref: "/elsewhere/pretend"}})
        meta = kb.latest_run(conn, task).metadata
        assert "missing" in meta["artifact_receipt"][0]["warning"].lower()
        assert "attachment_path" not in meta["artifact_receipt"][0]
        assert not meta.get("attached_from_workspace")


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


@pytest.mark.parametrize("surface", ["display", "worker-context"])
def test_legacy_attachment_path_is_display_only_filtered(surface):
    from hermes_cli.kanban_artifact_receipt import display_metadata
    from hermes_cli.kanban_db_context import _ctx_metadata_line

    # Old persisted receipts carry a path meaningful only on their executor.
    # Generating this with complete_task would silently test only the new format.
    legacy = {
        "artifacts": ["/scratch/report.md", "~/report.md", "relative.md"],
        "artifact_receipt": [{
            "machine": "worker-b", "path": "/scratch/report.md",
            "attachment_path": "/executor-only/attachments/report.md",
            "attachment_id": 17, "attachment_uid": "legacy-attachment-uid",
            "sha256": "a" * 64, "role": "artifact",
        }],
        "completion_receipt": receipt("/scratch/report.md"),
    }
    original = deepcopy(legacy)
    if surface == "display":
        shown = display_metadata(legacy)
    else:
        line = _ctx_metadata_line(legacy)
        assert line is not None
        assert line.startswith("_metadata_: `") and line.endswith("`")
        shown = json.loads(line[len("_metadata_: `"):-1])
    assert "attachment_path" not in shown["artifact_receipt"][0]
    assert "/executor-only/attachments/report.md" not in json.dumps(shown)
    assert shown["artifact_receipt"] == [{
        "machine": "worker-b", "path": "/scratch/report.md",
        "attachment_id": 17, "attachment_uid": "legacy-attachment-uid",
        "sha256": "a" * 64, "role": "artifact",
    }]
    assert shown["artifacts"] == [
        "worker-b:/scratch/report.md", "worker-b:~/report.md", "relative.md",
    ]
    assert shown["completion_receipt"] == original["completion_receipt"]
    assert legacy == original


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

