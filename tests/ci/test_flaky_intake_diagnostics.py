"""Large closed-owner inventories must not wedge durable intake delivery."""
import hashlib
import json

import pytest

from scripts.ci import flaky_intake as intake
from tests.ci.test_flaky_intake import Board


def seed(journal):
    journal.record("o/r", [(9, [{"test_id": "tests/a.py::test_a"}])],
                   {"id": 1, "head_sha": "a" * 40})
    return journal.pending("o/r")


def candidates(count):
    return [{"candidate": f"@task:default@worker-1/t_{i:016x}", "status": "done",
             "reason": "closed_history_incomplete"} for i in range(count)]


@pytest.mark.parametrize("notes", [candidates(60), [{"candidate": "界" * 7000}], candidates(10)])
def test_large_diagnostics_survive_restart_and_exact_readback(tmp_path, notes):
    path = tmp_path / "queue.sqlite3"
    journal = intake.Journal(path)
    rows = seed(journal)
    original = json.loads(rows[0]["metadata"])
    journal.plan_pages("o/r", rows, {rows[0]["id"]: notes})
    [page] = journal.db.execute("SELECT * FROM pages").fetchall()
    payload = page["payload"]
    assert len(payload.encode("utf-8")) <= intake.PAGE_BYTES
    item = json.loads(payload.split("```json\n")[1].split("\n```")[0])["occurrences"][0]
    assert all(item[key] == value for key, value in original.items())
    assert item["status"] == "needs_triage"
    assert "owner_check" not in item  # empty means no candidates, not omitted display
    reference = item["owner_check_ref"]
    assert reference["count"] == len(notes)
    journal.close()
    journal = intake.Journal(path)
    try:
        details = journal.db.execute("SELECT payload FROM owner_check_details WHERE digest=?",
                                     (reference["sha256"],)).fetchone()[0]
        assert json.loads(details) == notes
        assert hashlib.sha256(details.encode("utf-8")).hexdigest() == reference["sha256"]
        board = Board()
        board.fail = True
        assert journal.deliver("o/r", board)[0].startswith("FAILED")
        assert journal.db.execute("SELECT delivered FROM occurrences").fetchone()[0] == 0
        board.fail = False
        board.ambiguous = True
        assert not journal.deliver("o/r", board)[0].startswith("FAILED")
        assert board.notes["card0"] == [payload]
        assert journal.db.execute("SELECT delivered FROM occurrences").fetchone()[0] == 1
        assert journal.deliver("o/r", board) == []
        assert journal.db.execute("SELECT payload FROM owner_check_details").fetchone()[0] == details
    finally:
        journal.close()


@pytest.mark.parametrize("notes", [None, [], candidates(1)])
def test_small_diagnostics_keep_existing_inline_contract(tmp_path, notes):
    journal = intake.Journal(tmp_path / "queue.sqlite3")
    try:
        rows = seed(journal)
        journal.plan_pages("o/r", rows, None if notes is None else {rows[0]["id"]: notes})
        payload = journal.db.execute("SELECT payload FROM pages").fetchone()[0]
        item = json.loads(payload.split("```json\n")[1].split("\n```")[0])["occurrences"][0]
        assert "owner_check_ref" not in item
        if notes is None:
            assert "owner_check" not in item
        else:
            assert item["owner_check"] == notes
    finally:
        journal.close()


@pytest.mark.parametrize("size", [1024, 1025])
def test_serialized_size_boundary(tmp_path, size):
    notes = ["x" * (size - 4)]
    assert len(json.dumps(notes).encode("utf-8")) == size
    journal = intake.Journal(tmp_path / "queue.sqlite3")
    try:
        rows = seed(journal)
        journal.plan_pages("o/r", rows, {rows[0]["id"]: notes})
        payload = journal.db.execute("SELECT payload FROM pages").fetchone()[0]
        item = json.loads(payload.split("```json\n")[1].split("\n```")[0])["occurrences"][0]
        assert ("owner_check_ref" in item) == (size > 1024)
    finally:
        journal.close()


def test_replanning_keeps_content_addressed_history(tmp_path):
    journal = intake.Journal(tmp_path / "queue.sqlite3")
    try:
        for notes in (candidates(60), candidates(60), candidates(61)):
            rows = seed(journal)
            journal.plan_pages("o/r", rows, {rows[0]["id"]: notes})
            journal.recheck_unattempted("o/r")
        stored = [json.loads(row[0]) for row in journal.db.execute(
            "SELECT payload FROM owner_check_details ORDER BY length(payload)")]
        assert stored == [candidates(60), candidates(61)]
    finally:
        journal.close()


def test_details_and_pages_rollback_together(tmp_path, monkeypatch):
    journal = intake.Journal(tmp_path / "queue.sqlite3")
    try:
        rows = seed(journal)
        monkeypatch.setattr(intake, "PAGE_BYTES", 1)
        with pytest.raises(ValueError, match="intake_page_oversize"):
            journal.plan_pages("o/r", rows, {rows[0]["id"]: candidates(60)})
        assert len(journal.pending("o/r")) == 1
        assert journal.db.execute("SELECT count(*) FROM pages").fetchone()[0] == 0
        assert journal.db.execute("SELECT count(*) FROM owner_check_details").fetchone()[0] == 0
    finally:
        journal.close()
