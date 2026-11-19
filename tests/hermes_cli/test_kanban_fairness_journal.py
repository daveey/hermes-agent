"""The queue-fairness change journal on a real board database: what it records, its bound, and how a
capture directory reports loss. [daveey]"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_fairness_journal as journal
from hermes_cli import kanban_fairness_store as store


@pytest.fixture
def conn(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    with kbc.connect_closing() as handle:
        yield handle


def _rows(conn):
    return [(kind, task_id, op, json.loads(fields)) for _seq, kind, _ms, task_id, op, fields in journal.rows(conn)]


def _codes(out):
    return [gap["code"] for gap in store.load(out)["gaps"]]


def test_opening_snapshot_and_row_versions(conn):
    old = kb.create_task(conn, title="before", assignee="a")
    done = kb.create_task(conn, title="finished", assignee="a")
    kb.archive_task(conn, done)
    journal.arm(conn, "c1")
    new = kb.create_task(conn, title="after", assignee="a", priority=7)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET title = 'renamed' WHERE id = ?", (new,))  # not a queue-relevant field
        conn.execute("UPDATE tasks SET priority = 9 WHERE id = ?", (new,))
    rows = _rows(conn)
    assert [(k, t) for k, t, _op, _f in rows if k == "opening"] == [("opening", old)]
    versions = [(op, f["status"], f["priority"]) for k, t, op, f in rows if k == "task" and t == new]
    assert versions[0] == ("insert", "ready", 7) and versions[-1] == ("update", "ready", 9)
    assert len([v for v in versions if v[0] == "update"]) == 1
    assert ("event", new, None) in [(k, t, op) for k, t, op, _f in rows]
    assert "body" not in rows[0][3] and "title" not in rows[0][3]


def test_rollback_removes_the_journal_rows_too(conn):
    tid = kb.create_task(conn, title="x", assignee="a")
    journal.arm(conn, "c1")
    before = journal.status(conn)
    with pytest.raises(RuntimeError):
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET priority = 5 WHERE id = ?", (tid,))
            raise RuntimeError("abort")
    after = journal.status(conn)
    assert (after["stored"], after["last_seq"]) == (before["stored"], before["last_seq"])


def test_sync_applied_changes_and_deletes_are_recorded(conn):
    tid = kb.create_task(conn, title="x", assignee="a")
    journal.arm(conn, "c1")
    has_sync = conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'kanban_sync_state'").fetchone()
    with kb.write_txn(conn):
        if has_sync:  # the sync capture triggers skip writes made while a feed page is applied
            conn.execute("UPDATE kanban_sync_state SET applying = 1")
        conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (tid,))
        if has_sync:
            conn.execute("UPDATE kanban_sync_state SET applying = 0")
        conn.execute("DELETE FROM tasks WHERE id = ?", (tid,))
    ops = [(op, f.get("status")) for k, t, op, f in _rows(conn) if k == "task" and t == tid]
    assert ops == [("update", "running"), ("delete", None)]


def test_bound_stops_storing_and_counts_the_loss(conn, tmp_path):
    journal.arm(conn, "c1", max_records=3)
    made = [kb.create_task(conn, title=f"t{i}", assignee="a") for i in range(4)]
    state = journal.status(conn)
    assert state["stored"] == 3 and state["dropped"] > 0 and state["first_drop_ms"] is not None
    assert len(_rows(conn)) == 3
    # The writer was never refused: every card exists.
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == len(made)
    out = tmp_path / "evidence"
    store.write_manifest(conn, out, board="default", assumptions={}, policy={})
    assert "journal_dropped" in _codes(out)


def test_opening_backlog_larger_than_the_bound_is_a_loss(conn, tmp_path):
    for i in range(3):
        kb.create_task(conn, title=f"t{i}", assignee="a")
    state = journal.arm(conn, "c1", max_records=2)
    assert (state["stored"], state["dropped"]) == (2, 1)


def test_seal_stops_capture_and_survives_a_reopen(conn, tmp_path):
    out = tmp_path / "evidence"
    journal.arm(conn, "c1")
    kb.create_task(conn, title="x", assignee="a")
    store.drain(conn, out)
    with kbc.connect_closing() as again:  # another connection, as after a process restart
        kb.create_task(again, title="y", assignee="a")
        manifest = store.write_manifest(again, out, board="default", assumptions={}, policy={})
        kb.create_task(again, title="after the seal", assignee="a")
        assert journal.status(again)["triggers"] == []
        assert journal.status(again)["stored"] == manifest["journal"]["stored"]
    capture = store.load(out)
    expected_gaps = {"source_identity_unknown", "clock_unknown", "source_uid_unknown"}
    if journal.status(conn).get("trigger_integrity") is not True:
        expected_gaps.add("trigger_loss")
    assert {g["code"] for g in capture["gaps"]} == expected_gaps
    assert len(manifest["segments"]) == 2
    assert [r["seq"] for r in capture["records"]] == list(range(1, len(capture["records"]) + 1))


def test_arm_refuses_a_second_capture(conn):
    journal.arm(conn, "c1")
    with pytest.raises(journal.JournalError):
        journal.arm(conn, "c2")
    assert journal.arm(conn, "c2", replace=True)["capture_id"] == "c2"
    journal.clear(conn)
    assert journal.status(conn) is None
    kb.create_task(conn, title="x", assignee="a")  # no trigger is left pointing at a dropped table


def test_changed_segment_and_lost_journal_are_gaps(conn, tmp_path):
    out = tmp_path / "evidence"
    journal.arm(conn, "c1")
    kb.create_task(conn, title="x", assignee="a")
    store.write_manifest(conn, out, board="default", assumptions={}, policy={})
    segment = out / "segment-000001.jsonl"
    segment.write_bytes(segment.read_bytes().replace(b'"ready"', b'"todo"', 1))
    assert "segment_mismatch" in _codes(out)

    other = tmp_path / "evidence2"
    journal.arm(conn, "c3", replace=True)
    store.drain(conn, other)
    journal.arm(conn, "c4", replace=True)  # the database now carries someone else's journal
    assert "journal_replaced" in [g["code"] for g in store.drain(conn, other)["gaps"]]
    journal.clear(conn)  # ... or none at all, as after a replica rebuild
    assert "journal_missing" in [g["code"] for g in store.drain(conn, other)["gaps"]]
    assert "unsealed" in _codes(other)


def test_bad_record_line_is_a_gap_not_a_skip(conn, tmp_path):
    out = tmp_path / "evidence"
    journal.arm(conn, "c1")
    kb.create_task(conn, title="x", assignee="a")
    manifest = store.write_manifest(conn, out, board="default", assumptions={}, policy={})
    segment = out / manifest["segments"][0]["name"]
    lines = segment.read_text().splitlines()
    lines[0] = json.dumps({**json.loads(lines[0]), "version": 99})
    data = ("\n".join(lines) + "\n").encode()
    segment.write_bytes(data)
    import hashlib
    manifest["segments"][0]["sha256"] = hashlib.sha256(data).hexdigest()
    manifest["segments"][0]["bytes"] = len(data)
    (out / "manifest.json").write_text(json.dumps(manifest))
    codes = _codes(out)
    assert "bad_record" in codes and "record_count" in codes
