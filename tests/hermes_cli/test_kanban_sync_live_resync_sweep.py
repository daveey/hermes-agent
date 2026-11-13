"""A 410 on a primary board's LIVE file rebuilds it in place from a snapshot (t_01a1d5f9de4020fa).

A snapshot carries only live rows and ``resync`` only upserts, so a row of this gateway's cards that the Board deleted
below its retention floor stayed in the live file forever. The rebuild now sweeps such rows, and only those: rows of
this gateway's cards (``created_on`` this gateway, or unset), absent from the snapshot, with no outbox op (an unpushed
or refused local write), no local tombstone, and not written after the rebuild began. A picked-up copy of another
gateway's card is another partition's row and is never swept. The replica path already unlinks and rebuilds its file.
"""
from __future__ import annotations

import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_client as ksc
from hermes_cli import kanban_sync_resync_sweep as sweep
from tests.hermes_cli import kanban_sync_support as support
from tests.hermes_cli.kanban_sync_fake_board import FakeHub, FakeService

ME, OTHER, BOARD = "macbook", "mini", "default"


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    return support.kanban_home(tmp_path, monkeypatch)


@pytest.fixture
def svc():
    return FakeService()


@pytest.fixture
def live(kanban_home):
    support.write_sync_config(kanban_home, True)
    conn = kbc.connect(kanban_home / "kanban.db")
    ksc.ensure_identity(conn, BOARD, ME)
    sync.set_mode(conn, "primary")
    try:
        yield conn
    finally:
        conn.close()


class OpsDown(FakeHub):
    """/ops answers 503 while /changes and the snapshot keep working: a local write stays in the outbox."""

    def ops(self, board, ops):
        raise ksc.HubError(503, "service unavailable")


def _round(kanban_home, hub):
    return ksc.sync_board(BOARD, kanban_home / "kanban.db", hub, ME)


def _board_deletes(svc, *rows):
    hlc = f"{time.time_ns() // 1_000_000:013d}-0000-{OTHER}"
    res = svc.hub(OTHER).ops(BOARD, [{"client_op_id": f"del-{i}", "op": "delete", "table": table, "uid": uid,
                                      "hlc": hlc, "fields": {}} for i, (table, uid) in enumerate(rows)])
    assert [r["status"] for r in res["results"]] == ["applied"] * len(rows)


def _ids(conn, table="tasks", key="id"):
    return {r[0] for r in conn.execute(f"SELECT {key} FROM {table}")}


def _picked_up_foreign_copy(conn, task_id):
    """A copy of another gateway's card in this file (pickup), which the Board no longer holds."""
    with kbc.write_txn(conn):
        conn.execute("UPDATE kanban_sync_state SET applying = 1")
        conn.execute("INSERT INTO tasks (id, title, status, created_at, created_on) VALUES (?, 'foreign', 'ready', 1, ?)",
                     (task_id, OTHER))
        conn.execute("UPDATE kanban_sync_state SET applying = 0")


def test_a_410_on_the_live_file_sweeps_own_rows_the_board_deleted_and_keeps_local_work(live, svc, kanban_home):
    gone = kb.create_task(live, title="deleted on the Board")
    kept = kb.create_task(live, title="still on the Board")
    kb.add_comment(live, kept, "me", "a comment the Board deleted")
    kb.add_comment(live, kept, "me", "a comment the Board keeps")
    assert _round(kanban_home, svc.hub(ME)).error is None
    assert not live.execute("SELECT 1 FROM kanban_sync_outbox").fetchone()  # everything acked
    dropped, comment_kept = [r[0] for r in live.execute("SELECT uid FROM task_comments ORDER BY id")]
    _board_deletes(svc, ("task_comments", dropped), ("tasks", gone))
    svc.compact(BOARD)  # the deletes are below the floor now: the live cursor gets 410
    assert int(sync.sync_state(live)["cursor"]) < svc.board(BOARD)["floor"]

    local = kb.create_task(live, title="unpushed local card")  # its op waits in the outbox
    _picked_up_foreign_copy(live, "t_foreign_copy")
    with pytest.raises(ksc.HubError):  # the send after the pulls fails: the local card stays unpushed
        _round(kanban_home, OpsDown(svc, ME))

    tasks = _ids(live)
    assert gone not in tasks  # RED before: the snapshot only upserts, the Board's delete never reached this file
    assert dropped not in _ids(live, "task_comments", "uid")
    assert {kept, local, "t_foreign_copy"} <= tasks
    assert comment_kept in _ids(live, "task_comments", "uid")
    assert live.execute("SELECT 1 FROM kanban_sync_outbox WHERE row_uid = ? AND state = 'pending'",
                        (local,)).fetchone()
    assert live.execute("SELECT 1 FROM kanban_sync_tombstones WHERE table_name = 'tasks' AND row_uid = ?",
                        (gone,)).fetchone()  # a feed delete's tombstone: a stale copy can never bring it back
    assert int(sync.sync_state(live)["cursor"]) == svc.board(BOARD)["seq"]


def test_the_sweep_skips_rows_held_by_the_outbox_a_tombstone_or_written_after_the_rebuild_began(live, svc):
    held = kb.create_task(live, title="refused edit")
    stone = kb.create_task(live, title="tombstoned")
    fresh = kb.create_task(live, title="written during the rebuild")
    absent = kb.create_task(live, title="really gone")
    with kbc.write_txn(live):
        live.execute("DELETE FROM kanban_sync_outbox WHERE row_uid != ?", (held,))
        live.execute("UPDATE kanban_sync_outbox SET state = 'rejected'")  # still a local write the Board never took
        live.execute("INSERT INTO kanban_sync_tombstones (table_name, row_uid, deleted_at) VALUES ('tasks', ?, 1)",
                     (stone,))
        live.execute("UPDATE kanban_sync_state SET applying = 1")  # capture off: no outbox op keeps 'fresh'
        live.execute("UPDATE tasks SET created_at = ? WHERE id = ?", (int(time.time()) + 60, fresh))
        live.execute("UPDATE kanban_sync_state SET applying = 0")
    assert not live.execute("SELECT 1 FROM kanban_sync_outbox WHERE row_uid = ?", (fresh,)).fetchone()
    swept = sweep.sweep_absent(live, {t: set() for t in ksc.IMPORT_ORDER}, BOARD, ME, since=int(time.time()))
    assert swept.get("tasks") == 1
    assert _ids(live) == {held, stone, fresh}
    assert absent not in _ids(live)


def test_a_row_that_gains_an_outbox_op_after_the_candidate_read_is_not_swept(live, svc, monkeypatch):
    racer = kb.create_task(live, title="edited between the candidate read and the apply")
    absent = kb.create_task(live, title="really gone")
    with kbc.write_txn(live):
        live.execute("DELETE FROM kanban_sync_outbox")
    real_apply = sync.apply_remote_changes

    def local_edit_then_apply(conn, changes, **kw):  # a local write lands after the candidates were read
        assert {c.uid for c in changes if c.table == "tasks"} == {racer, absent}
        with kbc.write_txn(conn):
            conn.execute("UPDATE tasks SET title = 'edited locally' WHERE id = ?", (racer,))
        assert conn.execute("SELECT 1 FROM kanban_sync_outbox WHERE row_uid = ?", (racer,)).fetchone()
        return real_apply(conn, changes, **kw)

    monkeypatch.setattr(sync, "apply_remote_changes", local_edit_then_apply)
    swept = sweep.sweep_absent(live, {t: set() for t in ksc.IMPORT_ORDER}, BOARD, ME, since=int(time.time()) + 60)
    assert swept.get("tasks") == 1  # the in-transaction re-check refused the racer
    assert racer in _ids(live)
    assert absent not in _ids(live)


def test_an_incomplete_snapshot_sweeps_nothing(live, svc):
    tid = kb.create_task(live, title="acked")
    with kbc.write_txn(live):
        live.execute("DELETE FROM kanban_sync_outbox")
    assert sweep.sweep_absent(live, {}, BOARD, ME, since=int(time.time()) + 60) == {}
    assert tid in _ids(live)
