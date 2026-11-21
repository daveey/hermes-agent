"""Transactional observation provenance, not export-time fabrication."""
import json
import sqlite3
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_fairness_journal as journal
from hermes_cli import kanban_fairness_metrics as metrics
from hermes_cli import kanban_fairness_store as store
from tests.hermes_cli import test_kanban_fairness_journal as helpers
from tests.hermes_cli import test_kanban_fairness_metrics as metric_helpers
from tests.hermes_cli import test_kanban_fairness_vertical as vertical_helpers

conn = helpers.conn


def test_writer_clocks_survive_later_export_and_reopen(conn, tmp_path):
    tid = kb.create_task(conn, title="opening", assignee="a")
    before = time.monotonic_ns()
    journal.arm(conn, "clocked", clock_error_ns=2_000_000_000)
    with kbc.connect_closing() as other:
        other.execute("UPDATE tasks SET priority=3 WHERE id=?", (tid,))
    after = time.monotonic_ns()
    out = tmp_path / "capture"
    store.write_manifest(conn, out, board="default", assumptions={}, policy={})
    capture = store.load(out)
    assert {g["code"] for g in capture["gaps"]} == {"source_identity_unknown"}
    records = capture["records"]
    uids = [r["payload"]["source_uid"] for r in records]
    assert len(set(uids)) == len(records)
    assert all(uid.startswith("journal-observation:") for uid in uids)
    assert all(before <= r["mono_ns"] <= after for r in records)
    assert all(r["clock_error_ns"] >= 2_000_000_000 for r in records)
    persisted = conn.execute(f"SELECT source_uid, clock_json FROM {journal.JOURNAL} ORDER BY seq").fetchall()
    assert uids == [row[0] for row in persisted]
    assert [r["mono_ns"] for r in records] == [json.loads(row[1])["mono_ns"] for row in persisted]
    assert [r["wall_ns"] for r in records] == [json.loads(row[1])["wall_ns"] for row in persisted]


def test_clock_observation_rolls_back_with_writer(conn):
    tid = kb.create_task(conn, title="rollback", assignee="a")
    journal.arm(conn, "clocked", clock_error_ns=0)
    before = list(conn.execute(f"SELECT * FROM {journal.JOURNAL}"))
    with pytest.raises(RuntimeError), kb.write_txn(conn):
        conn.execute("UPDATE tasks SET priority=3 WHERE id=?", (tid,))
        raise RuntimeError("abort")
    assert list(conn.execute(f"SELECT * FROM {journal.JOURNAL}")) == before


def test_legacy_rows_are_not_backfilled(conn, tmp_path):
    kb.create_task(conn, title="old", assignee="a")
    journal.arm(conn, "legacy")
    conn.execute(f"ALTER TABLE {journal.JOURNAL} DROP COLUMN source_uid")
    conn.execute(f"ALTER TABLE {journal.JOURNAL} DROP COLUMN clock_json")
    store.write_manifest(conn, tmp_path, board="default", assumptions={}, policy={})
    capture = store.load(tmp_path)
    assert {"clock_unknown", "source_uid_unknown"} <= {g["code"] for g in capture["gaps"]}
    assert all(r["mono_ns"] is None and r["payload"]["source_uid"] is None for r in capture["records"])


@pytest.mark.parametrize("raw", ["[]", "{}", "null", '{"wall_ns":1,"mono_ns":2,"clock_error_ns":true}',
                                 '{"wall_ns":1,"mono_ns":-1,"clock_error_ns":0}', "not json"])
def test_bad_persisted_clock_is_export_gap(conn, tmp_path, raw):
    kb.create_task(conn, title="bad", assignee="a")
    journal.arm(conn, "bad", clock_error_ns=0)
    conn.execute(f"UPDATE {journal.JOURNAL} SET clock_json=?", (raw,))
    receipts = store.drain(conn, tmp_path)
    assert "export_failed" in {g["code"] for g in receipts["gaps"]}
    assert receipts["journal_seq"] == 0


@pytest.mark.parametrize("bound", [-1, True, 1.5])
def test_invalid_clock_bound_does_not_arm(conn, bound):
    with pytest.raises(journal.JournalError):
        journal.arm(conn, "bad", clock_error_ns=bound)
    assert journal.status(conn) is None


def test_unregistered_writer_refuses_atomically_until_clear(conn):
    tid = kb.create_task(conn, title="writer", assignee="a")
    path = journal.identity(conn)["path"]
    journal.arm(conn, "clocked", clock_error_ns=0)
    # Deliberate raw connection models a pre-upgrade or nonnative writer.
    with sqlite3.connect(path) as old:
        with pytest.raises(sqlite3.OperationalError, match="hermes_fairness_clock"):
            old.execute("UPDATE tasks SET priority=3 WHERE id=?", (tid,))
        old.rollback()
        assert old.execute("SELECT priority FROM tasks WHERE id=?", (tid,)).fetchone()[0] == 0
        journal.clear(conn)
    # Reconnect also discards SQLite's cached failed statement/schema on old writers.
    with sqlite3.connect(path) as recovered:
        recovered.execute("UPDATE tasks SET priority=3 WHERE id=?", (tid,))


def test_owner_journal_larger_than_metadata_is_not_truncated(tmp_path):
    line = json.dumps({"version": 1, "kind": "coverage", "source": "core-lane",
                       "from_s": 1, "to_s": 2, "complete": True}) + "\n"
    count = 100000
    (tmp_path / store.OWNERS).write_text(line * count)
    gaps = []
    owners = store._owners(tmp_path, gaps)
    assert owners is not None and len(owners) == count
    assert gaps == []


def test_owner_journal_bound_is_still_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "MAX_OWNER_BYTES", 10)
    (tmp_path / store.OWNERS).write_text(" " * 11)
    gaps = []
    assert store._owners(tmp_path, gaps) == []
    assert {g["code"] for g in gaps} == {"bad_owner_record"}


def test_writer_clock_cli_requires_explicit_database(tmp_path):
    out = tmp_path / "no-capture"
    assert vertical_helpers.cli.main(["capture", "arm", "--out", str(out), "--capture-id", "c",
                                      "--writer-clocks"]) == 3
    assert not out.exists()


def test_writer_clock_cli_persists_opening_samples(conn, tmp_path):
    kb.create_task(conn, title="cli", assignee="a")
    path = journal.identity(conn)["path"]
    assert vertical_helpers.cli.main(["capture", "arm", "--out", str(tmp_path / "cli"),
                                      "--capture-id", "c", "--db", path, "--writer-clocks"]) == 0
    raw = conn.execute(f"SELECT clock_json FROM {journal.JOURNAL}").fetchone()[0]
    assert json.loads(raw)["clock_error_ns"] >= 2_000_000_000


def test_report_does_not_narrow_attested_uncertainty():
    log = metric_helpers.Log()
    log.wait("near", 0, 1799)
    capture = log.capture(3600)
    for rec in capture["records"]:
        rec["clock_error_ns"] = 2_000_000_000
    result = metrics.verdict(capture, alarm_prefixes=())
    assert result["assumptions"]["clock_error_s"] == 2
    assert result["gates"]["p0_median_wait"]["status"] == "INDETERMINATE"
