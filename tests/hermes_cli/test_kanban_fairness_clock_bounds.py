"""Clock bounds and pre-upgrade capture compatibility at the persisted boundary."""
import json
import json.scanner

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_fairness_clocks as clocks
from hermes_cli import kanban_fairness_journal as journal
from hermes_cli import kanban_fairness_store as store
from tests.hermes_cli import test_kanban_fairness_journal as helpers
from tests.hermes_cli import test_kanban_fairness_vertical as vertical_helpers

conn = helpers.conn


@pytest.mark.parametrize("bound", [2**63, 10**40])
def test_out_of_sqlite_range_clock_bound_refuses_before_replace(conn, bound):
    journal.arm(conn, "original")
    with pytest.raises(journal.JournalError, match="clock_error_ns"):
        journal.arm(conn, "bad", replace=True, clock_error_ns=bound)
    assert journal.status(conn)["capture_id"] == "original"


@pytest.mark.parametrize("bound", ["inf", "-inf", "nan", "1e30", "1e300", "-1"])
def test_cli_invalid_clock_bound_is_tool_error_without_capture(conn, tmp_path, bound):
    out = tmp_path / "invalid"
    result = vertical_helpers.cli.main([
        "capture", "arm", "--out", str(out), "--capture-id", "bad",
        "--db", journal.identity(conn)["path"], "--writer-clocks", "--clock-error-s=" + bound])
    assert result == 3
    assert journal.status(conn) is None
    assert not (out / "plan.json").exists()


def test_sample_bound_includes_sampling_interval(monkeypatch):
    ticks = iter([100, 137])
    monkeypatch.setattr(clocks.time, "monotonic_ns", lambda: next(ticks))
    monkeypatch.setattr(clocks.time, "time_ns", lambda: 123456789)
    assert json.loads(clocks.sample(20)) == {
        "wall_ns": 123456789, "mono_ns": 100, "clock_error_ns": 57}


def test_deep_persisted_clock_reports_gap_without_advancing(conn, tmp_path, monkeypatch):
    kb.create_task(conn, title="deep", assignee="a")
    journal.arm(conn, "deep", clock_error_ns=0)
    # Exercise the real recursive decoder: native JSON implementations may decode
    # arbitrarily deep arrays without recursion (including the CI interpreter).
    decoder = json.JSONDecoder()
    decoder.scan_once = json.scanner.py_make_scanner(decoder)
    monkeypatch.setattr(json, "_default_decoder", decoder)
    depth = 100000
    raw = "[" * depth + "0" + "]" * depth
    with pytest.raises(RecursionError):
        json.loads(raw)
    conn.execute(f"UPDATE {journal.JOURNAL} SET clock_json=?", (raw,))
    receipts = store.drain(conn, tmp_path)
    assert "export_failed" in {g["code"] for g in receipts["gaps"]}
    assert receipts["journal_seq"] == 0


def test_blob_task_id_reports_type_error_gap_without_advancing(conn, tmp_path):
    kb.create_task(conn, title="blob", assignee="a")
    journal.arm(conn, "blob")
    # SQLite TEXT affinity retains BLOB values; export must record the failure
    # rather than escape or advance past evidence it could not serialize.
    conn.execute(f"UPDATE {journal.JOURNAL} SET task_id=?", (b"\xff\x00",))
    receipts = store.drain(conn, tmp_path)
    assert {"code": "export_failed", "detail": "TypeError"} in receipts["gaps"]
    assert receipts["journal_seq"] == 0


def test_pre_upgrade_session_can_be_inspected_and_sealed(conn):
    journal.arm(conn, "old")
    conn.execute(f"ALTER TABLE {journal.SESSION} DROP COLUMN clock_error_ns")
    assert journal.status(conn)["triggers_complete"] is True
    sealed = journal.seal(conn)
    assert sealed["sealed_ms"] is not None
    assert sealed["trigger_integrity"] is True
