"""Mixed versions: legacy retries, guarded freezing, and rollback fail-closed."""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import test_kanban_sync_envelope as fixtures

board = fixtures.board


class Versioned(fixtures.LostReply):
    cap = fixtures.GUARD_CAP

    def status_origin_capability(self, board):
        return self.cap


@pytest.mark.parametrize("cap", [{}, {"status_origin": 1}, {"status_origin": 0}, {"status_origin": True}, None,
                                 {**fixtures.GUARD_CAP, "hlc": "invalid"}, {**fixtures.GUARD_CAP, "frozen_replay": True}])
def test_no_guard_does_not_freeze_and_rebuilds_legacy_retry(board, monkeypatch, cap):
    conn, _ = board
    kb.create_task(conn, title="legacy", assignee="worker")
    hub = Versioned()
    hub.cap = cap
    fixtures.send(conn, hub)
    assert conn.execute("SELECT COUNT(*) FROM kanban_sync_outbox WHERE wire_json IS NOT NULL").fetchone()[0] == 0
    clocks = []
    real = client._next_hlc
    monkeypatch.setattr(client, "_next_hlc", lambda *args: clocks.append(real(*args)) or clocks[-1])
    fixtures.send(conn, hub)
    assert clocks  # legacy sender rebuilt, NOT unsafe immutable retry against old Worker
    assert hub.batches[0] != hub.batches[1]


def test_worker_rollback_holds_identical_uncertain_bytes_before_ops(board):
    conn, _ = board
    kb.create_task(conn, title="guarded", assignee="worker")
    hub = Versioned()
    fixtures.send(conn, hub)
    saved = list(conn.execute("SELECT wire_json FROM kanban_sync_outbox ORDER BY rowid"))
    hub.cap = {}
    with pytest.raises(OSError, match="capability absent"):
        client.push(conn, hub, "default", "sender", primary=False)
    assert len(hub.batches) == 1
    assert list(conn.execute("SELECT wire_json FROM kanban_sync_outbox ORDER BY rowid")) == saved
    hub.cap = fixtures.GUARD_CAP
    fixtures.send(conn, hub)
    assert hub.batches[0] == hub.batches[1]


def test_synthetic_parent_uses_capture_not_later_event_at_send(board):
    conn, _ = board
    tid = kb.create_task(conn, title="initial", assignee="worker")
    with kbc.write_txn(conn):
        conn.execute("DELETE FROM kanban_sync_outbox")
        # A separately arriving lifecycle event must not retroactively rebind the row.
        kb._append_event(conn, tid, "reopened", {})
    kb.add_comment(conn, tid, "author", "child")
    hub = Versioned()
    fixtures.send(conn, hub)
    parent = next(o for o in json.loads(hub.batches[0]) if o.get("table") == "tasks")
    assert parent["status_origin"] == ""


def test_unknown_historical_status_keeps_legacy_and_is_not_backfilled(board):
    conn, _ = board
    tid = kb.create_task(conn, title="historical", assignee="worker")
    with kbc.write_txn(conn):
        conn.execute("UPDATE kanban_sync_outbox SET status_origin=NULL")
        conn.execute("DELETE FROM kanban_sync_status_origins WHERE task_id=?", (tid,))
    hub = Versioned()
    fixtures.send(conn, hub)
    task_op = next(o for o in json.loads(hub.batches[0]) if o.get("table") == "tasks")
    assert "status_origin" not in task_op
    assert conn.execute("SELECT wire_json FROM kanban_sync_outbox WHERE table_name='tasks'").fetchone()[0] is None


@pytest.mark.parametrize("multiple", [False, True])
def test_full_row_enrichment_cannot_attach_old_origin_to_new_status(board, multiple):
    conn, _ = board
    tid = kb.create_task(conn, title="offline", assignee="worker")
    with kbc.write_txn(conn):
        if multiple:
            conn.execute("UPDATE tasks SET status='running' WHERE id=?", (tid,))
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
        kb._append_event(conn, tid, "completed", {})
    hub = Versioned()
    fixtures.send(conn, hub)
    ops = [o for o in json.loads(hub.batches[0]) if o.get("table") == "tasks"]
    # Before first acknowledgement these are full CURRENT snapshots, not old
    # deltas mixed with new metadata. Only the current status's own capture may
    # bind them. Ambiguous multiple-intent transactions retain legacy behavior.
    assert all(o["fields"]["status"] == "done" for o in ops)
    if multiple:
        assert all("status_origin" not in o for o in ops)
    else:
        completed = conn.execute("SELECT uid FROM task_events WHERE task_id=? AND kind='completed'", (tid,)).fetchone()[0]
        assert all(o["status_origin"] == completed for o in ops)

