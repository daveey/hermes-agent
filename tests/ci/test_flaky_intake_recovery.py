"""Independent journal identity and consistent backup recovery controls."""
import sqlite3

import pytest

from scripts.ci.flaky_intake import Journal


def test_pending_rows_and_immutable_pages_survive_backup_restore(tmp_path):
    queue, anchor = tmp_path / "queue.sqlite3", tmp_path / "independent/id.uuid"
    journal = Journal(queue, identity_path=anchor, initialize=True)
    journal.record("o/r", [(9, [{"test_id": "tests/test_x.py::test_y"}])],
                   {"id": 5, "head_sha": "a" * 40})
    journal.plan_pages("o/r", journal.pending("o/r"))
    with journal.db:
        journal.db.execute("UPDATE pages SET attempted=1")
    rows = [tuple(r) for r in journal.db.execute("SELECT * FROM occurrences")]
    pages = [tuple(r) for r in journal.db.execute("SELECT * FROM pages")]
    backup = tmp_path / "backup.sqlite3"
    with sqlite3.connect(backup) as destination:
        journal.db.backup(destination)
    journal.close()
    queue.unlink()
    with sqlite3.connect(backup) as source, sqlite3.connect(queue) as destination:
        source.backup(destination)
    restored = Journal(queue, identity_path=anchor)
    try:
        assert [tuple(r) for r in restored.db.execute("SELECT * FROM occurrences")] == rows
        assert [tuple(r) for r in restored.db.execute("SELECT * FROM pages")] == pages
        assert rows[0][6] == 0  # still pending, not inferred delivered from the backup
        restored.recheck_unattempted("o/r")
        assert [tuple(r) for r in restored.db.execute("SELECT * FROM pages")] == pages
    finally:
        restored.close()


@pytest.mark.parametrize("damage", ["invalid_uuid", "mismatch", "duplicate", "missing_table"])
def test_identity_refusal_releases_lock_and_does_not_modify_queue(tmp_path, damage):
    queue, anchor = tmp_path / "queue.sqlite3", tmp_path / "id.uuid"
    Journal(queue, identity_path=anchor, initialize=True).close()
    with sqlite3.connect(queue) as db:
        if damage == "invalid_uuid":
            anchor.write_text("not-a-uuid\n", encoding="utf-8")
            db.execute("UPDATE journal_identity SET uuid='not-a-uuid'")
        elif damage == "mismatch":
            anchor.write_text("00000000-0000-0000-0000-000000000000\n", encoding="utf-8")
        elif damage == "duplicate":
            db.execute("INSERT INTO journal_identity SELECT uuid FROM journal_identity")
        else:
            db.execute("DROP TABLE journal_identity")
    before = queue.read_bytes()
    for _ in range(2):
        with pytest.raises((ValueError, sqlite3.Error)):
            Journal(queue, identity_path=anchor)
        assert queue.read_bytes() == before


@pytest.mark.parametrize("existing", ["queue", "anchor", "lock"])
def test_explicit_bootstrap_never_clobbers_existing_evidence(tmp_path, existing):
    queue, anchor = tmp_path / "queue.sqlite3", tmp_path / "id.uuid"
    target = {"queue": queue, "anchor": anchor, "lock": queue.with_suffix(".lock")}[existing]
    target.write_text("retained evidence", encoding="utf-8")
    with pytest.raises(OSError, match="already_initialized"):
        Journal(queue, identity_path=anchor, initialize=True)
    assert target.read_text(encoding="utf-8") == "retained evidence"
