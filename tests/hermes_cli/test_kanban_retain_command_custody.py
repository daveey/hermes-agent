"""Existing failure-command custody gains retain bytes, never a second hold ledger."""
import pytest

from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_failure_release as release
from tests.hermes_cli import test_kanban_failure_transport as support

live = support.live
COLUMNS = {"operation_kind", "request_uid", "event_uid", "request_json", "request_sha256",
           "delivery_state", "canonical_receipt_json"}


def test_real_three_column_command_migration_preserves_legacy_unknown(live):
    tid, rid = support._running(live)
    uid = sync.run_uid(live, rid)
    with kbc.write_txn(live):
        live.execute("CREATE TABLE kanban_failure_commands (run_uid TEXT PRIMARY KEY, "
                     "task_id TEXT NOT NULL, op_id TEXT NOT NULL)")
        live.execute("INSERT INTO kanban_failure_commands VALUES (?,?,?)", (uid, tid, "sync"))
        release._ensure_commands(live)
        release._ensure_commands(live)
    columns = {row[1] for row in live.execute("PRAGMA table_info(kanban_failure_commands)")}
    assert COLUMNS <= columns
    row = live.execute("SELECT * FROM kanban_failure_commands").fetchone()
    assert (row["run_uid"], row["task_id"], row["op_id"]) == (uid, tid, "sync")
    assert all(row[name] is None for name in COLUMNS)
    assert release.queued(live, rid)
    assert release.command_owned(live, rid)


@pytest.mark.parametrize("synchronous", [False, True])
def test_new_release_writers_name_columns_and_never_attest_retain(live, synchronous):
    tid, rid = support._running(live)
    uid = sync.run_uid(live, rid)
    with kbc.write_txn(live):
        release._ensure_commands(live)
        if synchronous:
            release.mark_sent(live, uid, tid)
        else:
            assert release.queue(live, tid, rid, outcome="crashed", error="isolated fixture")
    row = live.execute("SELECT * FROM kanban_failure_commands").fetchone()
    assert row["operation_kind"] == "release"
    assert row["canonical_receipt_json"] is None
    assert release.queued(live, rid)
    assert release.command_owned(live, rid)


def test_retain_custody_is_not_legacy_queued_release(live):
    tid, rid = support._running(live)
    uid = sync.run_uid(live, rid)
    with kbc.write_txn(live):
        release._ensure_commands(live)
        live.execute("INSERT INTO kanban_failure_commands (run_uid,task_id,op_id,operation_kind) "
                     "VALUES (?,?,?,'retain')", (uid, tid, "retained-command"))
    assert release.command_owned(live, rid)
    assert not release.queued(live, rid)
